"""LLM 层:DeepSeek 请求(模型取 settings)、失败原因落 event_log、off 开关。全部 mock,不打真网络。"""

import json

import httpx

from app import events, store
from app.config import settings
from app.llm import base as llm
from app.llm import deepseek


def _mock_api(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(deepseek.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")


async def test_deepseek_request_uses_model_setting(fresh_db, monkeypatch):
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append((req.headers["authorization"], body["model"],
                     [m["role"] for m in body["messages"]]))
        return httpx.Response(200, json={"choices": [{"message": {"content": "了解だよ"}}]})

    _mock_api(monkeypatch, handler)
    assert await llm.complete("hi", system="sys") == "了解だよ"
    store.set("llm.model", "deepseek-v4-pro")          # 网页 /settings 改完即生效
    assert await llm.complete("hi") == "了解だよ"
    assert seen == [("Bearer sk-test", "deepseek-flash", ["system", "user"]),
                    ("Bearer sk-test", "deepseek-v4-pro", ["user"])]


async def test_deepseek_error_reason_is_logged(fresh_db, monkeypatch):
    """余额不足/key 失效的原因只在响应体里——必须进 event_log,事后才查得出。"""
    _mock_api(monkeypatch, lambda req: httpx.Response(
        402, json={"error": {"message": "Insufficient Balance"}}))
    assert await llm.complete("hi") is None
    [err] = events.recent(kind_prefix="llm_error")
    assert "402" in err["detail"] and "Insufficient Balance" in err["detail"]


async def test_provider_off_skips_api(fresh_db, monkeypatch):
    seen = []
    _mock_api(monkeypatch, lambda req: seen.append(req) or httpx.Response(200, json={}))
    store.set("llm.provider", "off")
    assert await llm.complete("hi") is None
    assert seen == []
