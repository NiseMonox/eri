"""长期记忆:艾莉自主增减的持久事实。
kind: schedule(带日期的安排,到期/取消自动失效)| preference(偏好习惯)| fact(背景事实)| mood(近况,默认 7 天淡出)。
维护 = 每轮对话后异步提取(maintain_after_turn)+ 每天 04:00 全量清理(nightly_cleanup)。"""

import json
import re
from datetime import timedelta

from .. import clock, db, events
from ..llm import base as llm

KINDS = ("schedule", "preference", "fact", "mood")
KIND_JA = {"schedule": "予定", "preference": "好み・習慣", "fact": "事実", "mood": "近況"}
MOOD_TTL_DAYS = 7
MAX_INJECT = 30

MAINTAIN_SYSTEM = """你是家庭 AI 助手「艾莉」的记忆管理器。根据【现有记忆】和【本轮对话】,决定记忆的增删改,只输出一个 JSON:
{"add":[{"kind":"schedule|preference|fact|mood","text":"简短日语一句","valid_until":"yyyy-MM-dd HH:mm 或 null"}],
 "update":[{"id":N,"text":"...","valid_until":"... 或 null"}],
 "remove":[N,...]}
规则:
- schedule(预定/安排):必须带 valid_until=事件时间(相对时间按【当前时间】换算);用户说取消/不去了/已经结束 → remove 对应 id
- preference(偏好/习惯,如常去的健身房、讨厌早上被催):valid_until=null;偏好变了 → update 而非重复 add
- fact(背景事实,如在准备就活、减重目标):valid_until=null
- mood(近况/情绪,如最近压力大、这周感冒):valid_until=当前时间+7天
- 只记对未来对话有用的信息;体重数字、当次提醒的确认/推迟这类系统已记录的事不要记
- 闲聊无信息量 → 三个数组全空
- text 用简短日语(用户是日语环境),第三人称描述用户(如「毎週水曜はジムに行く」)"""

CLEANUP_SYSTEM = """你是记忆管理器。对【现有记忆】做一次全量整理,只输出 JSON:
{"update":[{"id":N,"text":"...","valid_until":"... 或 null"}], "remove":[N,...]}
任务:合并重复(保留一条 update、其余 remove)、消解矛盾(以更新的为准)、删掉明显过时的安排。不确定的一律保留(不动)。"""


# --- CRUD ---

def add(kind: str, text: str, valid_until: str | None = None, source: str = "assistant") -> dict:
    if kind not in KINDS:
        kind = "fact"
    if kind == "mood" and not valid_until:
        valid_until = clock.iso(clock.now_utc() + timedelta(days=MOOD_TTL_DAYS))
    conn = db.get_db()
    now = clock.now_iso()
    cur = conn.execute(
        "INSERT INTO memories (kind, text, valid_from, valid_until, source, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (kind, text.strip()[:300], now, valid_until, source, now, now),
    )
    conn.commit()
    events.log("memory_add", {"kind": kind, "text": text[:80]}, "memory", cur.lastrowid)
    return get(cur.lastrowid)


def get(mid: int) -> dict | None:
    row = db.get_db().execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
    return dict(row) if row else None


def update(mid: int, text: str | None = None, valid_until: str | None = "__keep__") -> dict | None:
    row = get(mid)
    if row is None or not row["active"]:
        return None
    conn = db.get_db()
    new_text = text.strip()[:300] if text else row["text"]
    new_until = row["valid_until"] if valid_until == "__keep__" else valid_until
    conn.execute("UPDATE memories SET text=?, valid_until=?, updated_at=? WHERE id=?",
                 (new_text, new_until, clock.now_iso(), mid))
    conn.commit()
    events.log("memory_update", {"text": new_text[:80]}, "memory", mid)
    return get(mid)


def deactivate(mid: int, reason: str = "manual") -> bool:
    conn = db.get_db()
    cur = conn.execute("UPDATE memories SET active=0, updated_at=? WHERE id=? AND active=1",
                       (clock.now_iso(), mid))
    conn.commit()
    if cur.rowcount:
        events.log("memory_remove", {"reason": reason}, "memory", mid)
    return cur.rowcount > 0


def list_active(kind: str | None = None) -> list[dict]:
    """有效记忆:active 且未过期。"""
    q = "SELECT * FROM memories WHERE active=1 AND (valid_until IS NULL OR valid_until >= ?)"
    args: list = [clock.now_iso()]
    if kind:
        q += " AND kind=?"
        args.append(kind)
    q += " ORDER BY kind, COALESCE(valid_until, '9999'), id"
    return [dict(r) for r in db.get_db().execute(q, args).fetchall()]


def list_all(include_inactive: bool = False) -> list[dict]:
    q = "SELECT * FROM memories" + ("" if include_inactive else " WHERE active=1") + \
        " ORDER BY active DESC, kind, COALESCE(valid_until,'9999'), id"
    return [dict(r) for r in db.get_db().execute(q).fetchall()]


# --- 注入 ---

def context_block() -> str:
    """给 conversation 的【长期记忆】段。上限 MAX_INJECT 条。"""
    rows = list_active()[:MAX_INJECT]
    if not rows:
        return ""
    lines = ["【长期记忆】(id 供「忘掉」引用)"]
    for r in rows:
        until = f"(〜{clock.fmt_local(r['valid_until'], '%m/%d %H:%M')})" if r["valid_until"] else ""
        lines.append(f"- [{r['id']}] {KIND_JA.get(r['kind'], r['kind'])}: {r['text']}{until}")
    return "\n".join(lines)


def _listing_for_llm() -> str:
    rows = list_active()
    if not rows:
        return "(空)"
    return "\n".join(
        f"id={r['id']} kind={r['kind']} until={clock.fmt_local(r['valid_until'], '%Y-%m-%d %H:%M') if r['valid_until'] else 'null'} | {r['text']}"
        for r in rows
    )


# --- 维护 ---

def _parse(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        cleaned = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        d = json.loads(cleaned)
        return d if isinstance(d, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def apply_ops(ops: dict, source: str = "assistant") -> dict:
    """执行 LLM 给的增删改;id 必须在 active 集合内(幻觉 id 直接忽略)。返回计数。"""
    active_ids = {r["id"] for r in list_all()}
    n = {"add": 0, "update": 0, "remove": 0}
    for item in ops.get("add") or []:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        until = _norm_until(item.get("valid_until"))
        if item.get("kind") == "schedule" and until is None:
            continue   # 安排类没日期 = 无法自动失效,拒收
        add(item.get("kind", "fact"), str(item["text"]), until, source=source)
        n["add"] += 1
    for item in ops.get("update") or []:
        if not isinstance(item, dict):
            continue
        try:
            mid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if mid not in active_ids:
            continue
        until = "__keep__" if "valid_until" not in item else _norm_until(item.get("valid_until"))
        if update(mid, item.get("text"), until):
            n["update"] += 1
    for mid in ops.get("remove") or []:
        try:
            mid = int(mid)
        except (TypeError, ValueError):
            continue
        if mid in active_ids and deactivate(mid, reason="llm"):
            n["remove"] += 1
    return n


def _norm_until(v) -> str | None:
    if v in (None, "", "null"):
        return None
    try:
        return clock.parse_flexible_jst(str(v))
    except ValueError:
        return None


async def maintain_after_turn(user_text: str, assistant_reply: str) -> dict | None:
    """每轮对话后异步调用。LLM 不可用/输出坏 → 静默跳过。"""
    now = clock.now_local().strftime("%Y-%m-%d %H:%M (%A)")
    prompt = (f"【当前时间(东京)】{now}\n【现有记忆】\n{_listing_for_llm()}\n\n"
              f"【本轮对话】\n用户: {user_text}\n艾莉: {assistant_reply}")
    ops = _parse(await llm.complete(prompt, system=MAINTAIN_SYSTEM, timeout=40))
    if ops is None:
        return None
    n = apply_ops(ops)
    if any(n.values()):
        events.log("memory_ops", n)
    return n


async def nightly_cleanup() -> dict:
    """04:00:①过期失效 ②已完成/取消的 schedule 记忆(靠 LLM 语义,此处仅过期)③LLM 全量去重合并。"""
    conn = db.get_db()
    cur = conn.execute(
        "UPDATE memories SET active=0, updated_at=? WHERE active=1 AND valid_until IS NOT NULL "
        "AND valid_until < ?",
        (clock.now_iso(), clock.now_iso()),
    )
    conn.commit()
    expired = cur.rowcount
    n = {"expired": expired, "update": 0, "remove": 0}
    if len(list_active()) >= 2:
        ops = _parse(await llm.complete(f"【现有记忆】\n{_listing_for_llm()}",
                                        system=CLEANUP_SYSTEM, timeout=60))
        if ops:
            ops.pop("add", None)   # 清理阶段不新增
            r = apply_ops(ops, source="system")
            n["update"], n["remove"] = r["update"], r["remove"]
    events.log("memory_cleanup", n)
    return n
