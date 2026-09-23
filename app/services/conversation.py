"""对话层:自由文本的统一大脑。Telegram 与 Siri(/api/ingest/text)都走 handle()。
每句话都交给 LLM(function calling):像聊天一样回复,同时按意图自动调用工具——一句话里几件事就调几个。
工具 = 下面 _execute 的既有动作(id 验明正身、时间校验都在那里),LLM 看到执行结果再用艾莉口吻回复。
LLM 关闭/不可用时降级到正则快路径(模板回复),核心记录照常。每轮结束后异步跑记忆维护(不阻塞回复)。"""

import asyncio
import json
import re
from datetime import timedelta

from .. import clock, db, events, store
from ..bot import parser
from ..llm import base as llm
from . import intents, memories, reminders, routines

SYSTEM = """你是「艾莉」(エリ),用户家里的 AI 助手兼健康秘书:帮用户记体重、管提醒、到点催促,也陪着聊天。

【说话方式】
- 回复一律日语,像朋友一样轻松(だよ/ね口吻),一般 1-3 句,不说教
- 用户说中文或日语都要理解,但别把中文词原样混进日语回复(如「健身」→「ジム/運動」)
- 只依据【上下文】和工具返回的结果说话:不编数据,没执行成功的事不能说做了

【工具】
- 话里有能用工具办的事(记体重、建提醒、设重复提醒、推迟/改期、完成、取消、忘掉记忆、看今日待办、体重曲线、周报)就直接调用,不用先征求同意;一句话里有几件事就调几个
- 纯聊天、问身体数据(用【身体数据】的数字答)、问「你记得我什么」(用【长期记忆】答)时不调工具,直接回复
- reminder_id 只能取自【开放事项】或【今后的提醒】,routine_id 只能取自【ルーティン】,memory_id 只能取自【长期记忆】;对不上号时别猜,问清楚
- 时间一律东京时间 yyyy-MM-dd HH:mm,按【当前时间】换算:「下午」=14:00、「晚上」=20:00、「待会/过会」=+1 小时;只说了钟点而今天这个钟点已过,就当明天
- 一次性的事用 create_reminder;重复的事(每天吃药、隔一天做拉伸、每周一倒垃圾)用 set_recurring——【ルーティン】里已有的传它的 routine_id 修改,别重复新建;没说几点提醒就先问
- 用户说做完了:【开放事项】里有对应项用 mark_done,没有(比如提醒之前就做了)用 record_routine_done;说的是过去的事(「昨晚其实做了」)就把实际时刻填进 done_at
- 工具返回 ok=false 时照实说明原因,需要的话问清楚
- 历史消息里用户话前面的 [MM/DD HH:MM] 是发送时间;你的回复不要带这种时间戳"""

SIRI_NOTE = "\n\n这条消息来自 Siri 语音,回复会被朗读:不要用 emoji、列表和符号,说得口语一点。"
# 提示词管不住模型加 emoji,Siri 回复在代码里兜底删掉(快捷指令会把 😌 念成「ほっとした顔」)
RE_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE0F\U0000200D]")
FALLBACK_REPLY = "ごめん、いま頭がうまく回らないみたい。「62.5」で体重記録、「薬飲んだ」で服薬確認はできるよ"
MAX_ROUNDS = 4          # 一句话最多几轮「调工具 → 看结果」


def _tool(name: str, desc: str, props: dict | None = None, required: list | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}


_WHEN = {"type": "string", "description": "东京时间 yyyy-MM-dd HH:mm,必须是将来"}
_RID = {"type": "integer", "description": "【开放事项】或【今后的提醒】里的 id"}
_ROUTINE = {"type": "integer", "description": "【ルーティン】里的 routine_id"}
_PAST = {"type": ["string", "null"],
         "description": "实际做完的时刻 yyyy-MM-dd HH:mm(「昨晚其实做了」);就是刚才则 null"}
TOOLS = [
    _tool("record_weight", "记录体重(用户报了体重数字时)",
          {"weight_kg": {"type": "number", "description": "公斤"},
           "measured_at": {"type": ["string", "null"],
                           "description": "称重时刻 yyyy-MM-dd HH:mm;没说就 null(=现在)"}},
          ["weight_kg"]),
    _tool("create_reminder", "新建一次性提醒:到点推送 + 音箱播报,没回应会追催",
          {"title": {"type": "string", "description": "简短标题"}, "due_at": _WHEN},
          ["title", "due_at"]),
    _tool("snooze", "把某项推迟/改期到指定时刻(「下午再去」「改到 4 点」)",
          {"reminder_id": _RID, "until": _WHEN}, ["reminder_id", "until"]),
    _tool("mark_done", "把【开放事项】里的某项标记为完成(「做完了」「薬飲んだ」「已经去过了」)",
          {"reminder_id": _RID, "done_at": _PAST}, ["reminder_id"]),
    _tool("dismiss", "取消某项,不再催(「今天不去了」「算了取消吧」)",
          {"reminder_id": _RID}, ["reminder_id"]),
    _tool("forget_memory", "忘掉一条长期记忆(「那个不用记了」)",
          {"memory_id": {"type": "integer", "description": "【长期记忆】里的 id"}}, ["memory_id"]),
    _tool("show_today", "取今天的待办/日程清单"),
    _tool("weight_chart", "生成体重曲线图(图片会附在回复里)",
          {"days": {"type": "integer", "description": "天数 7-365,默认 30"}}),
    _tool("weekly_report", "生成体重周报并推送给用户"),
    _tool("set_recurring",
          "设定/修改重复提醒(到点推送+播报,没回应会追催)。every_days=1 每天;「隔一天」=2、「每三天」=3,"
          "以完成为准——那天没做完第二天接着提醒,做完了才隔开;weekdays=只在每周这几天(不能和 every_days≥2 同用)。"
          "传 routine_id 会替换它原来的时间表",
          {"routine_id": {"type": ["integer", "null"], "description": "【ルーティン】里已有的 id;新建填 null"},
           "name": {"type": "string", "description": "新建时:要做的事,简短"},
           "times": {"type": "array", "items": {"type": "string"}, "description": "提醒时刻 HH:MM,可多个"},
           "every_days": {"type": "integer", "description": "每 N 天,默认 1"},
           "weekdays": {"type": "array", "items": {"type": "integer"}, "description": "1=周一 … 7=周日"},
           "category": {"type": "string", "enum": list(routines.CATEGORIES), "description": "新建时的分类"},
           "detail": {"type": "string", "description": "补充说明,如「20分」「10mg」"}},
          ["times"]),
    _tool("stop_recurring", "停掉某个 routine 的重复提醒(「不用再提醒我吃维生素了」)",
          {"routine_id": _ROUTINE}, ["routine_id"]),
    _tool("record_routine_done",
          "记录某个 routine 做完了——【开放事项】里没有对应项时用(比如提醒之前就做了);以完成为准的间隔从这次重新算",
          {"routine_id": _ROUTINE, "done_at": _PAST}, ["routine_id"]),
]
# 工具名 → _execute 的动作名
TOOL_ACTIONS = {"record_weight": "weight", "create_reminder": "reminder", "snooze": "snooze",
                "mark_done": "done", "dismiss": "dismiss", "forget_memory": "forget",
                "show_today": "today", "weight_chart": "chart", "weekly_report": "report",
                "set_recurring": "recurring", "stop_recurring": "stop_recurring",
                "record_routine_done": "routine_record"}

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

    upcoming = reminders.upcoming()
    if upcoming:
        lines.append("【今后的提醒】")
        for r in upcoming:
            lines.append(f"- id={r['id']}「{r['title']}」 {clock.fmt_local(r['due_at'])}")

    rts = routines.list_all()
    if rts:
        lines.append("【ルーティン】")
        for rt in rts:
            nxt = routines.next_fire(rt["id"])
            detail = f"({rt['detail']})" if rt.get("detail") else ""
            lines.append(f"- routine_id={rt['id']} {rt['icon']}「{rt['name']}」{detail}:{routines.describe(rt['id'])};"
                         f"上次完成 {clock.fmt_local(routines.last_done_at(rt['id']), '%m/%d %H:%M')};"
                         f"下次提醒 {nxt.strftime('%m/%d %H:%M') if nxt else '—'}")

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

    return "\n".join(lines)


def _history() -> list[dict]:
    """近 3 天对话(预算内)转成真正的多轮消息;用户话前缀发送时间,模型才分得清新旧。"""
    msgs = [{"role": "user", "content": f"[{clock.fmt_local(c['ts'], '%m/%d %H:%M')}] {c['text']}"}
            if c["role"] == "user" else {"role": "assistant", "content": c["text"]}
            for c in _recent_chat()]
    while msgs and msgs[0]["role"] == "assistant":     # 预算截断后别让历史以艾莉的话开头
        msgs.pop(0)
    return msgs


async def handle(text: str, via: str) -> dict:
    """返回 {"ok", "reply", "photo"}。"""
    text = text.strip()
    res = await _agent(text, via)
    used_llm = res is not None
    if res is None:
        # LLM 关闭/不可用:能被正则认出的(体重/服药/今日/图表/周报)照样执行,核心记录不断
        fast = parser.parse_regex(text)
        res = await _execute(fast, text, via) if fast else _r(False, FALLBACK_REPLY)
    if via == "siri":
        res["reply"] = RE_EMOJI.sub("", res["reply"]).strip()
    _log(via, "user", text)
    _log(via, "assistant", res["reply"])
    if used_llm and store.get("memory.enabled", True):
        # 持引用防 GC
        _bg.add(t := asyncio.create_task(_maintain_safe(text, res["reply"])))
        t.add_done_callback(_bg.discard)
    return res


async def _agent(text: str, via: str) -> dict | None:
    """工具调用循环。首轮 LLM 就失败 → None(交给正则兜底);
    执行过工具后 LLM 才失败 → 用工具的模板回复收尾(不能再走正则,会重复执行)。"""
    system = SYSTEM + "\n\n【上下文】\n" + _context() + (SIRI_NOTE if via == "siri" else "")
    messages = [{"role": "system", "content": system}, *_history(), {"role": "user", "content": text}]
    done: list[dict] = []
    for _ in range(MAX_ROUNDS):
        msg = await llm.chat(messages, tools=TOOLS, timeout=30)
        if msg is None:
            return _result(done) if done else None
        calls = msg.get("tool_calls") or []
        if not calls:
            reply = (msg.get("content") or "").strip()
            if reply:
                return _result(done, reply)
            return _result(done) if done else None
        turn = {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
        if msg.get("reasoning_content"):
            turn["reasoning_content"] = msg["reasoning_content"]   # DeepSeek 思考模式:同一轮内回传推理
        messages.append(turn)
        for call in calls:
            res = await _run_tool(call, text, via)
            done.append(res)
            messages.append({"role": "tool", "tool_call_id": call.get("id"),
                             "content": json.dumps({"ok": res["ok"], "result": res["reply"]},
                                                   ensure_ascii=False)})
    return _result(done)      # 轮数用尽


async def _run_tool(call: dict, user_text: str, via: str) -> dict:
    fn = call.get("function") or {}
    kind = TOOL_ACTIONS.get(fn.get("name"))
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except (json.JSONDecodeError, TypeError):
        args = None
    if kind is None or not isinstance(args, dict):
        return _r(False, f"ツールの呼び出しが不正だよ({fn.get('name')})")
    try:
        return await _execute({**args, "action": kind}, user_text, via)
    except Exception as e:  # noqa: BLE001  一个工具出错不拖垮整轮对话,错误回喂给 LLM
        events.log("tool_error", {"tool": fn.get("name"), "error": str(e)[:200]})
        return _r(False, "ごめん、その処理でエラーが出ちゃった")


def _result(done: list[dict], reply: str | None = None) -> dict:
    """reply 缺省(LLM 中途失败/轮数用尽)时拼各工具的模板回复;photo 取第一张(体重曲线)。"""
    return {"ok": all(d["ok"] for d in done),
            "reply": reply or "\n".join(d["reply"] for d in done),
            "photo": next((d["photo"] for d in done if d.get("photo") is not None), None)}


_bg: set = set()


async def _maintain_safe(user_text: str, reply: str) -> None:
    try:
        await memories.maintain_after_turn(user_text, reply)
    except Exception as e:  # noqa: BLE001
        events.log("memory_error", {"error": str(e)[:200]})


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

    # LLM 给的 id 一律先对【开放事项】/【今后的提醒】/【长期记忆】验明正身
    if kind in ("snooze", "done", "dismiss"):
        known = {r["id"] for r in reminders.open_items() + reminders.upcoming()}
        try:
            rid = int(action["reminder_id"])
        except (KeyError, TypeError, ValueError):
            return _r(False, "どの件のことか分からなかったよ。もう少し具体的に言ってくれる?")
        if rid not in known:
            return _r(False, "該当する予定が見つからなかったよ。どの件のこと?")

    if kind == "snooze":
        try:
            until = clock.parse_flexible_jst(str(action["until"]))
        except (KeyError, TypeError, ValueError):
            return _r(False, "いつに延ばすか聞き取れなかったよ。「午後2時にまた」みたいに言ってね")
        if clock.parse_iso(until) <= clock.now_utc():
            return _r(False, f"{clock.fmt_local(until)} はもう過ぎてるよ。いつにする?")
        row = reminders.snooze(rid, until)
        if row is None:
            return _r(False, "延ばす対象が見つからなかったよ")
        t_local = clock.to_local(clock.parse_iso(row["due_at"]))
        when = t_local.strftime("%H:%M" if t_local.date() == clock.now_local().date() else "%m/%d %H:%M")
        from ..audio import tts

        await tts.announce_snoozed(row["title"], t_local)
        return _r(True, f"OK、「{row['title']}」は {when} にまた声かけるね")

    if kind == "done":
        try:
            at = _past_time(action.get("done_at"))
        except ValueError as e:
            return _r(False, str(e))
        row = reminders.get(rid)
        if row and row.get("kind") == "routine":
            row = routines.complete(rid, via=via, done_at=at)
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
        return _r(True, f"OK、「{row['title']}」はナシにしたよ。もう催促しないよ")

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

    if kind == "recurring":
        return _set_recurring(action)

    if kind in ("stop_recurring", "routine_record"):
        try:
            routine = routines.get(int(action["routine_id"]))
        except (KeyError, TypeError, ValueError):
            routine = None
        if routine is None or not routine["active"]:
            return _r(False, "どのルーティンのことか分からなかったよ")
        if kind == "stop_recurring":
            n = routines.stop_schedules(routine["id"], via=via)
            return _r(True, f"🔕 「{routine['name']}」のリマインドを止めたよ" if n
                      else f"「{routine['name']}」はもともとリマインドしてないよ")
        try:
            row = routines.record_done(routine["id"], via=via, done_at=_past_time(action.get("done_at")))
        except ValueError as e:
            return _r(False, str(e))
        from ..audio import tts

        await tts.announce_done(row["title"])
        return _r(True, f"✅ 「{routine['name']}」完了を記録したよ" + _next_line(routine["id"]))

    if kind in STANDARD_INTENTS:
        payload = dict(action)
        payload["intent"] = kind
        return await intents.execute(payload, via=via)

    return _r(False, "ごめん、よくわからなかったよ")


def _r(ok: bool, reply: str) -> dict:
    return {"ok": ok, "reply": reply, "photo": None}


def _past_time(v) -> str | None:
    """「昨晚其实做了」的实际完成时刻 → UTC ISO;空=现在。读不懂或在将来 → ValueError(日语说明)。"""
    if not v:
        return None
    try:
        at = clock.parse_flexible_jst(str(v))
    except ValueError:
        raise ValueError("いつやったのか読み取れなかったよ") from None
    if clock.parse_iso(at) > clock.now_utc() + timedelta(minutes=5):
        raise ValueError("その時刻はまだ来てないよ")
    return at


def _set_recurring(action: dict) -> dict:
    times, every, weekdays = action.get("times") or [], action.get("every_days") or 1, action.get("weekdays")
    try:
        routines.build_crons(times, every, weekdays)   # 先校验再建 routine:参数不对别留下没提醒的空 routine
        if action.get("routine_id") is not None:
            routine = routines.get(int(action["routine_id"]))
            if routine is None:
                return _r(False, "そのルーティンは見つからなかったよ")
        else:
            name = str(action.get("name") or "").strip()
            if not name:
                return _r(False, "何をリマインドするのか分からなかったよ")
            routine = routines.find_by_name(name) or routines.create(
                name, str(action.get("category") or "other"), str(action.get("detail") or ""))
        changes = {"active": 1} | ({"detail": str(action["detail"])} if action.get("detail") else {})
        routine = routines.update(routine["id"], **changes)
        routines.set_schedule(routine["id"], times, every, weekdays)
    except (TypeError, ValueError) as e:
        return _r(False, f"設定できなかったよ:{e}")
    return _r(True, f"🔁 「{routine['name']}」を {routines.describe(routine['id'])} で設定したよ"
                    + _next_line(routine["id"]))


def _next_line(routine_id: int) -> str:
    t = routines.next_fire(routine_id)
    if t is None:
        return ""
    wd = routines.WEEKDAY_JA[int(t.strftime("%w"))]
    return f"。次は {t.strftime('%m/%d')}({wd}) {t.strftime('%H:%M')} に声かけるね"
