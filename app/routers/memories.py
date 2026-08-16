from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_token
from ..services import memories as svc

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


class MemoryIn(BaseModel):
    kind: str = "fact"
    text: str
    valid_until: str | None = None


class MemoryUpdate(BaseModel):
    text: str | None = None
    valid_until: str | None = None


@router.get("/memories")
async def list_memories(all: bool = False):
    return svc.list_all(include_inactive=all)


@router.post("/memories")
async def create_memory(body: MemoryIn):
    until = svc._norm_until(body.valid_until) if body.valid_until else None
    return svc.add(body.kind, body.text, until, source="user")


@router.put("/memories/{mid}")
async def update_memory(mid: int, body: MemoryUpdate):
    until = "__keep__" if body.valid_until is None else svc._norm_until(body.valid_until)
    row = svc.update(mid, body.text, until)
    if row is None:
        raise HTTPException(404, "not found")
    return row


@router.delete("/memories/{mid}")
async def delete_memory(mid: int):
    if not svc.deactivate(mid, reason="user"):
        raise HTTPException(404, "not found")
    return {"deleted": mid}


@router.post("/memories/cleanup")
async def run_cleanup():
    """手动触发一次夜间清理(测试用)。"""
    return await svc.nightly_cleanup()
