"""长期记忆库 API:增删改查、调阈值用的相似度排名、手动整理/试运行、撤销、重新嵌入、状态。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import clock
from ..auth import require_token
from ..llm import embed
from ..services import consolidate
from ..services import memories as svc

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


class MemoryIn(BaseModel):
    kind: str = "fact"
    text: str
    event_at: str | None = None      # schedule/event 的时间(东京)
    core: bool = False


class MemoryUpdate(BaseModel):
    text: str | None = None
    core: bool | None = None


class RankIn(BaseModel):
    query: str
    limit: int = 10


@router.get("/memories")
async def list_memories(q: str | None = None, kind: str | None = None, core: bool | None = None,
                        day_from: str | None = None, day_to: str | None = None,
                        limit: int = 50, offset: int = 0):
    return svc.list_rows(q=q, kind=kind, core=core, day_from=day_from, day_to=day_to,
                         limit=min(limit, 200), offset=offset)


@router.post("/memories")
async def create_memory(body: MemoryIn):
    try:
        at = clock.parse_flexible_jst(body.event_at) if body.event_at else None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return svc.add(body.kind, body.text, event_at=at, core=body.core, source="user",
                   vec=await embed.embed_query(body.text))


@router.put("/memories/{mid}")
async def update_memory(mid: int, body: MemoryUpdate):
    row = svc.get(mid)
    if row is None or not row["active"]:
        raise HTTPException(404, "not found")
    if body.text is not None:
        svc.set_text(mid, body.text)
    if body.core is not None:
        svc.set_core(mid, body.core)
    return svc.get(mid)


@router.delete("/memories/{mid}")
async def delete_memory(mid: int):
    if not svc.deactivate(mid, reason="user"):
        raise HTTPException(404, "not found")
    return {"deleted": mid}


@router.post("/memories/{mid}/reactivate")
async def reactivate_memory(mid: int):
    if not svc.reactivate(mid):
        raise HTTPException(404, "not found or already active")
    return svc.get(mid)


@router.post("/memories/rank")
async def rank(body: RankIn):
    """调 memory.rag_min_sim 用:这句话和库里各条的相似度(不设门槛)。"""
    vec = await embed.embed_query(body.query)
    if vec is None:
        raise HTTPException(503, "向量模型不可用")
    return svc.rank(vec, limit=min(body.limit, 50))


@router.post("/memories/consolidate")
async def run_consolidate(dry_run: bool = False):
    """手动整理(无视 memory.enabled);dry_run=1 只看拟写入内容。"""
    return await consolidate.run(dry_run=dry_run, force=True)


@router.post("/memories/runs/{run_id}/undo")
async def undo_run(run_id: int):
    try:
        return consolidate.undo(run_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/memories/reembed")
async def reembed():
    """换了嵌入模型/改过文本后重算向量。"""
    try:
        return {"reembedded": await svc.ensure_vectors(limit=100000)}
    except embed.EmbedUnavailable as e:
        raise HTTPException(503, str(e)) from e


@router.get("/memories/status")
async def status():
    return {**consolidate.status(), "embed_ok": await embed.prewarm()}
