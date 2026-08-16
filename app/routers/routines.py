from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_token
from ..services import routines as svc

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


class RoutineIn(BaseModel):
    name: str
    category: str = "other"
    detail: str = ""
    nag: dict | None = None


class RoutineUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    detail: str | None = None
    active: int | None = None
    nag: dict | None = None


@router.get("/routines")
async def list_routines(all: bool = False):
    return svc.list_all(include_inactive=all)


@router.post("/routines")
async def create_routine(body: RoutineIn):
    return svc.create(body.name, body.category, body.detail, body.nag)


@router.put("/routines/{rid}")
async def update_routine(rid: int, body: RoutineUpdate):
    row = svc.update(rid, **body.model_dump(exclude_none=True))
    if row is None:
        raise HTTPException(404, "not found")
    return row


@router.get("/routines/instances")
async def list_instances(routine_id: int | None = None, status: str | None = None, limit: int = 100):
    return svc.instances(routine_id, status, limit)


@router.get("/routines/heatmap")
async def heatmap(weeks: int = 12):
    return svc.heatmap(weeks)


@router.post("/routines/instances/{iid}/done")
async def instance_done(iid: int, via: str = "web"):
    row = svc.complete(iid, via=via)
    if row is None:
        raise HTTPException(404, "not found")
    return row


@router.post("/routines/instances/{iid}/skip")
async def instance_skip(iid: int, via: str = "web"):
    row = svc.skip(iid, via=via)
    if row is None:
        raise HTTPException(404, "not found")
    return row


@router.post("/routines/done-latest")
async def done_latest(via: str = "siri", category: str | None = None):
    """Siri「やった/薬飲んだ」:完成最近一条待确认实例。"""
    inst = svc.latest_open(category)
    if inst is None:
        return {"ok": False, "message": "いま確認待ちのルーティンはないよ"}
    row = svc.complete(inst["id"], via=via)
    return {"ok": True, "message": f"完了にしたよ:{row['title']}", "instance": row}


# --- 旧路径别名(Siri 快捷指令里可能还写着 /api/meds/confirm-latest)---
legacy = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


@legacy.post("/meds/confirm-latest")
async def legacy_confirm_latest(via: str = "siri"):
    inst = svc.latest_open("med")
    if inst is None:
        return {"ok": False, "message": "いま確認待ちのお薬はないよ"}
    row = svc.complete(inst["id"], via=via)
    return {"ok": True, "message": f"服薬確認したよ:{row['title']}", "instance": row}
