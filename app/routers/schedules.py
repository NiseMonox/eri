from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_token
from ..scheduler import core, jobs
from ..services import schedules as svc

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


class ScheduleIn(BaseModel):
    name: str
    type: str
    cron: str
    payload: dict = {}
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    cron: str | None = None
    payload: dict | None = None
    enabled: bool | None = None


@router.get("/schedules")
async def list_schedules():
    return svc.list_all()


@router.post("/schedules")
async def create_schedule(body: ScheduleIn):
    try:
        return svc.create(body.name, body.type, body.cron, body.payload, body.enabled)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/schedules/{sid}")
async def update_schedule(sid: int, body: ScheduleUpdate):
    try:
        row = svc.update(sid, name=body.name, type_=body.type, cron=body.cron,
                         payload=body.payload, enabled=body.enabled)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if row is None:
        raise HTTPException(404, "not found")
    return row


@router.delete("/schedules/{sid}")
async def delete_schedule(sid: int):
    if not svc.delete(sid):
        raise HTTPException(404, "not found")
    return {"deleted": sid}


@router.post("/schedules/{sid}/toggle")
async def toggle_schedule(sid: int):
    row = svc.toggle(sid)
    if row is None:
        raise HTTPException(404, "not found")
    return row


@router.post("/schedules/{sid}/run")
async def run_now(sid: int):
    """立即执行一次(无视 enabled),不影响 cron。"""
    if svc.get(sid) is None:
        raise HTTPException(404, "not found")
    await jobs.run_schedule(sid, force=True)
    return {"ran": sid}


class OneshotIn(BaseModel):
    type: str
    payload: dict = {}
    in_sec: int = 60


@router.post("/dev/oneshot")
async def dev_oneshot(body: OneshotIn):
    """端到端测试:in_sec 秒后触发一次,不建 schedule 行。"""
    if body.type not in svc.TYPES:
        raise HTTPException(400, f"invalid type: {body.type}")
    run_at = core.add_oneshot(body.type, body.payload, body.in_sec)
    return {"run_at": run_at}
