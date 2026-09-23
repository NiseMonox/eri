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
