"""长期记忆库(只增不删)。
写入:每天 04:00 从对话整理(consolidate.py)、对话里的「记住」(remember 工具)、网页。
改写/取代 = 新行 + 旧行 active=0 且 superseded_by 指向新行(按整理批次可撤销);「忘掉」= active=0。
读取:核心档案 core_block(每次都带)+ 近日の予定 upcoming_block(按时间)+ 相关记忆 related(本地向量检索)
+ search(search_memory 工具:语义或按日期,含被取代的历史与当天原话)。
kind: schedule(将来的安排,event_at)| event(发生过的事)| diary(每日日记)| preference | fact | mood"""

import hashlib
import json
import math
import re
from datetime import datetime, timedelta

import numpy as np

from .. import clock, db, events, store
from ..llm import embed

KINDS = ("schedule", "event", "diary", "preference", "fact", "mood")
KIND_JA = {"preference": "好み・習慣", "fact": "事実", "schedule": "予定", "event": "出来事",
           "mood": "近況", "diary": "日記"}
CORE_KINDS = ("fact", "preference")
TEXT_MAX = 300
MOOD_DAYS, SCHEDULE_DAYS = 14, 60      # 相关记忆里:两周前的心情、两个月前的安排不再自动带出(search 仍能查到)
DUP_SIM = 0.90                          # 「记住」时 ≥ 这个相似度视为已经记着
SEARCH_MIN_SIM = 0.45
# 记忆要存很久,相对时间词过几天就没法读懂:整理与「记住」都要求绝对日期(日记例外,它本身挂在某一天)
RE_RELATIVE = re.compile(
    "明日|あした|あす|明後日|あさって|昨日|きのう|一昨日|おととい|今日|きょう|今週|来週|先週|今月|来月|先月"
    "|今年|来年|去年|昨年|明天|后天|昨天|前天|今天|本周|这周|下周|上周|下个月|上个月|这个月|明年")


def _est_tokens(s: str) -> int:
    cjk = sum(1 for ch in s if "\U00003000" <= ch <= "\U00009FFF" or "\U0000FF00" <= ch <= "\U0000FFEF")
    return cjk + max(0, len(s) - cjk) // 4 + 1


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def local_day(iso: str) -> str:
    return clock.to_local(clock.parse_iso(iso)).strftime("%Y-%m-%d")


def date_label(iso: str, with_time: bool = False) -> str:
    """09/24(木)[ 15:00];不是今年的带年份。星期由这里算好给 LLM——模型自己推星期常算错。"""
    from .routines import WEEKDAY_JA

    d = clock.to_local(clock.parse_iso(iso))
    s = d.strftime("%m/%d" if d.year == clock.now_local().year else "%Y/%m/%d")
    s += f"({WEEKDAY_JA[int(d.strftime('%w'))]})"
    if with_time and (d.hour or d.minute):
        s += d.strftime(" %H:%M")
    return s


def _day_iso(day: str) -> str:
    return clock.iso(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=clock.TOKYO))


def actions_note(actions_json: str | None) -> str:
    """chat_log.actions → 「〔実行済み: …〕」(对话历史与夜间整理共用)。只读工具不列。"""
    try:
        acts = json.loads(actions_json) if actions_json else []
    except (json.JSONDecodeError, TypeError):
        return ""
    done = [a.get("result") for a in acts if isinstance(a, dict) and a.get("ok") and a.get("result")]
    return f"〔実行済み: {' / '.join(done)}〕" if done else ""


# --- 向量矩阵缓存(任何写入即失效)---

_version = 0
_cache: dict = {}


def invalidate() -> None:
    global _version
    _version += 1


def _searchable() -> tuple[list[dict], np.ndarray]:
    """可检索的行(启用中 + 被取代的历史;用户忘掉的不在内)及向量矩阵,只用当前模型算的向量。"""
    conn = db.get_db()
    key = (id(conn), embed.model(), _version)
    if _cache.get("key") == key:
        return _cache["val"]
    rows = conn.execute(
        "SELECT m.id, m.kind, m.text, m.core, m.active, m.event_at, m.day, m.created_at, m.updated_at, "
        "m.valid_until, m.superseded_by, m.src_hi, v.vec FROM memories m "
        "JOIN memory_vectors v ON v.memory_id=m.id "
        "WHERE v.model=? AND (m.active=1 OR m.superseded_by IS NOT NULL) ORDER BY m.id",
        (embed.model(),),
    ).fetchall()
    meta, vecs = [], []
    for r in rows:
        d = dict(r)
        vecs.append(np.frombuffer(d.pop("vec"), dtype=np.float32))
        meta.append(d)
    val = (meta, np.vstack(vecs) if vecs else np.zeros((0, 0), dtype=np.float32))
    _cache.update(key=key, val=val)
    return val


def _sims(M: np.ndarray, qvec: np.ndarray) -> np.ndarray | None:
    if M.size == 0 or M.shape[1] != qvec.shape[0]:
        return None
    return M @ qvec


def upsert_vectors(conn, items: list[tuple[int, str, np.ndarray]]) -> None:
    """写向量,不 commit(夜间整理在自己的大事务里调用)。"""
    for mid, text, vec in items:
        conn.execute(
            "INSERT OR REPLACE INTO memory_vectors (memory_id, model, dim, text_hash, vec) VALUES (?,?,?,?,?)",
            (mid, embed.model(), int(vec.shape[0]), _hash(text), np.asarray(vec, dtype=np.float32).tobytes()),
        )


async def ensure_vectors(limit: int = 1000) -> int:
    """回填:缺向量 / 换了模型 / 文本改过 的可检索行。Ollama 不可用抛 EmbedUnavailable。"""
    rows = db.get_db().execute(
        "SELECT m.id, m.text, v.model, v.text_hash FROM memories m "
        "LEFT JOIN memory_vectors v ON v.memory_id=m.id "
        "WHERE m.active=1 OR m.superseded_by IS NOT NULL ORDER BY m.id"
    ).fetchall()
    todo = [(r["id"], r["text"]) for r in rows
            if r["model"] != embed.model() or r["text_hash"] != _hash(r["text"])][:limit]
    if not todo:
        return 0
    vecs = await embed.embed_batch([t for _, t in todo])
    conn = db.get_db()
    upsert_vectors(conn, [(mid, t, v) for (mid, t), v in zip(todo, vecs)])
    conn.commit()
    invalidate()
    return len(todo)


# --- 写(即时:网页 / 「记住」)---

def add(kind: str, text: str, *, event_at: str | None = None, core: bool = False,
        source: str = "assistant", vec: np.ndarray | None = None) -> dict:
    kind = kind if kind in KINDS else "fact"
    text = text.strip()[:TEXT_MAX]
    now = clock.now_iso()
    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO memories (kind, text, valid_from, event_at, day, core, source, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (kind, text, now, event_at, local_day(event_at or now), int(bool(core) and kind in CORE_KINDS),
         source, now, now),
    )
    if vec is not None:
        upsert_vectors(conn, [(cur.lastrowid, text, vec)])
    conn.commit()
    invalidate()
    events.log("memory_add", {"kind": kind, "text": text[:80], "source": source}, "memory", cur.lastrowid)
    return get(cur.lastrowid)


def get(mid: int) -> dict | None:
    row = db.get_db().execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
    return dict(row) if row else None


def set_text(mid: int, text: str) -> dict | None:
    """网页改文本:就地改,向量作废(回填时重算)。"""
    conn = db.get_db()
    cur = conn.execute("UPDATE memories SET text=?, updated_at=? WHERE id=? AND active=1",
                       (text.strip()[:TEXT_MAX], clock.now_iso(), mid))
    if cur.rowcount:
        conn.execute("DELETE FROM memory_vectors WHERE memory_id=?", (mid,))
    conn.commit()
    invalidate()
    if cur.rowcount:
        events.log("memory_update", {"text": text[:80]}, "memory", mid)
    return get(mid) if cur.rowcount else None


def set_core(mid: int, flag: bool) -> bool:
    conn = db.get_db()
    cur = conn.execute("UPDATE memories SET core=?, updated_at=? WHERE id=? AND active=1 AND kind IN (?,?)",
                       (int(flag), clock.now_iso(), mid, *CORE_KINDS))
    conn.commit()
    invalidate()
    return cur.rowcount > 0


def deactivate(mid: int, reason: str = "manual") -> bool:
    conn = db.get_db()
    cur = conn.execute("UPDATE memories SET active=0, updated_at=? WHERE id=? AND active=1",
                       (clock.now_iso(), mid))
    conn.commit()
    invalidate()
    if cur.rowcount:
        events.log("memory_remove", {"reason": reason}, "memory", mid)
    return cur.rowcount > 0


def reactivate(mid: int) -> bool:
    conn = db.get_db()
    try:
        cur = conn.execute("UPDATE memories SET active=1, superseded_by=NULL, valid_until=NULL, updated_at=? "
                           "WHERE id=? AND active=0", (clock.now_iso(), mid))
        conn.commit()
    except Exception:  # noqa: BLE001  同一天已有启用中的日记(唯一索引)
        conn.rollback()
        return False
    invalidate()
    if cur.rowcount:
        events.log("memory_reactivate", None, "memory", mid)
    return cur.rowcount > 0


# --- 读 ---

def list_rows(*, q: str | None = None, kind: str | None = None, core: bool | None = None,
              day_from: str | None = None, day_to: str | None = None,
              limit: int = 50, offset: int = 0) -> list[dict]:
    """网页/API 用:启用中的记忆,新的在前。"""
    where, args = ["active=1"], []
    if q:
        where.append("text LIKE ?")
        args.append(f"%{q}%")
    if kind:
        where.append("kind=?")
        args.append(kind)
    if core is not None:
        where.append("core=?")
        args.append(int(core))
    if day_from:
        where.append("day>=?")
        args.append(day_from)
    if day_to:
        where.append("day<=?")
        args.append(day_to)
    sql = (f"SELECT * FROM memories WHERE {' AND '.join(where)} "
           "ORDER BY COALESCE(day, '') DESC, id DESC LIMIT ? OFFSET ?")
    return [dict(r) for r in db.get_db().execute(sql, (*args, limit, offset)).fetchall()]


def forgotten(limit: int = 30) -> list[dict]:
    """用户忘掉的(不含被取代的历史),可恢复。"""
    rows = db.get_db().execute(
        "SELECT * FROM memories WHERE active=0 AND superseded_by IS NULL ORDER BY updated_at DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


def core_rows() -> list[dict]:
    rows = db.get_db().execute(
        "SELECT * FROM memories WHERE active=1 AND core=1 ORDER BY CASE kind WHEN 'fact' THEN 0 ELSE 1 END, id"
    ).fetchall()
    return [dict(r) for r in rows]


def _core_line(r: dict) -> str:
    return f"- [#{r['id']} {KIND_JA.get(r['kind'], r['kind'])}] {r['text']}"


def core_block() -> tuple[str, set[int]]:
    """【核心档案】:固定顺序、不含任何随时间变化的内容(一天内逐字不变,命中前缀缓存);超预算的靠后条目不带。"""
    budget = int(store.get("memory.core_budget_tokens", 400) or 400)
    lines, ids, used = [], set(), 0
    for r in core_rows():
        line = _core_line(r)
        t = _est_tokens(line)
        if used + t > budget:
            break
        lines.append(line)
        ids.add(r["id"])
        used += t
    if not lines:
        return "", ids
    return "【核心档案】(用户的长期要点;#id 可用于 forget_memory)\n" + "\n".join(lines), ids


def core_usage() -> dict:
    rows = core_rows()
    used = sum(_est_tokens(_core_line(r)) for r in rows)
    return {"count": len(rows), "tokens": used,
            "budget": int(store.get("memory.core_budget_tokens", 400) or 400),
            "max": int(store.get("memory.core_max", 20) or 20)}


def upcoming_block(days: int = 3, limit: int = 5) -> tuple[str, set[int]]:
    """【近日の予定】:今天起 N 天内的安排,按时间取(不靠相似度——「明天出差」和当前话题无关也得知道)。"""
    now = clock.now_local()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.get_db().execute(
        "SELECT * FROM memories WHERE active=1 AND kind='schedule' AND event_at>=? AND event_at<=? "
        "ORDER BY event_at LIMIT ?",
        (clock.iso(start), clock.iso(now + timedelta(days=days)), limit)).fetchall()
    if not rows:
        return "", set()
    lines = [f"- [#{r['id']}] {date_label(r['event_at'], with_time=True)} {r['text']}" for r in rows]
    return "【近日の予定】(记忆里的安排,#id 可用于 forget_memory)\n" + "\n".join(lines), {r["id"] for r in rows}


def render(m: dict) -> str:
    """相关记忆/检索结果的一行:[#id 日付 種類] 本文。日付:予定/出来事/近況/日記 = 那件事的时间,
    事实/偏好 = 记下的日子;过期的安排标(予定だった),被取代的标到哪天为止。"""
    ref = m.get("event_at") or (_day_iso(m["day"]) if m.get("day") else m["created_at"])   # 事实/偏好:说这件事的那天
    tail = ""
    if m["kind"] == "schedule" and ref < clock.now_iso():
        tail = "(予定だった)"
    if not m.get("active", 1) and m.get("superseded_by"):
        tail += f"(〜{clock.fmt_local(m.get('valid_until') or m['updated_at'], '%m/%d')} 時点の情報)"
    return (f"- [#{m['id']} {date_label(ref, with_time=m['kind'] == 'schedule')} {KIND_JA.get(m['kind'], m['kind'])}] "
            f"{m['text']}{tail}")


def related(qvec: np.ndarray | None, *, hist_first_id: int | None = None,
            exclude: frozenset | set = frozenset()) -> tuple[list[dict], list[tuple[int, float]]]:
    """每句话自动带上的相关记忆。返回 (选中的行, 过门槛前的 top5 [(id, sim)] 供校准阈值)。
    排除:核心档案(已常驻)、日记、两周前的心情、两个月前的安排、来源对话还在原文窗口里的(免得重复)。
    门槛 sim ≥ max(rag_min_sim, top1−0.12);排序 = sim + 近期加成 + 两周内将来安排加成。"""
    if qvec is None:
        return [], []
    meta, M = _searchable()
    sims = _sims(M, qvec)
    if sims is None:
        return [], []
    now = clock.now_utc()
    cands = []
    for m, s in zip(meta, sims):
        if not m["active"] or m["core"] or m["kind"] == "diary" or m["id"] in exclude:
            continue
        if hist_first_id is not None and m["src_hi"] is not None and m["src_hi"] >= hist_first_id:
            continue
        age = (now - clock.parse_iso(m["event_at"] or m["created_at"])).total_seconds() / 86400
        if (m["kind"] == "mood" and age > MOOD_DAYS) or (m["kind"] == "schedule" and age > SCHEDULE_DAYS):
            continue
        bonus = 0.04 * math.exp(-max(age, 0) / 30) + (0.05 if m["kind"] == "schedule" and -14 <= age <= 0 else 0)
        cands.append((float(s) + bonus, float(s), m))
    top5 = [(m["id"], round(s, 3)) for _, s, m in sorted(cands, key=lambda c: -c[1])[:5]]
    if not cands:
        return [], top5
    floor = max(float(store.get("memory.rag_min_sim", 0.60) or 0.60), max(c[1] for c in cands) - 0.12)
    top_k = int(store.get("memory.rag_top_k", 6) or 6)
    budget = int(store.get("memory.rag_budget_tokens", 400) or 400)
    picked, used = [], 0
    for _, sim, m in sorted(cands, key=lambda c: -c[0]):
        if sim < floor:
            continue
        line = render(m)
        t = _est_tokens(line)
        if len(picked) >= top_k or used + t > budget:
            break
        picked.append({**m, "sim": round(sim, 3), "line": line})
        used += t
    return picked, top5


def related_block(rows: list[dict]) -> str:
    if not rows:
        return ""
    return ("【相关记忆】(按这句话从记忆库检索,旧的可能已过时;#id 可用于 forget_memory)\n"
            + "\n".join(r["line"] for r in rows))


def find_duplicate(text: str, vec: np.ndarray | None) -> dict | None:
    """「记住」前查重:向量相似度 ≥ DUP_SIM(拿不到向量时退回全文相同)。"""
    if vec is not None:
        meta, M = _searchable()
        sims = _sims(M, vec)
        if sims is not None:
            best = [(float(s), m) for m, s in zip(meta, sims) if m["active"]]
            if best:
                s, m = max(best, key=lambda x: x[0])
                if s >= DUP_SIM:
                    return m
    row = db.get_db().execute("SELECT * FROM memories WHERE active=1 AND text=?", (text.strip(),)).fetchone()
    return dict(row) if row else None


def search(query: str | None, qvec: np.ndarray | None = None, date_from: str | None = None,
           date_to: str | None = None, limit: int = 10) -> list[dict]:
    """search_memory 工具。有 query:语义检索(没有向量时退回关键词 LIKE),含被取代的历史;
    只给日期:那几天的日记 + 记忆 + 用户原话(chat_log)。返回 [{"id"(原话行没有), "line"}]。"""
    date_from = date_from or date_to
    date_to = date_to or date_from

    def in_range(day: str | None) -> bool:
        return (not date_from or (day or "") >= date_from) and (not date_to or (day or "") <= date_to)

    out: list[dict] = []
    if query:
        meta, M = _searchable()
        sims = _sims(M, qvec) if qvec is not None else None
        if sims is not None:
            for i in np.argsort(-sims):
                if sims[i] < SEARCH_MIN_SIM or len(out) >= limit:
                    break
                if in_range(meta[i]["day"]):
                    out.append({**meta[i], "line": render(meta[i])})
            return out
        terms = [t for t in re.split(r"\s+", query.strip()) if t][:5]
        rows = db.get_db().execute(
            "SELECT * FROM memories WHERE (active=1 OR superseded_by IS NOT NULL) AND ("
            + " OR ".join("text LIKE ?" for _ in terms) + ") ORDER BY id DESC LIMIT 100",
            [f"%{t}%" for t in terms]).fetchall()
        return [{**dict(r), "line": render(dict(r))} for r in rows if in_range(r["day"])][:limit]
    if not date_from:
        return []
    rows = db.get_db().execute(
        "SELECT * FROM memories WHERE active=1 AND day>=? AND day<=? "
        "ORDER BY CASE kind WHEN 'diary' THEN 0 ELSE 1 END, day, id LIMIT 30", (date_from, date_to)).fetchall()
    out = [{**dict(r), "line": render(dict(r))} for r in rows]
    start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=clock.TOKYO)
    end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=clock.TOKYO) + timedelta(days=1)
    for c in db.get_db().execute(
            "SELECT ts, text FROM chat_log WHERE role='user' AND ts>=? AND ts<? ORDER BY id LIMIT 40",
            (clock.iso(start), clock.iso(end))).fetchall():
        out.append({"line": f"- {clock.fmt_local(c['ts'], '%m/%d %H:%M')} ユーザーの発言「{c['text'][:200]}」"})
    return out


def rank(qvec: np.ndarray, limit: int = 10) -> list[dict]:
    """调阈值用:可检索行按相似度排序,不设门槛、不做排除。"""
    meta, M = _searchable()
    sims = _sims(M, qvec)
    if sims is None:
        return []
    return [{"id": meta[i]["id"], "kind": meta[i]["kind"], "text": meta[i]["text"], "core": meta[i]["core"],
             "active": meta[i]["active"], "sim": round(float(sims[i]), 3)} for i in np.argsort(-sims)[:limit]]


def stats() -> dict:
    conn = db.get_db()
    by_kind = {r["kind"]: r["n"] for r in conn.execute(
        "SELECT kind, COUNT(*) AS n FROM memories WHERE active=1 GROUP BY kind")}
    missing = conn.execute(
        "SELECT COUNT(*) FROM memories m LEFT JOIN memory_vectors v ON v.memory_id=m.id AND v.model=? "
        "WHERE (m.active=1 OR m.superseded_by IS NOT NULL) AND v.memory_id IS NULL", (embed.model(),)
    ).fetchone()[0]
    return {"active": sum(by_kind.values()), "by_kind": by_kind, "missing_vectors": missing,
            "core": core_usage()}
