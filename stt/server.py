"""eri-stt:语音识别小服务(SenseVoice-Small int8 + Silero VAD,sherpa-onnx),只听 127.0.0.1,给 health-hub 用。
接口按 OpenAI 的 /v1/audio/transcriptions 的形状(multipart file → {"text"}),以后想换别的识别服务只改 voice.stt_url。
单独一个进程:原生库崩了、内存涨了都只影响识别,不连累提醒主服务。不 import app.*。

SenseVoice 一段音频只判一种语言,而且适合 30 秒以内的短句:整段送 60 秒中日交替的录音,中文部分会整段丢掉。
所以先用 VAD 按停顿切段,每段单独识别、各自判断语言,再拼起来——「一句中文、一句日语」也能认对。
模型用 scripts/fetch_stt_model.sh 下载;VAD 模型缺失时退化为整段识别。"""

import asyncio
import os
import re
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile

MODEL_DIR = Path(os.environ.get(
    "STT_MODEL_DIR", "data/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"))
VAD_MODEL = Path(os.environ.get("STT_VAD_MODEL", "data/models/silero_vad.onnx"))
THREADS = int(os.environ.get("STT_THREADS", "4"))
MAX_SEC = float(os.environ.get("STT_MAX_SEC", "120"))
MAX_BYTES = 25 * 1024 * 1024
SR = 16000
MIN_SEC = 0.3
SILENCE_PEAK = 0.01      # 峰值低于它当静音(满幅 1.0);静音送进模型会凭空吐出「嗯。」这类字
PAD = int(0.2 * SR)      # 每段前后多带 0.2 秒,免得切掉开头结尾的辅音
DECODE_TIMEOUT = 20

_recognizer = None
_vad = None
_vad_window = 512
_load_error: str | None = None
_lock = asyncio.Lock()   # 识别吃满 THREADS 个核、VAD 有状态,一次只处理一段录音

_CJK = "　-〿぀-ヿ㐀-䶿一-鿿＀-￯"
# 模型偶尔在日语词之间插空格(「うち の 中学 は」),汉字/假名之间(以及和数字之间)的空格去掉,英文单词间的保留
_RE_CJK_SPACE = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}0-9])|(?<=[{_CJK}0-9])\s+(?=[{_CJK}])")


def _load():
    import sherpa_onnx

    model, tokens = MODEL_DIR / "model.int8.onnx", MODEL_DIR / "tokens.txt"
    if not (model.is_file() and tokens.is_file()):
        raise FileNotFoundError(f"模型不在 {MODEL_DIR}(先跑 make stt-model)")
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model), tokens=str(tokens), num_threads=THREADS, use_itn=True, language="auto")
    s = rec.create_stream()                    # 预热:第一次解码要分配内存,慢
    s.accept_waveform(SR, np.zeros(SR, dtype=np.float32))
    rec.decode_stream(s)
    vad, window = None, 512
    if VAD_MODEL.is_file():
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(VAD_MODEL)
        cfg.silero_vad.threshold = 0.5
        cfg.silero_vad.min_silence_duration = 0.4    # 句间停顿;一句话里的换气一般更短
        cfg.silero_vad.min_speech_duration = 0.2
        cfg.silero_vad.max_speech_duration = 20      # 一口气说太久也强制切开
        cfg.sample_rate = SR
        vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=MAX_SEC + 10)
        window = cfg.silero_vad.window_size
    return rec, vad, window


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _recognizer, _vad, _vad_window, _load_error
    t = time.monotonic()
    try:
        _recognizer, _vad, _vad_window = await asyncio.to_thread(_load)
        print(f"model loaded: {MODEL_DIR.name} vad={_vad is not None} threads={THREADS} "
              f"in {time.monotonic() - t:.1f}s", flush=True)
    except Exception as e:  # noqa: BLE001 — 模型缺失时进程照常起来、/health 报原因,免得 systemd 反复重启
        _load_error = f"{type(e).__name__}: {e}"
        print(f"model load failed: {_load_error}", flush=True)
    yield


app = FastAPI(title="eri-stt", lifespan=lifespan)


def decode(path: str, max_sec: float) -> np.ndarray:
    """任意格式 → 16 kHz 单声道 float32。读文件而不是管道:iOS 录的 m4a 常把索引(moov)放在末尾,管道读不了。
    多解 1 秒,用来判断是否超长。解不开抛 ValueError。"""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-t", f"{max_sec + 1:.1f}",
           "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=DECODE_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise ValueError("ffmpeg timeout") from e
    if p.returncode != 0:
        raise ValueError(p.stderr.decode(errors="replace").strip()[-300:] or f"ffmpeg exit {p.returncode}")
    return np.frombuffer(p.stdout, dtype=np.float32)


def _recognize_one(samples: np.ndarray) -> tuple[str, str | None]:
    s = _recognizer.create_stream()
    s.accept_waveform(SR, samples)
    _recognizer.decode_stream(s)
    r = s.result
    lang = (getattr(r, "lang", "") or "").strip("<|>") or None    # "<|ja|>" → "ja"
    return tidy(r.text), lang


def _segments(samples: np.ndarray) -> list[tuple[int, int]]:
    """VAD 切出的说话区间 [(起, 止)](样本下标)。"""
    _vad.reset()
    for i in range(0, len(samples), _vad_window):
        _vad.accept_waveform(samples[i:i + _vad_window])
    _vad.flush()
    spans = []
    while not _vad.empty():
        seg = _vad.front
        spans.append((seg.start, seg.start + len(seg.samples)))
        _vad.pop()
    return spans


def recognize(samples: np.ndarray) -> tuple[str, str | None, int]:
    """返回 (文字, 主要语言, 段数)。段数 0 = VAD 没听到人声。语言取说得最久的那种。"""
    spans = _segments(samples) if _vad is not None else [(0, len(samples))]
    if len(spans) <= 1:
        # 没切开(或没有 VAD)就整段识别:只有一句时,裁掉首尾静音再识别反而略差(韩语会多出空格)
        if not spans:
            return "", None, 0
        text, lang = _recognize_one(samples)
        return ("" if is_blank(text) else text), lang, 1
    texts, langs = [], {}
    for a, b in spans:
        text, lang = _recognize_one(samples[max(0, a - PAD):min(len(samples), b + PAD)])
        if text and not is_blank(text):
            texts.append(text)
            if lang:
                langs[lang] = langs.get(lang, 0) + (b - a)
    return join(texts), (max(langs, key=langs.get) if langs else None), len(spans)


def tidy(text: str) -> str:
    return _RE_CJK_SPACE.sub("", text).strip()


def join(parts: list[str]) -> str:
    """各段的识别结果拼成一句;英文开头的段和前面的英文(字母或标点)之间补空格,中日文直接相接。"""
    out = ""
    for p in parts:
        if out and out[-1].isascii() and not out[-1].isspace() and p[0].isascii() and p[0].isalnum():
            out += " "
        out += p
    return out


def is_blank(text: str) -> bool:
    return not any(ch.isalnum() for ch in text)    # 只有标点/空白


@app.post("/v1/audio/transcriptions")
async def transcriptions(file: UploadFile = File(...), max_sec: float | None = Form(None),
                         language: str | None = Form(None), model: str | None = Form(None),
                         response_format: str | None = Form(None)):
    """language/model/response_format 只为兼容 OpenAI 的请求形状:SenseVoice 自动判别语言,一律回 JSON。"""
    if _recognizer is None:
        raise HTTPException(503, f"model not loaded: {_load_error}")
    limit = min(max_sec or MAX_SEC, MAX_SEC)
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "too_large")
    if not data:
        raise HTTPException(400, "bad_audio: empty file")
    t0 = time.monotonic()
    suffix = Path(file.filename or "").suffix[:8]
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(data)
        tmp.flush()
        try:
            samples = await asyncio.to_thread(decode, tmp.name, limit)
        except ValueError as e:
            raise HTTPException(400, f"bad_audio: {e}") from e
    dur = len(samples) / SR
    base = {"text": "", "language": None, "duration": round(dur, 2), "segments": 0}
    if dur > limit + 0.05:
        raise HTTPException(413, "too_long")
    if dur < MIN_SEC:
        return {**base, "reason": "too_short", "elapsed_ms": _ms(t0)}
    if float(np.max(np.abs(samples))) < SILENCE_PEAK:
        return {**base, "reason": "silence", "elapsed_ms": _ms(t0)}
    async with _lock:
        text, lang, nseg = await asyncio.to_thread(recognize, samples)
    # 日志不记原文(journald 里不留对话内容)
    print(f"stt dur={dur:.1f}s segs={nseg} lang={lang} chars={len(text)} elapsed={_ms(t0)}ms", flush=True)
    out = {**base, "text": text, "language": lang, "segments": nseg, "elapsed_ms": _ms(t0)}
    if not text:
        out["reason"] = "no_speech" if nseg == 0 else "empty"
    return out


@app.get("/health")
async def health():
    return {"ok": _recognizer is not None, "vad": _vad is not None, "model": MODEL_DIR.name,
            "threads": THREADS, "error": _load_error}


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
