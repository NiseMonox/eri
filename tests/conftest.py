import os
import sys
from pathlib import Path

os.environ["HH_TEST"] = "1"
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app import clock, db
from app.config import settings


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    """.env 里是真 DeepSeek key;测试一律清空——漏 mock 的 LLM 调用只会降级成 None,不会打到真 API。"""
    monkeypatch.setattr(settings, "deepseek_api_key", "")


@pytest.fixture(autouse=True)
def _no_real_embed(monkeypatch):
    """本机真跑着 Ollama:测试一律连不上(漏 mock 的检索只会降级);并清掉模块级熔断/向量缓存,防止测试之间串味。"""
    from app.llm import embed
    from app.services import memories

    async def unavailable(texts, timeout):
        raise embed.EmbedUnavailable("tests: no ollama")

    monkeypatch.setattr(embed, "_post", unavailable)
    embed.reset()
    memories._cache.clear()
    yield
    embed.reset()
    memories._cache.clear()


@pytest.fixture(autouse=True)
def _no_real_stt(monkeypatch):
    """本机真跑着 eri-stt:测试一律连不上(漏 mock 的识别只会报 stt_unavailable)。"""
    import httpx

    from app.audio import stt

    async def unavailable(files, data):
        raise httpx.ConnectError("tests: no stt")

    async def dead():
        return False

    monkeypatch.setattr(stt, "_post", unavailable)
    monkeypatch.setattr(stt, "alive", dead)
    stt.reset()


class FakePlayer:
    """假播放器:只记录调用。wait() 阻塞到 stop() 或 finish()(= 这段音频放完了)。
    和 MpvPlayer 一样,play() 会先结束上一段(同一个实例在会话和播报之间共用)。"""

    def __init__(self) -> None:
        import asyncio

        self.calls: list[tuple] = []
        self._running = False
        self._done = asyncio.Event()

    async def play(self, source, volume=100, loop=False):
        import asyncio

        self._done.set()
        self.calls.append(("play", source, volume, loop))
        self._running = True
        self._done = asyncio.Event()

    async def set_volume(self, volume):
        self.calls.append(("volume", volume))

    async def stop(self):
        self.calls.append(("stop",))
        self._running = False
        self._done.set()

    async def wait(self):
        await self._done.wait()

    def running(self):
        return self._running

    def finish(self):
        self._running = False
        self._done.set()


@pytest.fixture(autouse=True)
def _no_real_speaker(monkeypatch):
    """本机真跑着 TTS 引擎和声卡:测试一律不合成、不出声。AudioManager 换成假播放器,tts.synth 直接失败;
    每个测试重置单例状态,锁也换新的(模块级的 asyncio.Lock 会绑在第一个争用它的事件循环上)。"""
    import asyncio

    from app.audio import tts
    from app.audio.manager import audio_manager as am

    player = FakePlayer()
    monkeypatch.setattr(am, "_player", lambda: player)
    am.session, am._paused, am._tasks, am._fade_task, am._announcing = None, None, [], None, False
    am._lock = asyncio.Lock()
    monkeypatch.setattr(tts, "_announce_lock", asyncio.Lock())

    async def no_synth(ja_text):
        raise RuntimeError("tests: no tts engine")

    async def engine_dead():
        return False

    monkeypatch.setattr(tts, "synth", no_synth)
    monkeypatch.setattr(tts, "engine_alive", engine_dead)
    return player


@pytest.fixture()
def fake_player(_no_real_speaker):
    return _no_real_speaker


@pytest.fixture()
def fake_llm(monkeypatch):
    """按顺序回放的假 LLM。script(*steps),每步:[(工具名, 参数), ...] = 发起工具调用;str = 最终回复;
    dict = 原样返回的 assistant message;None = 调用失败。返回每次收到的 messages 快照的列表。"""
    import copy
    import json

    from app.llm import base as llm

    def script(*steps):
        seen = []
        it = iter(steps)

        async def fake_chat(messages, tools=None, timeout=45):
            seen.append(copy.deepcopy(messages))
            step = next(it)
            if step is None or isinstance(step, dict):
                return step
            if isinstance(step, str):
                return {"role": "assistant", "content": step}
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"call_{i}", "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
                for i, (name, args) in enumerate(step)]}

        async def no_complete(*a, **k):
            return None

        monkeypatch.setattr(llm, "chat", fake_chat)
        monkeypatch.setattr(llm, "complete", no_complete)
        return seen

    return script


@pytest.fixture()
def fake_embedder(monkeypatch):
    """确定性假向量:字符 bigram 经 crc32 散列到 256 维再归一化——字面越像,向量越像。
    (不能用 hash():每个进程的盐不同。)返回 vec(text) 供测试自己算相似度。"""
    import zlib

    import numpy as np

    from app.llm import embed

    def vec(text: str) -> np.ndarray:
        v = np.zeros(256, dtype=np.float32)
        t = f"^{text}$"
        for a, b in zip(t, t[1:]):
            v[zlib.crc32((a + b).encode()) % 256] += 1
        n = np.linalg.norm(v)
        return v / n if n else v

    async def fake(texts, timeout):
        return np.vstack([vec(t) for t in texts])

    monkeypatch.setattr(embed, "_post", fake)
    embed.reset()
    return vec


@pytest.fixture()
def fresh_db(tmp_path):
    conn = db.init_db(tmp_path / "test.db")
    yield conn
    clock.set_override(None)


@pytest.fixture()
def sent_notices(monkeypatch):
    """截获 notify,记录而不真发。"""
    calls = []

    async def fake_notify(profile, title, body, **kw):
        calls.append({"profile": profile, "title": title, "body": body, **kw})
        return {"bark": True, "telegram": False}

    import app.notify.service as ns

    monkeypatch.setattr(ns, "notify", fake_notify)
    import app.scheduler.jobs as jobs

    monkeypatch.setattr(jobs, "notify", fake_notify)
    return calls
