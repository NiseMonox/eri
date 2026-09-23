"""对话层:自由文本的统一大脑。Telegram、Siri(/api/ingest/text)和语音入口(services/voice.py)都走 handle()。
每句话都交给 LLM(function calling):像聊天一样回复,同时按意图自动调用工具——一句话里几件事就调几个。
工具 = 下面 _execute 的既有动作(id 验明正身、时间校验都在那里),LLM 看到执行结果再用艾莉口吻回复。
LLM 关闭/不可用时降级到正则快路径(模板回复),核心记录照常。
记忆:system 带【核心档案】;最后一条消息带【当前状态】【近日の予定】【相关记忆】(本地向量检索)+ 本次原话。
长期记忆由每天 04:00 的整理写入(consolidate.py),这里只有「记住/忘掉/查」三个即时工具。"""

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta

from .. import clock, db, emoji, events, store
from ..bot import parser
from ..llm import base as llm
from ..llm import embed
from . import intents, memories, reminders, routines

SYSTEM = """你是「艾莉」(エリ),用户家里的 AI 助手兼健康秘书:帮用户记体重、管提醒、到点催促,也陪着聊天。

【消息结构】
- 这条系统消息末尾的【核心档案】是关于用户最重要的长期信息
- 每轮最后一条消息 = 系统注入的【当前状态】(时间、开放事项、今后的提醒、ルーティン、身体数据、スピーカー=家里音箱正在放什么)+【近日の予定】【相关记忆】(从长期记忆库按时间/按这句话取出,带日期,旧的可能已过时)+【ユーザーの発言】(用户这次真正说的话)
- 历史消息里用户话前的 [MM/DD HH:MM] 是发送时间,艾莉话前的〔実行済み: …〕是系统记下的已执行操作;你的回复里不要写这两种标注

【说话方式】
- 回复一律日语,像朋友一样轻松(だよ/ね口吻),一般 1-3 句,不说教
- 不用 emoji(表情符号,以及闪光、音符、星星之类的装饰符号),语气靠措辞表达
- 用户说中文或日语都要理解,但别把中文词原样混进日语回复(如「健身」→「ジム/運動」)
- 纯文本:Telegram 不渲染 Markdown,不要用 **粗体**、# 标题;要列举就用「・」开头的短行
- 只依据上面这些信息和工具返回的结果说话:不编数据,没执行成功的事不能说做了;记忆和对话记录冲突时以对话记录为准

【工具】
- 话里有能用工具办的事(记体重、建提醒、设重复提醒、推迟/改期、完成、取消、记住、忘掉、查记忆、看今日待办、体重曲线、周报、停闹钟/放白噪音/调音量)就直接调用,不用先征求同意;一句话里有几件事就调几个
- 纯聊天、问身体数据(用【身体数据】的数字答)、问「你记得我什么」(用【核心档案】【相关记忆】答)时不调工具,直接回复
- reminder_id 只能取自【开放事项】或【今后的提醒】,routine_id 只能取自【ルーティン】,memory_id 只能取自【核心档案】【近日の予定】【相关记忆】或 search_memory 的结果;对不上号时别猜,问清楚
- 时间一律东京时间 yyyy-MM-dd HH:mm,按当前时间换算:「下午」=14:00、「晚上」=20:00、「待会/过会」=+1 小时;只说了钟点而今天这个钟点已过,就当明天
- 一次性的事用 create_reminder;重复的事(每天吃药、隔一天做拉伸、每周一倒垃圾)用 set_recurring——【ルーティン】里已有的传它的 routine_id 修改,别重复新建;没说几点提醒就先问
- 用户说做完了:【开放事项】里有对应项用 mark_done,没有(比如提醒之前就做了)用 record_routine_done;说的是过去的事(「昨晚其实做了」)就把实际时刻填进 done_at
- 报了体重数字就只调 record_weight:量体重的 routine 会自动完成,不用再对它 mark_done / record_routine_done
- 用户明确说「记住…」才用 remember(日常对话不用,每天夜里会自动整理进记忆);问到过去的事而上面找不到时才用 search_memory(query 用日语;问某一天就给日期)
- 家里音箱用 control_audio:停掉正在响的闹钟/白噪音、放白噪音、调正在放的声音的音量;「小声点/大声点」= 在【スピーカー】的当前音量上减/加 10-20;没在放东西时说的音量指艾莉自己说话的声音(set_voice_volume)
- 工具返回 ok=false 时照实说明原因,需要的话问清楚"""

# 语音入口和 Siri 的消息末尾加这段(放在最后一条消息里,不影响前缀缓存)
VOICE_NOTE = ("\n\n这条消息来自语音(语音识别转写,可能有同音错字或中日混杂),回复会被朗读:"
              "按最合理的意思理解,拿不准的数字和时间简短确认一下;不要用列表和符号,说得口语一点。")
RE_ACTION_TAG = re.compile(r"〔実行済み[^〕]*〕\s*")   # 模型偶尔学着历史写标注,回复里删掉
FALLBACK_REPLY = "ごめん、いま頭がうまく回らないみたい。「62.5」で体重記録、「薬飲んだ」で服薬確認はできるよ"
FALLBACK_REPLY_VOICE = "ごめん、いま頭がうまく回らないみたい。体重の数字と「薬飲んだ」なら、今でも記録できるよ"
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
    _tool("forget_memory", "忘掉一条长期记忆(「那个不用记了」);id 取自【核心档案】【近日の予定】【相关记忆】或检索结果",
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
           "category": {"type": "string", "enum": list(routines.CATEGORIES),
                        "description": "新建时的分类;量体重用 weight(记了体重自动完成,当天称过就不提醒)"},
           "detail": {"type": "string", "description": "补充说明,如「20分」「10mg」"}},
          ["times"]),
    _tool("stop_recurring", "停掉某个 routine 的重复提醒(「不用再提醒我吃维生素了」)",
          {"routine_id": _ROUTINE}, ["routine_id"]),
    _tool("record_routine_done",
          "记录某个 routine 做完了——【开放事项】里没有对应项时用(比如提醒之前就做了);以完成为准的间隔从这次重新算",
          {"routine_id": _ROUTINE, "done_at": _PAST}, ["routine_id"]),
    _tool("remember", "用户明确要求「记住…」时立刻存进长期记忆(平常的对话不用,每天夜里会自动整理)",
          {"text": {"type": "string", "description": "日语、第三人称一句;有日期写绝对日期(如「9/24 は傘を持っていく」)"},
           "kind": {"type": "string", "enum": ["fact", "preference", "schedule", "event", "mood"],
                    "description": "fact 事实 / preference 偏好习惯 / schedule 将来的安排 / event 发生过的事 / mood 近况"},
           "event_at": {"type": ["string", "null"], "description": "schedule/event 的时间 yyyy-MM-dd[ HH:mm]"},
           "core": {"type": "boolean", "description": "是否放进核心档案(长期重要的:目标、过敏、固定习惯)"}},
          ["text"]),
    _tool("search_memory",
          "查长期记忆库里过去的事——只在【核心档案】【相关记忆】和对话记录里都找不到答案时用"
          "(「上周三我说了什么」「你还记得我去年…吗」)",
          {"query": {"type": ["string", "null"], "description": "用日语写的检索语句;只按日期查时为 null"},
           "date_from": {"type": ["string", "null"], "description": "yyyy-MM-dd"},
           "date_to": {"type": ["string", "null"], "description": "yyyy-MM-dd"}}),
    _tool("control_audio",
          "操作家里的音箱:stop=停掉正在响的闹钟/白噪音;play_noise=放白噪音;set_volume=调正在放的声音的音量;"
          "set_voice_volume=调艾莉自己说话的音量。只管现在的播放,改不了闹钟的定时",
          {"op": {"type": "string", "enum": ["stop", "play_noise", "set_volume", "set_voice_volume"]},
           "preset": {"type": "string", "enum": ["brown", "pink"], "description": "play_noise 的音色,默认 brown"},
           "volume": {"type": ["integer", "null"], "description": "0-100;play_noise 不说就 null(=40)"},
           "duration_min": {"type": ["integer", "null"],
                            "description": "play_noise 放多少分钟;不说就 null(=60)"}},
          ["op"]),
]
# 工具名 → _execute 的动作名
TOOL_ACTIONS = {"record_weight": "weight", "create_reminder": "reminder", "snooze": "snooze",
                "mark_done": "done", "dismiss": "dismiss", "forget_memory": "forget",
                "show_today": "today", "weight_chart": "chart", "weekly_report": "report",
                "set_recurring": "recurring", "stop_recurring": "stop_recurring",
                "record_routine_done": "routine_record", "remember": "remember", "search_memory": "search_memory",
                "control_audio": "audio"}
# 这些工具的结果不写进 chat_log.actions(不是需要记住的事实),只记工具名。
# 音箱操作是一时的,也不该被夜间整理当成「用户的事」记进长期记忆
READ_ONLY_TOOLS = {"show_today", "weight_chart", "weekly_report", "search_memory", "today", "chart", "report",
                   "control_audio", "audio_stop"}

STANDARD_INTENTS = {"weight", "reminder", "today", "chart", "report"}


@dataclass
class TurnCtx:
    """一轮对话内工具共享的状态:本轮 LLM 看得到的记忆 id(forget 只能动这些)、执行过的操作(写进 chat_log.actions)。
    语音轮:voice=这句话是说出来的(工具不再各自播报确认,只念最后的回复);reply_spoken=回复会在家里音箱念;
    briefing=停了闹钟、早安播报会代替回复念出来;after_speech=回复念完再开始放的音频(白噪音)。"""
    visible: set = field(default_factory=set)
    actions: list = field(default_factory=list)
    remembered: int = 0
    voice: bool = False
    reply_spoken: bool = False
    briefing: bool = False
    after_speech: list = field(default_factory=list)


def _log(via: str, role: str, text: str, *, ts: str | None = None, actions: list | None = None) -> None:
    """chat_log 是长期记忆的唯一来源:原文最多留 4000 字;assistant 行带上本轮执行过的操作。"""
    conn = db.get_db()
    conn.execute("INSERT INTO chat_log (ts, via, role, text, actions) VALUES (?,?,?,?,?)",
                 (ts or clock.now_iso(), via, role, text[:4000],
                  json.dumps(actions, ensure_ascii=False) if actions else None))
    conn.commit()


def _est_tokens(s: str) -> int:
    """粗估:CJK 每字≈1 token,其他按 4 字符≈1 token。"""
    cjk = sum(1 for ch in s if "　" <= ch <= "鿿" or "＀" <= ch <= "￯")
    return cjk + max(0, len(s) - cjk) // 4 + 1


def _recent_chat() -> list[dict]:
    """对话原文窗口:昨天习惯日起点(04:00)以后的 ∪ 还没整理进记忆的(开着记忆时);超预算从最旧的丢。
    起点一天只前移一次:一天之内历史只往后追加,前缀稳定、能命中 DeepSeek 缓存。"""
    anchor = clock.iso(routines.habit_day_start(routines.habit_day(clock.now_local()) - timedelta(days=1)))
    budget = int(store.get("chat.context_budget_tokens", 3000) or 3000)
    conn = db.get_db()
    if store.get("memory.enabled", True):
        rows = conn.execute("SELECT * FROM chat_log WHERE id > ? OR ts >= ? ORDER BY id DESC LIMIT 400",
                            (int(store.get("memory.watermark", 0) or 0), anchor)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM chat_log WHERE ts >= ? ORDER BY id DESC LIMIT 400",
                            (anchor,)).fetchall()
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
            lines.append(f"- id={r['id']} [{tag}]「{r['title']}」 {memories.date_label(r['due_at'], True)} [{state}]")

    upcoming = reminders.upcoming()
    if upcoming:
        lines.append("【今后的提醒】")
        for r in upcoming:
            lines.append(f"- id={r['id']}「{r['title']}」 {memories.date_label(r['due_at'], True)}")

    rts = routines.list_all()
    if rts:
        lines.append("【ルーティン】")
        for rt in rts:
            nxt = routines.next_fire(rt["id"])
            detail = f"({rt['detail']})" if rt.get("detail") else ""
            lines.append(f"- routine_id={rt['id']}「{rt['name']}」{detail}:{routines.describe(rt['id'])};"
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

    lines.append(_speaker_line())
    return "\n".join(lines)


def _speaker_line() -> str:
    """家里音箱现在的状态,LLM 靠它处理「停下」「小声点」。不暴露文件路径。"""
    from ..audio.manager import audio_manager

    st = audio_manager.status()
    voice_vol = f"エリの声 {int(store.get('tts.volume', 80) or 80)}"
    if st.get("action") == "announce":
        paused = f"({st['paused_label']}は一時停止中、音量{st['paused_volume']})" if st.get("paused") else ""
        return f"【スピーカー】エリが話し中{paused} / {voice_vol}"
    if not st.get("playing"):
        return f"【スピーカー】何も再生していない / {voice_vol}"
    parts = [f"{st.get('label') or st.get('action')}再生中", f"音量{st.get('target_volume')}"]
    if st.get("ends_at"):
        left = (clock.parse_iso(st["ends_at"]) - clock.now_utc()).total_seconds() / 60
        parts.append(f"あと{max(1, round(left))}分で自動停止")
    return "【スピーカー】" + "・".join(parts) + f" / {voice_vol}"


def _history() -> tuple[list[dict], int | None]:
    """原文窗口转成真正的多轮消息,返回 (messages, 窗口里最早的 chat_log.id)。
    用户话前缀发送时间(模型才分得清新旧),艾莉话前缀〔実行済み: …〕(知道自己当时做了什么)。"""
    rows = _recent_chat()
    msgs = []
    for c in rows:
        if c["role"] == "user":
            msgs.append({"role": "user", "content": f"[{clock.fmt_local(c['ts'], '%m/%d %H:%M')}] {c['text']}"})
        else:
            note = memories.actions_note(c.get("actions"))
            msgs.append({"role": "assistant", "content": f"{note}\n{c['text']}" if note else c["text"]})
    while msgs and msgs[0]["role"] == "assistant":     # 预算截断后别让历史以艾莉的话开头
        msgs.pop(0)
    return msgs, (rows[0]["id"] if rows else None)


async def _related(text: str, first_id: int | None) -> list[dict]:
    """每句话自动检索相关记忆。纯体重数字/「薬飲んだ」这类不需要回忆;不到 12 字的短句拼上 15 分钟内的
    上一句再检索(「那个呢?」)。Ollama 不可用 → 空,照常回复。"""
    if parser.parse_regex(text):
        return []
    q = text
    if len(text) < 12:
        prev = db.get_db().execute(
            "SELECT text FROM chat_log WHERE role='user' AND ts>=? ORDER BY id DESC LIMIT 1",
            (clock.iso(clock.now_utc() - timedelta(minutes=15)),)).fetchone()
        if prev:
            q = f"{prev['text']} {text}"
    vec = await embed.embed_query(q)
    rows, top5 = memories.related(vec, hist_first_id=first_id)
    if top5:     # 记下过门槛前的 top5 分数,用真实数据校准 memory.rag_min_sim
        events.log("memory_rag", {"q": q[:40], "top": top5, "picked": [r["id"] for r in rows]})
    return rows


def _turn_message(text: str, via: str, arrived: str, upcoming: str, related: list[dict],
                  voice: bool = False) -> str:
    parts = ["【当前状态】\n" + _context(), upcoming, memories.related_block(related),
             f"【ユーザーの発言】[{clock.fmt_local(arrived, '%m/%d %H:%M')}] {text}"]
    return "\n\n".join(p for p in parts if p) + (VOICE_NOTE if voice or via == "siri" else "")


async def handle(text: str, via: str, *, voice: bool = False, reply_spoken: bool = False) -> dict:
    """返回 {"ok", "reply", "photo", "briefing", "after_speech"}。
    voice=语音入口(识别出来的话;工具不各自播报确认),reply_spoken=调用方会在家里音箱念 reply。"""
    arrived = clock.now_iso()
    text = text.strip()
    ctx = TurnCtx(voice=voice, reply_spoken=reply_spoken)
    res = await _agent(text, via, arrived, ctx)
    if res is None:
        # LLM 关闭/不可用:能被正则认出的(体重/服药/今日/图表/周报/停下)照样执行,核心记录不断
        fast = parser.parse_regex(text)
        res = (await _execute(fast, text, via, ctx) if fast
               else _r(False, FALLBACK_REPLY_VOICE if voice else FALLBACK_REPLY))
        if fast:
            _note_action(ctx, fast.get("intent", "?"), res)
    res["reply"] = RE_ACTION_TAG.sub("", res["reply"]).strip() or res["reply"]
    # 提示词管不住模型偶尔加 emoji:所有渠道都在这里兜底删掉(Siri 还会把表情念成「ほっとした顔」)
    res["reply"] = emoji.strip(res["reply"]) or "了解だよ"
    _log(via, "user", text, ts=arrived)
    _log(via, "assistant", res["reply"], actions=ctx.actions)
    res["briefing"] = ctx.briefing
    res["after_speech"] = list(ctx.after_speech)
    return res


async def _agent(text: str, via: str, arrived: str, ctx: TurnCtx) -> dict | None:
    """工具调用循环。首轮 LLM 就失败 → None(交给正则兜底);
    执行过工具后 LLM 才失败 → 用工具的模板回复收尾(不能再走正则,会重复执行)。
    消息顺序按「越靠前越稳定」排:人设+核心档案 → 原文历史 → 本轮状态/记忆/原话(命中前缀缓存)。"""
    history, first_id = _history()
    core_text, core_ids = memories.core_block()
    upcoming, up_ids = memories.upcoming_block()
    related = await _related(text, first_id)
    ctx.visible |= core_ids | up_ids | {r["id"] for r in related}
    messages = [{"role": "system", "content": SYSTEM + (f"\n\n{core_text}" if core_text else "")},
                *history,
                {"role": "user", "content": _turn_message(text, via, arrived, upcoming, related, ctx.voice)}]
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
            res = await _run_tool(call, text, via, ctx)
            done.append(res)
            messages.append({"role": "tool", "tool_call_id": call.get("id"),
                             "content": json.dumps({"ok": res["ok"], "result": res["reply"]},
                                                   ensure_ascii=False)})
    return _result(done)      # 轮数用尽


async def _run_tool(call: dict, user_text: str, via: str, ctx: TurnCtx) -> dict:
    fn = call.get("function") or {}
    kind = TOOL_ACTIONS.get(fn.get("name"))
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except (json.JSONDecodeError, TypeError):
        args = None
    if kind is None or not isinstance(args, dict):
        return _r(False, f"ツールの呼び出しが不正だよ({fn.get('name')})")
    # LLM 起的标题/名字/记忆原文也不留 emoji:它们会进 DB、通知、iPhone 同步和网页
    args = {k: emoji.strip(v) if isinstance(v, str) else v for k, v in args.items()}
    try:
        res = await _execute({**args, "action": kind}, user_text, via, ctx)
    except Exception as e:  # noqa: BLE001  一个工具出错不拖垮整轮对话,错误回喂给 LLM
        events.log("tool_error", {"tool": fn.get("name"), "error": str(e)[:200]})
        res = _r(False, "ごめん、その処理でエラーが出ちゃった")
    _note_action(ctx, fn.get("name"), res)
    return res


def _note_action(ctx: TurnCtx, tool: str, res: dict) -> None:
    ctx.actions.append({"tool": tool, "ok": res["ok"],
                        "result": None if tool in READ_ONLY_TOOLS else res["reply"][:200]})


def _result(done: list[dict], reply: str | None = None) -> dict:
    """reply 缺省(LLM 中途失败/轮数用尽)时拼各工具的模板回复;photo 取第一张(体重曲线)。"""
    return {"ok": all(d["ok"] for d in done),
            "reply": reply or "\n".join(d["reply"] for d in done),
            "photo": next((d["photo"] for d in done if d.get("photo") is not None), None)}


async def _execute(action: dict, user_text: str, via: str, ctx: TurnCtx | None = None) -> dict:
    kind = action.get("action") or action.get("intent")

    # 正则快路径的 med_confirm / routine_done:找最近待确认实例
    if kind in ("med_confirm", "routine_done"):
        cat = "med" if kind == "med_confirm" else None
        inst = routines.latest_open(cat)
        if inst is None:
            return _r(True, "いま確認待ちのお薬はないよ" if cat == "med" else "いま確認待ちのルーティンはないよ")
        row = routines.complete(inst["id"], via=via)
        if not _voice(ctx):
            from ..audio import tts

            await tts.announce_done(row["title"])
        return _r(True, f"{row['title']}、完了だよ({clock.fmt_local(row['done_at'], '%H:%M')})")

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
        if not _voice(ctx):
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
        if not _voice(ctx):
            from ..audio import tts

            await tts.announce_done(row["title"])
        return _r(True, f"記録したよ:「{row['title']}」完了")

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
        m = memories.get(mid)
        if ctx is None or mid not in ctx.visible or m is None or not m["active"]:
            return _r(False, "その記憶は見つからなかったよ")
        memories.deactivate(mid, reason="user")
        return _r(True, f"忘れたよ:「{m['text']}」")

    if kind == "remember":
        return await _remember(action, ctx)

    if kind == "search_memory":
        return await _search_memory(action, ctx)

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
            return _r(True, f"「{routine['name']}」のリマインドを止めたよ" if n
                      else f"「{routine['name']}」はもともとリマインドしてないよ")
        try:
            row = routines.record_done(routine["id"], via=via, done_at=_past_time(action.get("done_at")))
        except ValueError as e:
            return _r(False, str(e))
        if not _voice(ctx):
            from ..audio import tts

            await tts.announce_done(row["title"])
        return _r(True, f"「{routine['name']}」完了を記録したよ" + _next_line(routine["id"]))

    if kind in ("audio", "audio_stop"):     # audio_stop = 正则快路径的「止めて」
        return await _control_audio({"op": "stop"} if kind == "audio_stop" else action, ctx)

    if kind in STANDARD_INTENTS:
        payload = dict(action)
        payload["intent"] = kind
        return await intents.execute(payload, via=via)

    return _r(False, "ごめん、よくわからなかったよ")


def _r(ok: bool, reply: str) -> dict:
    return {"ok": ok, "reply": reply, "photo": None}


def _voice(ctx: TurnCtx | None) -> bool:
    """语音轮只念最后那段回复:工具自己的播报确认(「えらい!…完了だよ」)不再单独念。
    人不在家(speak=false)时也一样,家里保持安静。"""
    return ctx is not None and ctx.voice


def _clamp(v, lo: int, hi: int, default: int | None) -> int | None:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


_AUDIO_JA = {"alarm": "アラーム", "white_noise": "ホワイトノイズ", "file": "音声"}
_NOISE_JA = {"brown": "ブラウン", "pink": "ピンク"}


async def _control_audio(action: dict, ctx: TurnCtx | None) -> dict:
    """家里音箱:停 / 放白噪音 / 调播放音量 / 调艾莉的声音。播报进行中时,停和调音量作用在被暂停的那个上。"""
    from ..audio.manager import audio_manager

    op = action.get("op")
    st = audio_manager.status()
    if op == "stop":
        if st.get("action") == "announce":
            was, label = st.get("paused"), st.get("paused_label")
        else:
            was, label = (st.get("action"), st.get("label")) if st.get("playing") else (None, None)
        if not was:
            return _r(True, "いまは何も鳴ってないよ")
        # 和 Bark 的停止链接、网页按钮一样用 manual:停闹钟会触发早安播报(时刻+今日待办)。
        # 例外是语音轮而家里不出声(speak=false,人不在家):那就安静地停
        quiet = ctx is not None and ctx.voice and not ctx.reply_spoken
        await audio_manager.stop(reason="remote" if quiet else "manual")
        if was == "alarm" and not quiet:
            if ctx is not None:
                ctx.briefing = True
            return _r(True, "アラームを止めたよ。今の時刻と今日の予定はスピーカーで読み上げるね")
        return _r(True, f"{label or _AUDIO_JA.get(was, '音')}を止めたよ")

    if op == "play_noise":
        preset = action.get("preset") if action.get("preset") in _NOISE_JA else "brown"
        vol = _clamp(action.get("volume"), 0, 100, 40)
        minutes = _clamp(action.get("duration_min"), 1, 480, 60)
        payload = {"action": "white_noise", "preset": preset, "volume": vol, "duration_min": minutes}
        name = f"ホワイトノイズ({_NOISE_JA[preset]})"
        if ctx is not None and ctx.reply_spoken:
            # 回复要在音箱上念:先停掉现在的,念完再开始放,免得「响起来—被回复打断—再响」
            await audio_manager.stop(reason="replaced")
            ctx.after_speech.append(payload)
            return _r(True, f"返事のあとで{name}を{minutes}分、音量{vol}で流すね")
        await audio_manager.start(payload)
        return _r(True, f"{name}を{minutes}分、音量{vol}で流し始めたよ")

    if op == "set_volume":
        vol = _clamp(action.get("volume"), 0, 100, None)
        if vol is None:
            return _r(False, "音量をいくつにするか分からなかったよ")
        if await audio_manager.set_volume(vol) is None:
            return _r(False, "いまは何も流れてないよ。わたしの声の大きさを変えたいなら、そう言ってね")
        return _r(True, f"音量を{vol}にしたよ")

    if op == "set_voice_volume":
        vol = _clamp(action.get("volume"), 10, 100, None)
        if vol is None:
            return _r(False, "声の大きさをいくつにするか分からなかったよ")
        store.set("tts.volume", vol)
        return _r(True, f"わたしの声の大きさを{vol}にしたよ")

    return _r(False, "音の操作が分からなかったよ")


async def _remember(action: dict, ctx: TurnCtx | None) -> dict:
    """「记住…」:立即写入(夜间整理之外唯一的写入口)。查重、拒相对日期、核心档案满了降为普通条目。"""
    text = str(action.get("text") or "").strip()
    if not text:
        return _r(False, "何を覚えればいいか分からなかったよ")
    if memories.RE_RELATIVE.search(text):
        return _r(False, "「明日」みたいな言い方は後で分からなくなるから、日付で書き直してね(例: 9/24)")
    if ctx is not None and ctx.remembered >= 3:
        return _r(False, "一度に覚えるのは3つまでにしてね")
    kind = action.get("kind") if action.get("kind") in ("fact", "preference", "schedule", "event", "mood") else "fact"
    try:
        event_at = clock.parse_flexible_jst(str(action["event_at"])) if action.get("event_at") else None
    except ValueError:
        return _r(False, "日時が読み取れなかったよ")
    if kind == "schedule" and event_at is None:
        return _r(False, "予定は日時もいっしょに教えてね")
    vec = await embed.embed_query(text)
    dup = memories.find_duplicate(text, vec)
    if dup:
        if ctx is not None:
            ctx.visible.add(dup["id"])
        return _r(True, f"もう覚えてるよ(#{dup['id']}「{dup['text']}」)")
    core = bool(action.get("core")) and kind in memories.CORE_KINDS
    note = ""
    if core and memories.core_usage()["count"] >= int(store.get("memory.core_max", 20) or 20):
        core, note = False, "(コアメモが満杯だから普通の記憶として保存したよ。/memories で整理してね)"
    row = memories.add(kind, text, event_at=event_at, core=core, source="user", vec=vec)
    if ctx is not None:
        ctx.visible.add(row["id"])
        ctx.remembered += 1
    return _r(True, f"覚えたよ(#{row['id']}){note}")


async def _search_memory(action: dict, ctx: TurnCtx | None) -> dict:
    q = str(action.get("query") or "").strip() or None
    days: list[str | None] = []
    for k in ("date_from", "date_to"):
        try:
            days.append(memories.local_day(clock.parse_flexible_jst(str(action[k]))) if action.get(k) else None)
        except ValueError:
            return _r(False, "日付が読み取れなかったよ")
    if not q and not any(days):
        return _r(False, "何を探せばいいか分からなかったよ")
    hits = memories.search(q, await embed.embed_query(q) if q else None, days[0], days[1])
    if ctx is not None:
        ctx.visible |= {h["id"] for h in hits if h.get("id")}
    return _r(True, "\n".join(h["line"] for h in hits) if hits else "記憶の中には見つからなかったよ")


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
    return _r(True, f"「{routine['name']}」を {routines.describe(routine['id'])} で設定したよ"
                    + _next_line(routine["id"]))


def _next_line(routine_id: int) -> str:
    t = routines.next_fire(routine_id)
    if t is None:
        return ""
    wd = routines.WEEKDAY_JA[int(t.strftime("%w"))]
    return f"。次は {t.strftime('%m/%d')}({wd}) {t.strftime('%H:%M')} に声かけるね"
