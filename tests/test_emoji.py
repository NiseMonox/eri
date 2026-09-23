"""艾莉对外的文字不带 emoji:emoji.strip 本身、通知出口、app/ 源码里的固定文案。"""

from pathlib import Path

from app import emoji
from app.notify import service as notify_service

ROOT = Path(__file__).parent.parent
# 允许出现 emoji 字面量的文件:都不是对外输出
ALLOWED = {
    "app/emoji.py",        # 要删的字符表本身
    "app/bot/parser.py",   # 输入端:用户发来的「飲んだ✅」也要认得
    "app/db.py",           # 历史迁移 SQL(v4→v5 当年写入的图标),改了会让旧库升级结果不一致
}


def test_strip_removes_emoji_and_the_space_after_it():
    assert emoji.strip("✅ 完了 09:12") == "完了 09:12"
    assert emoji.strip("OK、14時にまた声かけるね👍") == "OK、14時にまた声かけるね"
    assert emoji.strip("⚖️ 70.2kg 記録したよ") == "70.2kg 記録したよ"
    assert emoji.strip("家族👨‍👩‍👧 と 1️⃣ 位 🇯🇵") == "家族と 1位"          # ZWJ 序列、键帽、国旗
    assert emoji.strip("今日の予定:\n✅ 08:00 散歩\n🔔 21:00 日記") == "今日の予定:\n08:00 散歩\n21:00 日記"
    assert emoji.strip("🎉") == ""


def test_strip_keeps_japanese_symbols():
    text = "A → B・C〜 ○ ◎ △ × ■ ①「OK」"
    assert emoji.strip(text) is text          # 没有 emoji 时原样返回同一个对象
    assert emoji.find(text) == []


async def test_notify_strips_title_body_and_buttons(fresh_db, monkeypatch):
    got = {}

    async def fake_bark(title, body, **kw):
        got["bark"] = (title, body)
        return True

    async def fake_tg(text, buttons=None):
        got["telegram"] = (text, buttons)
        return True

    monkeypatch.setattr(notify_service.bark, "send", fake_bark)
    monkeypatch.setattr(notify_service.telegram, "send_message", fake_tg)
    await notify_service.notify("med", "💊 お薬の時間:ビタミンD", "✅ タップで完了にできるよ",
                                tg_buttons=[("✅ 飲んだよ", "rdone:1"), ("今回はスキップ", "rskip:1")])
    assert got["bark"] == ("お薬の時間:ビタミンD", "タップで完了にできるよ")
    assert got["telegram"] == ("お薬の時間:ビタミンD\nタップで完了にできるよ",
                               [("飲んだよ", "rdone:1"), ("今回はスキップ", "rskip:1")])


def test_no_emoji_literals_in_app_code():
    """固定文案(回复、通知、按钮、网页)里不写 emoji;出口虽然有兜底,源头也要干净。"""
    hits = []
    for p in sorted((ROOT / "app").rglob("*")):
        rel = p.relative_to(ROOT).as_posix()
        if p.suffix not in (".py", ".html", ".js", ".css") or p.name == "chart.umd.js" or rel in ALLOWED:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if emoji.find(line):
                hits.append(f"{rel}:{i}: {emoji.find(line)}")
    assert hits == []
