"""网页:每页登录后都能渲染,输出里没有 emoji(routines.icon 列里存的是 emoji,网页不能再显示它)。"""

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import clock, events
from app.config import settings
from app.services import memories, reminders, routines, weights

EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")
PAGES = ["/", "/schedules", "/routines", "/weights", "/reminders", "/memories", "/settings"]


@pytest.fixture()
def client(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "t0ken")
    from app.main import app

    c = TestClient(app)   # 不用 with:不跑 lifespan(不启调度器/bot,也不去开默认路径的 DB)
    c.cookies.set("hh_token", "t0ken")
    return c


def _seed(conn):
    rt = routines.create("ストレッチ", "care", "20分")
    assert EMOJI.search(rt["icon"])   # 前提:库里的图标确实是 emoji
    now = clock.now_utc()
    for status, due in (("notified", now - timedelta(hours=1)), ("missed", now - timedelta(days=1)),
                        ("done", now - timedelta(days=2))):
        conn.execute(
            "INSERT INTO reminders (title, due_at, status, done_at, created_at, kind, routine_id) "
            "VALUES (?,?,?,?,?,'routine',?)",
            (rt["name"], clock.iso(due), status, clock.iso(due) if status == "done" else None, clock.now_iso(), rt["id"]))
    conn.execute("INSERT INTO schedules (name, type, cron, payload, enabled, created_at, updated_at) "
                 "VALUES ('夜', 'routine', '0 21 * * *', ?, 1, ?, ?)",
                 (f'{{"routine_id": {rt["id"]}}}', clock.now_iso(), clock.now_iso()))
    conn.commit()
    reminders.create("ゴミ出し", "可燃ゴミ", clock.iso(now + timedelta(hours=2)))
    weights.add_weight(62.5, None, source="manual")
    memories.add("fact", "好きな季節は秋", core=True, source="user")
    events.log("notify_error", {"channel": "bark", "error": "boom"})


def test_every_page_renders_without_emoji(client, fresh_db):
    _seed(fresh_db)
    for path in PAGES:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "ストレッチ" in r.text or path in ("/weights", "/memories", "/settings"), path
        hit = EMOJI.search(r.text)
        assert hit is None, f"{path}: {hit.group()!r}"


def test_pages_link_versioned_assets(client, fresh_db):
    html = client.get("/").text
    assert re.search(r'/static/style\.css\?v=\d+', html)
    assert re.search(r'/static/eri\.js\?v=\d+', html)
    assert 'aria-current="page"' in html


def test_public_pages_without_emoji(client, fresh_db):
    client.cookies.clear()
    assert client.get("/", follow_redirects=False).status_code == 302
    for path, text in (("/login", "ログイン"), ("/c/r/nope", "無効なリンク"), ("/s/stop", "停止トークン")):
        r = client.get(path)
        assert r.status_code == 200 and text in r.text, path
        assert EMOJI.search(r.text) is None, path
