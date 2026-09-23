"""本地向量模型(Ollama + bge-m3,见 deploy/ollama)。长期记忆 RAG 用:返回 L2 归一化 float32,余弦 = 点积。
- embed_query:对话用。超时短、失败熔断 60s——拿不到向量就不带相关记忆,绝不拖慢回复。
- embed_batch:夜间整理/回填用。超时长、每批重试一次,仍失败抛 EmbedUnavailable 让整理中止。"""

import time

import httpx
import numpy as np

from .. import events, store


class EmbedUnavailable(RuntimeError):
    pass


QUERY_TIMEOUT = 1.5
BREAKER_SEC = 60
_breaker_until = 0.0      # 熔断截止(monotonic)
_last_err_log = 0.0       # embed_error 事件限频:10 分钟一条


def model() -> str:
    return str(store.get("memory.embed_model", "bge-m3") or "bge-m3")


def _url() -> str:
    return str(store.get("memory.embed_url", "http://127.0.0.1:11434") or "").rstrip("/")


async def _post(texts: list[str], timeout: float) -> np.ndarray:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{_url()}/api/embed", json={
                "model": model(), "input": texts, "keep_alive": -1, "truncate": True})
    except httpx.HTTPError as e:
        raise EmbedUnavailable(f"{type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise EmbedUnavailable(f"HTTP {r.status_code}: {r.text[:200]}")
    vecs = r.json().get("embeddings") or []
    if len(vecs) != len(texts):
        raise EmbedUnavailable(f"返回条数不符:{len(vecs)}/{len(texts)}")
    m = np.asarray(vecs, dtype=np.float32)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return m / norms


def _log_error(e: Exception) -> None:
    global _last_err_log
    if time.monotonic() - _last_err_log > 600:
        _last_err_log = time.monotonic()
        events.log("embed_error", {"error": str(e)[:200]})


async def embed_query(text: str) -> np.ndarray | None:
    """对话用:失败返回 None 并熔断,期间直接 None(不再等超时)。"""
    global _breaker_until
    if time.monotonic() < _breaker_until:
        return None
    try:
        return (await _post([text], timeout=QUERY_TIMEOUT))[0]
    except EmbedUnavailable as e:
        _breaker_until = time.monotonic() + BREAKER_SEC
        _log_error(e)
        return None


async def embed_batch(texts: list[str], batch: int = 32) -> np.ndarray:
    """夜间用:每批 30s 超时、失败重试一次,仍失败抛 EmbedUnavailable。"""
    out = []
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        for attempt in (0, 1):
            try:
                out.append(await _post(chunk, timeout=30))
                break
            except EmbedUnavailable as e:
                if attempt:
                    _log_error(e)
                    raise
    return np.vstack(out) if out else np.zeros((0, 0), dtype=np.float32)


async def prewarm() -> bool:
    """启动时把模型加载进内存(冷加载约 1.6s,超过对话用的 1.5s 超时)。"""
    try:
        await _post(["ping"], timeout=60)
        return True
    except EmbedUnavailable as e:
        _log_error(e)
        return False


def reset() -> None:
    """测试用:清熔断与限频状态。"""
    global _breaker_until, _last_err_log
    _breaker_until = _last_err_log = 0.0
