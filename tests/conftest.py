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
