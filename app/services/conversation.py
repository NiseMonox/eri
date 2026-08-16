"""对话层:自由文本的统一大脑。Telegram 与 Siri(/api/ingest/text)都走 handle()。
快路径=正则(零延迟);其余带上下文(开放事项+近期对话)一次 LLM 调用出动作 JSON。"""

import json
import re

from .. import clock, db, events
from ..bot import parser
from ..llm import base as llm
from . import intents, meds, reminders

SYSTEM = """你是家庭健康秘书的决策器。根据【上下文】和用户消息,输出一个 JSON(只输出 JSON,别的什么都不要):
- 推迟提醒 {"action":"snooze","reminder_id":N,"until":"yyyy-MM-dd HH:mm"}   ← 「下午再去」「过会儿叫我」;until 按当前时间换算(东京时间);"下午"=14:00,"晚上"=20:00,"待会/过会"=+1小时
- 完成提醒 {"action":"done","reminder_id":N}   ← 「那我去健身咯」「做完了」「已经去过了」
- 取消提醒 {"action":"dismiss","reminder_id":N} ← 「今天不去了」「算了取消吧」
- 确认服药 {"action":"med_confirm"}
- 记体重 {"action":"weight","weight_kg":62.5,"measured_at":null 或 "yyyy-MM-dd HH:mm"}
- 新建提醒 {"action":"reminder","title":"...","due_at":"yyyy-MM-dd HH:mm"}
- 今日待办 {"action":"today"} / 体重曲线 {"action":"chart"} / 周报 {"action":"report"}
- 问身体数据(「体脂率多少」「最近体重怎么样」)→ 直接用【身体数据】里的数字回答 {"action":"chat","reply":"..."}
- 闲聊或无法判断 {"action":"chat","reply":"简短自然的【日语】回应(用户是日语环境,回复一律日语,轻松的だよ/ね口吻)"}
指代消解:用户说的事必须对应【开放事项】里的 id;没有对应事项却像完成/推迟时,用 chat 问清楚。\n注意:用户消息可能是中文或日语,都要理解;但 chat 的 reply 一律用日语。"""

STANDARD_INTENTS = {"weight", "med_confirm", "reminder", "today", "chart", "report"}


def _log(via: str, role: str, text: str) -> None:
    conn = db.get_db()
    conn.execute("INSERT INTO chat_log (ts, via, role, text) VALUES (?,?,?,?)",
                 (clock.now_iso(), via, role, text[:500]))
    conn.commit()


def _recent_chat(hours: int = 6, limit: int = 6) -> list[dict]:
    from datetime import timedelta

    cutoff = clock.iso(clock.now_utc() - timedelta(hours=hours))
    rows = db.get_db().execute(
        "SELECT * FROM chat_log WHERE ts>=? ORDER BY id DESC LIMIT ?", (cutoff, limit)
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def _context() -> str:
    lines = [f"当前时间(东京):{clock.now_local().strftime('%Y-%m-%d %H:%M (%A)')}"]
    opens = reminders.open_items()
    if opens:
        lines.append("【开放事项】(提醒)")
        for r in opens:
            state = {"pending": "待触发", "notified": "已提醒,等回应"}.get(r["status"], r["status"])
            lines.append(f"- id={r['id']} 「{r['title']}」 {clock.fmt_local(r['due_at'])} [{state}]")
    pend_meds = meds.list_logs(status="pending", limit=5)
    if pend_meds:
        lines.append("【开放事项】(服药待确认)")
        for m in pend_meds:
            lines.append(f"- {m['med_name']} 应服于 {clock.fmt_local(m['due_at'])}")
    from . import body_metrics, weights

    st = weights.stats(7)
    if st:
        lines.append(f"【身体数据】最新体重 {st['last']}kg(7日变化 {st['delta']:+.2f}kg)")
        body_line = body_metrics.summary_ja()
        if body_line:
            lines.append(f"最新体成分:{body_line}")
    chat = _recent_chat()
    if chat:
        lines.append("【最近对话】")
        for c in chat:
            who = "用户" if c["role"] == "user" else "秘书"
            lines.append(f"{who}: {c['text']}")
    return "\n".join(lines)


async def handle(text: str, via: str) -> dict:
    """返回 {"ok", "reply", "photo"}。"""
    text = text.strip()
    fast = parser.parse_regex(text)
    if fast:
        res = await intents.execute(fast, via=via)
        _log(via, "user", text)
        _log(via, "assistant", res["reply"])
        return res

    raw = await llm.complete(f"【上下文】\n{_context()}\n\n用户消息:{text}", system=SYSTEM)
    action = _parse_json(raw)
    res = await _execute(action, text, via)
    _log(via, "user", text)
    _log(via, "assistant", res["reply"])
    return res


def _parse_json(raw: str | None) -> dict:
    if not raw:
        return {"action": "chat",
                "reply": "ごめん、いま頭がうまく回らないみたい。「62.5」で体重記録、「薬飲んだ」で服薬確認はできるよ"}
    try:
        cleaned = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        d = json.loads(cleaned)
        if isinstance(d, dict) and (d.get("action") or d.get("intent")):
            return d
    except (json.JSONDecodeError, TypeError):
        pass
    return {"action": "chat", "reply": (raw or "")[:200]}


async def _execute(action: dict, user_text: str, via: str) -> dict:
    kind = action.get("action") or action.get("intent")

    # LLM 给的 reminder_id 一律先对【开放事项】验明正身:
    # 幻觉 id 不能复活历史行、不能吞掉未来提醒(系统提示词不是防线,这里才是)
    if kind in ("snooze", "done", "dismiss"):
        open_ids = {r["id"] for r in reminders.open_items()}
        try:
            rid = int(action["reminder_id"])
        except (KeyError, TypeError, ValueError):
            return _r(False, "どの件のことか分からなかったよ。もう少し具体的に言ってくれる?")
        if rid not in open_ids:
            return _r(False, "該当する予定が見つからなかったよ。どの件のこと?")

    if kind == "snooze":
        try:
            until = clock.parse_flexible_jst(str(action["until"]))
        except (TypeError, ValueError):
            return _r(False, "いつに延ばすか聞き取れなかったよ。「午後2時にまた」みたいに言ってね")
        row = reminders.snooze(rid, until)
        if row is None:
            return _r(False, "延ばす対象のリマインダーが見つからなかったよ")
        t_local = clock.to_local(clock.parse_iso(row["due_at"]))
        from ..audio import tts

        await tts.announce_snoozed(row["title"], t_local)
        return _r(True, f"OK、「{row['title']}」は {t_local.strftime('%H:%M')} にまた声かけるね")

    if kind == "done":
        row = reminders.set_status(rid, "done")
        if row is None or row["status"] != "done":
            return _r(False, "そのリマインダーが見つからなかったよ")
        from ..audio import tts

        await tts.announce_done(row["title"])
        return _r(True, f"記録したよ:「{row['title']}」今日は完了 ✅")

    if kind == "dismiss":
        row = reminders.set_status(rid, "dismissed")
        if row is None or row["status"] != "dismissed":
            return _r(False, "そのリマインダーが見つからなかったよ")
        return _r(True, f"OK、「{row['title']}」は今日はナシね。もう催促しないよ")

    if kind in STANDARD_INTENTS:
        payload = dict(action)
        payload["intent"] = kind
        return await intents.execute(payload, via=via)

    reply = action.get("reply") or "ごめん、よくわからなかった。言い方を変えてみて?"
    return _r(True, reply)


def _r(ok: bool, reply: str) -> dict:
    return {"ok": ok, "reply": reply, "photo": None}
