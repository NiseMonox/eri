"""对话层:自由文本的统一大脑。Telegram 与 Siri(/api/ingest/text)都走 handle()。
快路径=正则(零延迟);其余带上下文(开放事项+3天对话+长期记忆)一次 LLM 调用出动作 JSON。
每轮结束后异步跑记忆维护(不阻塞回复)。"""

import asyncio
import json
import re
from datetime import timedelta

from .. import clock, db, events, store
from ..bot import parser
from ..llm import base as llm
from . import intents, memories, reminders, routines

SYSTEM = """你是家庭 AI 助手「艾莉」的决策器。根据【上下文】和用户消息,输出一个 JSON(只输出 JSON,别的什么都不要):
- 推迟 {"action":"snooze","reminder_id":N,"until":"yyyy-MM-dd HH:mm"}   ← 「下午再去」「过会儿叫我」;until 按当前时间换算(东京时间);"下午"=14:00,"晚上"=20:00,"待会/过会"=+1小时
- 完成 {"action":"done","reminder_id":N}   ← 「那我去健身咯」「做完了」「已经去过了」「薬飲んだ」;对应【开放事项】里的 routine 或提醒
- 取消 {"action":"dismiss","reminder_id":N} ← 「今天不去了」「算了取消吧」
- 忘掉记忆 {"action":"forget","memory_id":N} ← 「忘掉 XX」「那个不用记了」;对应【长期记忆】里的 id
- 记体重 {"action":"weight","weight_kg":62.5,"measured_at":null 或 "yyyy-MM-dd HH:mm"}
- 新建一次性提醒 {"action":"reminder","title":"...","due_at":"yyyy-MM-dd HH:mm"}
- 今日待办 {"action":"today"} / 体重曲线 {"action":"chart"} / 周报 {"action":"report"}
- 问身体数据(「体脂率多少」)→ 用【身体数据】里的数字回答 {"action":"chat","reply":"..."}
- 问「你记得我什么」→ 用【长期记忆】内容回答 {"action":"chat","reply":"..."}
- 闲聊或无法判断 {"action":"chat","reply":"简短自然的【日语】回应(用户是日语环境,回复一律日语,轻松的だよ/ね口吻)"}
指代消解:done/snooze/dismiss 的 id 必须来自【开放事项】;forget 的 id 必须来自【长期记忆】;没有对应项却像在指代时,用 chat 问清楚。
用户消息可能是中文或日语,都要理解;chat 的 reply 一律日语——不要把用户消息里的中文词原样混进日语回复(如「健身」→「運動/ジム」)。"""

STANDARD_INTENTS = {"weight", "reminder", "today", "chart", "report"}


def _log(via: str, role: str, text: str) -> None:
    conn = db.get_db()
    conn.execute("INSERT INTO chat_log (ts, via, role, text) VALUES (?,?,?,?)",
                 (clock.now_iso(), via, role, text[:500]))
    conn.commit()


def _est_tokens(s: str) -> int:
    """粗估:CJK 每字≈1 token,其他按 4 字符≈1 token。"""
    cjk = sum(1 for ch in s if "　" <= ch <= "鿿" or "＀" <= ch <= "￯")
    return cjk + max(0, len(s) - cjk) // 4 + 1


def _recent_chat() -> list[dict]:
    """3 天窗口 + token 预算(chat.context_budget_tokens,默认 1500):从最新往回取,超预算即停。"""
    hours = int(store.get("chat.context_hours", 72) or 72)
    budget = int(store.get("chat.context_budget_tokens", 1500) or 1500)
    cutoff = clock.iso(clock.now_utc() - timedelta(hours=hours))
    rows = db.get_db().execute(
        "SELECT * FROM chat_log WHERE ts>=? ORDER BY id DESC LIMIT 200", (cutoff,)
    ).fetchall()
    picked, used = [], 0
    for r in rows:
        t = _est_tokens(r["text"])
        if used + t > budget:
            break
        picked.append(dict(r))
        used += t
    return list(reversed(picked))


def _context() -> str:
    lines = [f"当前时间(东京):{clock.now_local().strftime('%Y-%m-%d %H:%M (%A)')}"]

    opens = reminders.open_items()
    if opens:
        lines.append("【开放事项】")
        for r in opens:
            state = {"pending": "待触发", "notified": "已提醒,等回应", "missed": "超时未回应"}.get(
                r["status"], r["status"])
            tag = "routine" if r.get("kind") == "routine" else "提醒"
            lines.append(f"- id={r['id']} [{tag}]「{r['title']}」 {clock.fmt_local(r['due_at'])} [{state}]")

    from . import body_metrics, weights

    st = weights.stats(7)
    if st:
        lines.append(f"【身体数据】最新体重 {st['last']}kg(7日变化 {st['delta']:+.2f}kg)")
        body_line = body_metrics.summary_ja()
        if body_line:
            lines.append(f"最新体成分:{body_line}")

    act = body_metrics.activity_summary_ja()
    if act:
        lines.append(f"【活動】(Apple Watch)最新 {act}")

    mem = memories.context_block()
    if mem:
        lines.append(mem)

    chat = _recent_chat()
    if chat:
        lines.append("【最近对话】(3日以内)")
        for c in chat:
            who = "用户" if c["role"] == "user" else "艾莉"
            when = clock.fmt_local(c["ts"], "%m/%d %H:%M")
            lines.append(f"[{when}] {who}: {c['text']}")
    return "\n".join(lines)


async def handle(text: str, via: str) -> dict:
    """返回 {"ok", "reply", "photo"}。"""
    text = text.strip()
    fast = parser.parse_regex(text)
    if fast:
        res = await _execute(fast, text, via)
        _log(via, "user", text)
        _log(via, "assistant", res["reply"])
        return res

    raw = await llm.complete(f"【上下文】\n{_context()}\n\n用户消息:{text}", system=SYSTEM)
    action = _parse_json(raw)
    res = await _execute(action, text, via)
    _log(via, "user", text)
    _log(via, "assistant", res["reply"])
    if store.get("memory.enabled", True) and raw is not None:
        # 持引用防 GC;决策 LLM 都失败时不再追加一次维护调用
        _bg.add(t := asyncio.create_task(_maintain_safe(text, res["reply"])))
        t.add_done_callback(_bg.discard)
    return res


_bg: set = set()


async def _maintain_safe(user_text: str, reply: str) -> None:
    try:
        await memories.maintain_after_turn(user_text, reply)
    except Exception as e:  # noqa: BLE001
        events.log("memory_error", {"error": str(e)[:200]})


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

    # 正则快路径的 med_confirm / routine_done:找最近待确认实例
    if kind in ("med_confirm", "routine_done"):
        cat = "med" if kind == "med_confirm" else None
        inst = routines.latest_open(cat)
        if inst is None:
            return _r(True, "いま確認待ちのお薬はないよ 👍" if cat == "med" else "いま確認待ちのルーティンはないよ 👍")
        row = routines.complete(inst["id"], via=via)
        from ..audio import tts

        await tts.announce_done(row["title"])
        return _r(True, f"✅ {row['title']}、完了だよ({clock.fmt_local(row['done_at'], '%H:%M')})")

    # LLM 给的 id 一律先对【开放事项】/【长期记忆】验明正身
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
            return _r(False, "延ばす対象が見つからなかったよ")
        t_local = clock.to_local(clock.parse_iso(row["due_at"]))
        from ..audio import tts

        await tts.announce_snoozed(row["title"], t_local)
        return _r(True, f"OK、「{row['title']}」は {t_local.strftime('%H:%M')} にまた声かけるね")

    if kind == "done":
        row = reminders.get(rid)
        if row and row.get("kind") == "routine":
            row = routines.complete(rid, via=via)
        else:
            row = reminders.set_status(rid, "done")
        if row is None or row["status"] != "done":
            return _r(False, "その件が見つからなかったよ")
        from ..audio import tts

        await tts.announce_done(row["title"])
        return _r(True, f"記録したよ:「{row['title']}」完了 ✅")

    if kind == "dismiss":
        row = reminders.get(rid)
        if row and row.get("kind") == "routine":
            row = routines.skip(rid, via=via)
        else:
            row = reminders.set_status(rid, "dismissed")
        if row is None or row["status"] != "dismissed":
            return _r(False, "その件が見つからなかったよ")
        return _r(True, f"OK、「{row['title']}」は今日はナシね。もう催促しないよ")

    if kind == "forget":
        try:
            mid = int(action["memory_id"])
        except (KeyError, TypeError, ValueError):
            return _r(False, "どの記憶のことか分からなかったよ")
        active_ids = {m["id"] for m in memories.list_all()}
        if mid not in active_ids:
            return _r(False, "その記憶は見つからなかったよ")
        m = memories.get(mid)
        memories.deactivate(mid, reason="user")
        return _r(True, f"忘れたよ:「{m['text']}」")

    if kind in STANDARD_INTENTS:
        payload = dict(action)
        payload["intent"] = kind
        return await intents.execute(payload, via=via)

    reply = action.get("reply") or "ごめん、よくわからなかった。言い方を変えてみて?"
    return _r(True, reply)


def _r(ok: bool, reply: str) -> dict:
    return {"ok": ok, "reply": reply, "photo": None}
