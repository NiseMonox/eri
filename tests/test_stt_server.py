"""eri-stt(stt/server.py):解码、静音/过短/超长的拦截、VAD 切段后逐段识别再拼接。
识别器和 VAD 用假的(不加载真模型);ffmpeg 解码是真的。"""

import io
import shutil
import subprocess
import wave
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from stt import server

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")
SR = 16000


def _wav(seconds: float, amp: float = 0.3, freq: float = 440.0) -> bytes:
    t = np.arange(int(SR * seconds)) / SR
    pcm = (amp * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class FakeRecognizer:
    """按调用顺序返回 outputs 里的 (text, lang);记下每次收到的样本数。"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.seen: list[int] = []

    def create_stream(self):
        stream = SimpleNamespace(samples=None, result=None)
        stream.accept_waveform = lambda sr, samples: setattr(stream, "samples", samples)
        return stream

    def decode_stream(self, stream):
        self.seen.append(len(stream.samples))
        text, lang = self.outputs.pop(0)
        stream.result = SimpleNamespace(text=text, lang=f"<|{lang}|>" if lang else "")


class FakeVad:
    """不管输入是什么,都切出预设的几段 [(起, 止)](样本下标)。"""

    def __init__(self, spans):
        self.spans = spans
        self.queue: list = []

    def reset(self):
        self.queue = []

    def accept_waveform(self, chunk):
        pass

    def flush(self):
        self.queue = [SimpleNamespace(start=a, samples=[0.0] * (b - a)) for a, b in self.spans]

    def empty(self):
        return not self.queue

    @property
    def front(self):
        return self.queue[0]

    def pop(self):
        self.queue.pop(0)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(server, "_vad", None)
    monkeypatch.setattr(server, "_load_error", None)
    return TestClient(server.app)     # 不用 with:不跑 lifespan,不加载真模型


def _post(client, data: bytes, name: str = "a.wav", **form):
    return client.post("/v1/audio/transcriptions", files={"file": (name, data)}, data=form)


def test_wav_is_decoded_to_16k_float(client, monkeypatch):
    rec = FakeRecognizer([("今何時?", "ja")])
    monkeypatch.setattr(server, "_recognizer", rec)
    r = _post(client, _wav(1.0))
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "今何時?" and body["language"] == "ja" and body["segments"] == 1
    assert abs(rec.seen[0] - SR) < 400 and abs(body["duration"] - 1.0) < 0.05


def test_m4a_with_index_at_the_end(client, monkeypatch, tmp_path):
    """iOS 录的 m4a 常把 moov 放在文件末尾:走临时文件解码,不能走管道。"""
    src, dst = tmp_path / "a.wav", tmp_path / "a.m4a"
    src.write_bytes(_wav(1.5))
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-c:a", "aac", str(dst)], check=True)
    monkeypatch.setattr(server, "_recognizer", FakeRecognizer([("テスト", "ja")]))
    r = _post(client, dst.read_bytes(), name="rec.m4a")
    assert r.status_code == 200 and r.json()["text"] == "テスト"


def test_silence_and_too_short_skip_the_model(client, monkeypatch):
    rec = FakeRecognizer([])
    monkeypatch.setattr(server, "_recognizer", rec)
    silence = _post(client, _wav(1.0, amp=0.0)).json()
    short = _post(client, _wav(0.2)).json()
    assert silence["text"] == "" and silence["reason"] == "silence"
    assert short["text"] == "" and short["reason"] == "too_short"
    assert rec.seen == []           # 静音送进模型会凭空吐字,所以根本不送


def test_too_long_and_bad_audio(client, monkeypatch):
    monkeypatch.setattr(server, "_recognizer", FakeRecognizer([]))
    assert _post(client, _wav(2.0), max_sec="1").status_code == 413
    r = _post(client, b"\x00\x01not audio at all" * 50, name="x.bin")
    assert r.status_code == 400 and r.json()["detail"].startswith("bad_audio")


def test_model_not_loaded(client, monkeypatch):
    monkeypatch.setattr(server, "_recognizer", None)
    monkeypatch.setattr(server, "_load_error", "FileNotFoundError: 模型不在")
    assert _post(client, _wav(1.0)).status_code == 503
    h = client.get("/health").json()
    assert h["ok"] is False and "模型" in h["error"]


def test_vad_segments_are_recognized_one_by_one(client, monkeypatch):
    """中日交替:每段单独识别、各自判语言;拼接后的语言取说得最久的那种。"""
    rec = FakeRecognizer([("明天早上九点叫我。", "zh"), ("あと、傘を忘れないで。", "ja")])
    monkeypatch.setattr(server, "_recognizer", rec)
    monkeypatch.setattr(server, "_vad", FakeVad([(0, 16000), (24000, 60000)]))
    body = _post(client, _wav(4.0)).json()
    assert body["text"] == "明天早上九点叫我。あと、傘を忘れないで。"
    assert body["segments"] == 2 and body["language"] == "ja"     # 第二段更长
    assert rec.seen[0] == 16000 + server.PAD                       # 前后各带 0.2 秒(开头那段前面没得带)


def test_single_segment_uses_whole_clip(client, monkeypatch):
    rec = FakeRecognizer([("はい", "ja")])
    monkeypatch.setattr(server, "_recognizer", rec)
    monkeypatch.setattr(server, "_vad", FakeVad([(8000, 12000)]))
    assert _post(client, _wav(1.0)).json()["text"] == "はい"
    assert abs(rec.seen[0] - SR) < 400                            # 整段,不是切出来的 0.25 秒


def test_no_speech_from_vad(client, monkeypatch):
    rec = FakeRecognizer([])
    monkeypatch.setattr(server, "_recognizer", rec)
    monkeypatch.setattr(server, "_vad", FakeVad([]))
    body = _post(client, _wav(1.0)).json()
    assert body["text"] == "" and body["reason"] == "no_speech" and rec.seen == []


def test_tidy_and_join():
    assert server.tidy("うち の 中学 は 50 円 の パン") == "うちの中学は50円のパン"
    assert server.tidy("The tribal chieftain") == "The tribal chieftain"
    assert server.join(["Hello.", "World again"]) == "Hello. World again"
    assert server.join(["你好。", "こんにちは"]) == "你好。こんにちは"
    assert server.is_blank("。、 ") and not server.is_blank("はい。")
