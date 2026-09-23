"""每天 04:00 把对话整理进长期记忆库(jobs.memory_consolidate)。水位线 memory.watermark = 已整理到的 chat_log.id。
每个过完的习惯日(04:00 起算)一次:LLM#1 抽取候选条目 + 日记 → 候选与库里相似的旧条目配对 →
LLM#2 对账(ADD/UPDATE/SUPERSEDE/SKIP)→ 所有 await 结束后一个同步事务写入(记忆+向量+日记+批次记录+水位线)。
进程共用一个 SQLite 连接:事务中途不能 await,也不能调会 commit 的 events.log / store.set。
夜间任务不删除:改写/取代 = 新行 + 旧行失效并指向新行,最近一批可整体撤销(undo)。"""

import asyncio
import json
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from .. import clock, db, events, store
from ..llm import base as llm
from ..llm import embed
from ..notify import service as notify_service
from . import memories, routines

MAX_DAYS_PER_RUN = 7
MAX_ENTRIES = 15
MAX_CORE_NOMINATIONS = 2
NEIGHBOR_SIM = 0.55      # 候选与旧条目配对进对账的门槛(bge-m3 实测:「ゴールドジムに通っている」↔「サクラフィットに通っている」0.61)
RELATED_SIM = 0.45       # 抽取前按当天的话找「相关的已有记忆」给 LLM 看的门槛
NEIGHBORS = 5
MAX_REWRITES = 5          # 单日 UPDATE+SUPERSEDE 超过这个数 = 视为坏输出,当天全部降级为 ADD
STUCK_NIGHTS = 3          # 同一天连续失败这么多晚 → 降级只 ADD;降级后还失败就跳过这天(原话仍可 search)
TRANSCRIPT_MAX = 60000
ENTRY_KINDS = ("fact", "preference", "schedule", "event", "mood")

EXTRACT_SYSTEM = """你是家庭 AI 助手「艾莉」的记忆整理员。读【当天的对话】,把以后还用得上的信息整理成长期记忆条目,再写一篇当天的日记。只输出 JSON:
{"memories":[{"kind":"fact|preference|schedule|event|mood","text":"...","event_at":"yyyy-MM-dd HH:mm 或 yyyy-MM-dd 或 null","core":false,"about":[已有记忆的 id]}],"diary":"..."}
规则:
- 只记用户说过或确认过的事;艾莉自己的建议、寒暄不记
- 一条只写一件事:日语、第三人称(如「ジムを変えた」),60 字以内
- 一律用绝对日期(如「9/30」),任何地方(包括括号里)都不要出现「明日/来週/昨天/下周」这类相对说法
- kind:fact=背景事实(学校、工作、目标);preference=偏好/习惯;schedule=将来的安排(event_at=那件事的时间);event=已经发生的事(event_at=发生的时间);mood=心情/身体状况(event_at=当天)
- 〔実行済み: …〕是系统已经执行并存好的操作(体重、提醒、routine 的设定与完成、remember/forget):数据本身不要再记,但用户说的原因、感受、背景要记
- 当天被用户否定、取消或要求忘掉的事不要记
- 偏好或事实变了(换了健身房、改了目标…)时,写成变化后的现状(如「ゴールドジムに通っている」),不要只写「変えた」;需要的话另记一条 event
- about:这条和【核心档案】【相关的已有记忆】里哪几条说的是同一件事(补充、订正、变化了),填它们的 id;没有就给空数组
- core=true 只给长期重要、几乎每次对话都该知道的 fact/preference(目标、过敏、作息偏好…),每天最多 2 条;【核心档案】里已有的不要重复
- 最多 15 条;没什么可记的就给空数组
- diary:2–4 句日语,第三人称概括这一天(做了什么、状态如何、有什么安排);几乎没聊什么就给 null"""

RECONCILE_SYSTEM = """你是家庭 AI 助手「艾莉」的记忆整理员。每条【新候选】下面列了记忆库里和它相似的【已有记忆】,逐条判定。只输出 JSON:
{"decisions":[{"c":候选序号,"op":"ADD|UPDATE|SUPERSEDE|SKIP","id":已有记忆的 id 或 null,"text":"UPDATE 时合并后的完整一句,否则 null"}]}
- SKIP:已有记忆已经包含这条信息(说法不同也算)
- UPDATE:同一件事的补充或订正 → text 写合并后的完整一句(日语、第三人称、绝对日期)
- SUPERSEDE:情况变了,旧的不再成立(换了健身房、目标改了、安排取消或改期)→ 旧的作为历史保留
- ADD:新的信息,或者拿不准
- id 只能从该候选自己的【已有记忆】里选"""


class PlanError(RuntimeError):
    pass


@dataclass
class DayPlan:
    day: str
    lo_id: int
    hi_id: int
    items: list[dict] = field(default_factory=list)   # {"op","cand","cand_vec","target","text","vec"}
    diary: str | None = None
    diary_vec: np.ndarray | None = None
    rejected: int = 0
    guard: bool = False

    def preview(self) -> dict:
        return {"day": self.day, "chat_ids": [self.lo_id, self.hi_id], "diary": self.diary,
                "rejected": self.rejected, "guard": self.guard,
                "items": [{"op": it["op"], "kind": it["cand"]["kind"], "text": it["text"],
                           "target": (it["target"] or {}).get("id")} for it in self.items]}


_lock = asyncio.Lock()


# --- 入口 ---

async def run(*, dry_run: bool = False, force: bool = False) -> dict:
    """force=手动触发(无视 memory.enabled);dry_run=只算不写,返回拟写入的内容。"""
    if not (force or dry_run) and not store.get("memory.enabled", True):
        return {"skipped": "disabled"}
    if _lock.locked():
        return {"skipped": "running"}
    async with _lock:
        return await _run(dry_run)


async def _run(dry_run: bool) -> dict:
    boundary = routines.habit_day_start(routines.habit_day(clock.now_local()))   # 今天习惯日的起点
    rows = db.get_db().execute(
        "SELECT * FROM chat_log WHERE id > ? AND ts < ? ORDER BY id",
        (_watermark(), clock.iso(boundary))).fetchall()
    result: dict = {"dry_run": dry_run, "days": []}
    if rows:
        try:
            await embed.embed_batch(["ping"])          # Ollama 挂了就别白花 LLM 调用
        except embed.EmbedUnavailable as e:
            result["error"] = f"向量模型不可用: {e}"
            if not dry_run:
                await _failed(None, result["error"])
            return result
        groups: OrderedDict[str, list] = OrderedDict()
        for r in rows:
            groups.setdefault(routines.habit_day(clock.to_local(clock.parse_iso(r["ts"]))).isoformat(), []).append(r)
        for day, day_rows in list(groups.items())[:MAX_DAYS_PER_RUN]:
            add_only = _stuck_count(day) >= STUCK_NIGHTS
            try:
                plan = await _plan_day(day, day_rows, add_only=add_only)
            except (PlanError, embed.EmbedUnavailable) as e:
                result["error"] = f"{day}: {e}"
                if dry_run:
                    break
                if add_only:      # 降级后仍失败(多半是抽取本身坏了):跳过这天,别让积压越堆越多
                    _skip_day(day, day_rows, str(e))
                    await notify_service.notify(
                        "info", "記憶の整理をスキップ",
                        f"{day} の会話は何度やっても整理できなかったから飛ばしたよ(原文は検索できる)")
                    continue
                await _failed(day, str(e))
                break
            result["days"].append(plan.preview() if dry_run else _apply(plan))
            if not dry_run:
                _clear_failures()
    if not dry_run:
        try:
            result["backfilled"] = await memories.ensure_vectors()
        except embed.EmbedUnavailable:
            pass
    return result


# --- 规划(只读 + await)---

async def _plan_day(day: str, rows: list, add_only: bool = False) -> DayPlan:
    plan = DayPlan(day=day, lo_id=rows[0]["id"], hi_id=rows[-1]["id"])
    core_rows = memories.core_rows()
    core = "\n".join(f"- [#{r['id']}] {r['text']}" for r in core_rows) or "(空)"
    related = await _related_existing(rows)
    known = {r["id"] for r in core_rows} | {m["id"] for m in related}
    d = await _ask_json(EXTRACT_SYSTEM,
                        f"【整理対象日】{day}(04:00〜翌 04:00,东京时间)\n【核心档案】\n{core}\n"
                        f"【相关的已有记忆】\n{chr(10).join(memories.render(m) for m in related) or '(空)'}\n\n"
                        f"【当天的对话】\n{_transcript(rows)}")
    cands, plan.diary, plan.rejected = _clean_candidates(d, day, known)
    texts = [c["text"] for c in cands] + ([plan.diary] if plan.diary else [])
    if not texts:
        return plan
    vecs = await embed.embed_batch(texts)
    cand_vecs = vecs[:len(cands)]
    if plan.diary:
        plan.diary_vec = vecs[len(cands)]
    neighbors = _neighbors(cand_vecs)
    active = {m["id"]: m for m in memories._searchable()[0] if m["active"] and m["kind"] != "diary"}
    for i, c in enumerate(cands):      # 措辞差太远、向量配不上的(「変えた」↔「通っている」),靠 LLM#1 的 about 对上号
        for aid in c["about"]:
            if aid in active and all(n["id"] != aid for n in neighbors[i]):
                neighbors[i].insert(0, active[aid])
    plan.items = [{"op": "ADD", "cand": c, "cand_vec": v, "target": None, "text": c["text"], "vec": v}
                  for c, v in zip(cands, cand_vecs)]
    need = [i for i, n in enumerate(neighbors) if n]
    if not need or add_only:
        return plan
    lines = []
    for i in need:
        c = cands[i]
        lines.append(f"#{i} 新候选:[{memories.KIND_JA[c['kind']]}] {c['text']}"
                     + (f"(時刻 {c['event_at_local']})" if c.get("event_at_local") else "") + "\n   已有:\n"
                     + "\n".join(f"   {memories.render(n)}" for n in neighbors[i]))
    d2 = await _ask_json(RECONCILE_SYSTEM, f"【整理対象日】{day}\n\n" + "\n".join(lines))
    decisions = _validate_decisions(d2, cands, neighbors)
    if sum(1 for op, _, _ in decisions.values() if op in ("UPDATE", "SUPERSEDE")) > MAX_REWRITES:
        plan.guard = True
        return plan
    merged = []
    for i, (op, target, text) in decisions.items():
        it = plan.items[i]
        it["op"], it["target"] = op, target
        if op == "UPDATE":
            it["text"] = text
            merged.append(i)
    if merged:
        mvecs = await embed.embed_batch([plan.items[i]["text"] for i in merged])
        for i, v in zip(merged, mvecs):
            plan.items[i]["vec"] = v
    return plan


def _transcript(rows: list) -> str:
    lines = []
    for r in rows:
        t = clock.fmt_local(r["ts"], "%m/%d %H:%M")
        if r["role"] == "user":
            lines.append(f"[{t}] ユーザー: {r['text']}")
        else:
            note = memories.actions_note(r["actions"])
            lines.append(f"[{t}] エリ: {note + ' ' if note else ''}{r['text']}")
    return "\n".join(lines)[-TRANSCRIPT_MAX:]


def _clean_candidates(d: dict, day: str, known: set[int] = frozenset()) -> tuple[list[dict], str | None, int]:
    """校验 LLM#1:kind 合法、有正文、无相对时间词、安排必须有时间;core 限 fact/preference 且每晚 ≤2 条;
    about 只留给它看过的 id(核心档案 + 相关的已有记忆)。"""
    out, rejected, nominated = [], 0, 0
    for item in (d.get("memories") or [])[:MAX_ENTRIES * 2]:
        if not isinstance(item, dict) or item.get("kind") not in ENTRY_KINDS:
            rejected += 1
            continue
        text = str(item.get("text") or "").strip()[:120]
        if not text or memories.RE_RELATIVE.search(text):
            rejected += 1
            continue
        event_at = None
        if item.get("event_at"):
            try:
                event_at = clock.parse_flexible_jst(str(item["event_at"]))
            except ValueError:
                event_at = None
        if item["kind"] == "schedule" and event_at is None:
            rejected += 1          # 没有日期的安排无法按时间提起,也无法判断过没过期
            continue
        if item["kind"] in ("event", "mood") and event_at is None:
            event_at = clock.iso(routines.habit_day_start(_date(day)).replace(hour=12))
        core = bool(item.get("core")) and item["kind"] in memories.CORE_KINDS and nominated < MAX_CORE_NOMINATIONS
        nominated += core
        about = []
        for x in item.get("about") or []:
            try:
                if int(x) in known:
                    about.append(int(x))
            except (TypeError, ValueError):
                continue
        out.append({"kind": item["kind"], "text": text, "event_at": event_at, "core": core, "about": about[:3],
                    "event_at_local": clock.fmt_local(event_at, "%Y-%m-%d %H:%M") if event_at else None})
        if len(out) >= MAX_ENTRIES:
            break
    diary = str(d.get("diary") or "").strip()[:400] or None
    return out, diary, rejected


async def _related_existing(rows: list) -> list[dict]:
    """当天用户说的每句话各自在库里找最像的旧条目(核心档案另给),最多 30 条,给 LLM#1 对号入座用。"""
    texts = [r["text"][:500] for r in rows if r["role"] == "user"][:60]
    meta, M = memories._searchable()
    idx = [i for i, m in enumerate(meta) if m["active"] and m["kind"] != "diary" and not m["core"]]
    if not texts or not idx or M.size == 0:
        return []
    Q = await embed.embed_batch(texts)
    if Q.shape[1] != M.shape[1]:
        return []
    best = (Q @ M[idx].T).max(axis=0)          # 每条旧记忆与当天任一句话的最高相似度
    return [meta[idx[j]] for j in np.argsort(-best)[:30] if best[j] >= RELATED_SIM]


def _neighbors(cand_vecs: np.ndarray) -> list[list[dict]]:
    meta, M = memories._searchable()
    idx = [i for i, m in enumerate(meta) if m["active"] and m["kind"] != "diary"]
    if not idx or M.size == 0 or len(cand_vecs) == 0 or M.shape[1] != cand_vecs.shape[1]:
        return [[] for _ in range(len(cand_vecs))]
    S = cand_vecs @ M[idx].T
    out = []
    for row in S:
        order = [j for j in np.argsort(-row)[:NEIGHBORS] if row[j] >= NEIGHBOR_SIM]
        out.append([meta[idx[j]] for j in order])
    return out


def _validate_decisions(d: dict, cands: list[dict], neighbors: list[list[dict]]) -> dict[int, tuple]:
    """LLM#2 → {候选序号: (op, 目标行 meta|None, text)}。id 必须在该候选自己的邻居里、同一目标只能动一次;
    不合法 / 没给判定 / UPDATE 没给合法合并句 → ADD。"""
    out: dict[int, tuple] = {}
    used: set[int] = set()
    for dec in d.get("decisions") or []:
        try:
            c = int(dec.get("c"))
        except (TypeError, ValueError, AttributeError):
            continue
        if not 0 <= c < len(cands) or c in out:
            continue
        op = str(dec.get("op") or "ADD").upper()
        if op == "SKIP":
            out[c] = ("SKIP", None, cands[c]["text"])
            continue
        if op not in ("UPDATE", "SUPERSEDE"):
            continue
        try:
            tid = int(dec.get("id"))
        except (TypeError, ValueError):
            continue
        target = next((n for n in neighbors[c] if n["id"] == tid), None)
        if target is None or tid in used:
            continue
        text = str(dec.get("text") or "").strip()[:120]
        if op == "UPDATE" and (not text or memories.RE_RELATIVE.search(text)):
            continue
        used.add(tid)
        out[c] = (op, target, text if op == "UPDATE" else cands[c]["text"])
    return {i: out.get(i, ("ADD", None, cands[i]["text"])) for i in range(len(cands))}


async def _ask_json(system: str, user: str) -> dict:
    model = str(store.get("memory.llm_model", "") or "") or None
    for _ in (0, 1):
        msg = await llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                             timeout=180, model=model, response_format={"type": "json_object"})
        content = (msg or {}).get("content") or ""
        try:
            d = json.loads(re.sub(r"^```(json)?|```$", "", content.strip(), flags=re.MULTILINE).strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(d, dict):
            return d
    raise PlanError("LLM から使える JSON が返ってこなかった")


# --- 写入(一个同步事务,中途不 await、不调会 commit 的函数)---

def _apply(plan: DayPlan) -> dict:
    conn = db.get_db()
    now = clock.now_iso()
    stats = {"added": 0, "updated": 0, "superseded": 0, "skipped": 0, "conflict": 0,
             "diary": False, "rejected": plan.rejected, "guard": plan.guard}
    try:
        run_id = conn.execute(
            "INSERT INTO memory_runs (day, lo_id, hi_id, status, started_at) VALUES (?,?,?,'running',?)",
            (plan.day, plan.lo_id, plan.hi_id, now)).lastrowid
        vec_items = []
        for it in plan.items:
            op, target, text, vec = it["op"], it["target"], it["text"], it["vec"]
            if op == "SKIP":
                stats["skipped"] += 1
                continue
            if target is not None:
                cur = conn.execute("SELECT active, updated_at FROM memories WHERE id=?", (target["id"],)).fetchone()
                if cur is None or not cur["active"] or cur["updated_at"] != target["updated_at"]:
                    # 整理期间用户改过/忘掉了这条:不碰旧行,也不把它的内容合并复活——按候选原文新增
                    op, target, text, vec = "ADD", None, it["cand"]["text"], it["cand_vec"]
                    stats["conflict"] += 1
            c = it["cand"]
            event_at = c["event_at"] or (target or {}).get("event_at")
            core = int(bool(c["core"]) or bool((target or {}).get("core")))
            new_id = conn.execute(
                "INSERT INTO memories (kind, text, valid_from, event_at, day, core, source, created_at, updated_at, "
                "run_id, src_hi) VALUES (?,?,?,?,?,?,'consolidate',?,?,?,?)",
                (c["kind"], text[:memories.TEXT_MAX], now, event_at,
                 memories.local_day(event_at) if event_at else plan.day, core, now, now, run_id, plan.hi_id),
            ).lastrowid
            vec_items.append((new_id, text, vec))
            if target is not None:
                conn.execute("UPDATE memories SET active=0, superseded_by=?, valid_until=?, updated_at=? WHERE id=?",
                             (new_id, now if op == "SUPERSEDE" else None, now, target["id"]))
                stats["updated" if op == "UPDATE" else "superseded"] += 1
            else:
                stats["added"] += 1
        if plan.diary and not conn.execute(
                "SELECT 1 FROM memories WHERE kind='diary' AND active=1 AND day=?", (plan.day,)).fetchone():
            did = conn.execute(
                "INSERT INTO memories (kind, text, valid_from, event_at, day, source, created_at, updated_at, run_id, "
                "src_hi) VALUES ('diary',?,?,?,?,'consolidate',?,?,?,?)",
                (plan.diary, now, clock.iso(routines.habit_day_start(_date(plan.day))), plan.day, now, now,
                 run_id, plan.hi_id)).lastrowid
            vec_items.append((did, plan.diary, plan.diary_vec))
            stats["diary"] = True
        memories.upsert_vectors(conn, [v for v in vec_items if v[2] is not None])
        conn.execute("UPDATE memory_runs SET status='ok', stats=?, finished_at=? WHERE id=?",
                     (json.dumps(stats), now, run_id))
        _set_watermark(conn, plan.hi_id, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    memories.invalidate()
    events.log("memory_consolidate", {"day": plan.day, "run_id": run_id, **stats})
    return {"day": plan.day, "run_id": run_id, **stats}


def _skip_day(day: str, rows: list, reason: str) -> None:
    conn = db.get_db()
    now = clock.now_iso()
    try:
        conn.execute("INSERT INTO memory_runs (day, lo_id, hi_id, status, stats, started_at, finished_at) "
                     "VALUES (?,?,?,'skipped',?,?,?)",
                     (day, rows[0]["id"], rows[-1]["id"], json.dumps({"reason": reason[:200]}), now, now))
        _set_watermark(conn, rows[-1]["id"], now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    events.log("memory_consolidate_skipped", {"day": day, "reason": reason[:200]})
    _clear_failures()


def undo(run_id: int) -> dict:
    """撤销最近一批:本批新行失效、被它取代的旧行复原、水位线退回本批之前(下次整理会重做这一天)。"""
    conn = db.get_db()
    run = conn.execute("SELECT * FROM memory_runs WHERE id=?", (run_id,)).fetchone()
    latest = conn.execute("SELECT MAX(id) FROM memory_runs WHERE status='ok'").fetchone()[0]
    if run is None or run["status"] != "ok":
        raise ValueError("そのバッチは取り消せないよ")
    if run_id != latest:
        raise ValueError("取り消せるのは一番新しいバッチだけだよ")
    now = clock.now_iso()
    new_ids = [r[0] for r in conn.execute("SELECT id FROM memories WHERE run_id=?", (run_id,)).fetchall()]
    try:
        conn.execute("UPDATE memories SET active=0, updated_at=? WHERE run_id=?", (now, run_id))
        if new_ids:
            conn.execute("UPDATE memories SET active=1, superseded_by=NULL, valid_until=NULL, updated_at=? "
                         f"WHERE superseded_by IN ({','.join('?' * len(new_ids))})", (now, *new_ids))
        conn.execute("UPDATE memory_runs SET status='undone', finished_at=? WHERE id=?", (now, run_id))
        _set_watermark(conn, run["lo_id"] - 1, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    memories.invalidate()
    events.log("memory_undo", {"run_id": run_id, "day": run["day"], "reverted": len(new_ids)})
    return {"run_id": run_id, "day": run["day"], "reverted": len(new_ids)}


# --- 状态 / 失败记账 ---

def _date(day: str) -> date:
    return date.fromisoformat(day)


def _watermark() -> int:
    return int(store.get("memory.watermark", 0) or 0)


def _set_watermark(conn, value: int, now: str) -> None:
    conn.execute("INSERT INTO settings (key, value, updated_at) VALUES ('memory.watermark', ?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                 (json.dumps(int(value)), now))


def _stuck_count(day: str) -> int:
    s = store.get("memory.stuck", {}) or {}
    return int(s.get("count", 0)) if s.get("day") == day else 0


async def _failed(day: str | None, reason: str) -> None:
    streak = int(store.get("memory.fail_streak", 0) or 0) + 1
    store.set("memory.fail_streak", streak)
    if day:
        store.set("memory.stuck", {"day": day, "count": _stuck_count(day) + 1})
    events.log("memory_consolidate_error", {"day": day, "reason": reason[:200], "streak": streak})
    if streak == 2:
        await notify_service.notify("info", "記憶の整理が2晩続けて失敗", f"{reason[:100]}\n/memories で状態を見てね")


def _clear_failures() -> None:
    if store.get("memory.fail_streak", 0):
        store.set("memory.fail_streak", 0)
    if store.get("memory.stuck", {}):
        store.set("memory.stuck", {})


def status() -> dict:
    conn = db.get_db()
    wm = _watermark()
    last = conn.execute("SELECT * FROM memory_runs ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "enabled": bool(store.get("memory.enabled", True)),
        "watermark": wm,
        "pending_messages": conn.execute("SELECT COUNT(*) FROM chat_log WHERE id > ?", (wm,)).fetchone()[0],
        "last_run": {**dict(last), "stats": json.loads(last["stats"] or "{}")} if last else None,
        "fail_streak": int(store.get("memory.fail_streak", 0) or 0),
        "running": _lock.locked(),
        **memories.stats(),
    }
