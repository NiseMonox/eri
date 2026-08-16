from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_token
from ..services import meds as svc

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


class MedIn(BaseModel):
    name: str
    dose: str = ""
    notes: str = ""


class MedUpdate(BaseModel):
    name: str | None = None
    dose: str | None = None
    notes: str | None = None
    active: int | None = None


@router.get("/meds")
async def list_meds(all: bool = False):
    return svc.list_meds(include_inactive=all)


@router.post("/meds")
async def create_med(body: MedIn):
    return svc.create_med(body.name, body.dose, body.notes)


@router.put("/meds/{mid}")
async def update_med(mid: int, body: MedUpdate):
    row = svc.update_med(mid, **body.model_dump(exclude_none=True))
    if row is None:
        raise HTTPException(404, "not found")
    return row


@router.get("/meds/logs")
async def list_logs(status: str | None = None, limit: int = 100):
    return svc.list_logs(status, limit)


@router.post("/meds/logs/{log_id}/confirm")
async def confirm_log(log_id: int, via: str = "web"):
    row = svc.confirm(log_id=log_id, via=via)
    if row is None:
        raise HTTPException(404, "not found")
    return row


@router.post("/meds/logs/{log_id}/skip")
async def skip_log(log_id: int, via: str = "web"):
    row = svc.skip(log_id, via=via)
    if row is None:
        raise HTTPException(404, "not found")
    return row


@router.post("/meds/confirm-latest")
async def confirm_latest(via: str = "siri"):
    """Siri「薬飲んだ」:确认最近一条 pending。没有 pending 时不报错,返回提示。"""
    pending = svc.list_logs(status="pending", limit=1)
    if not pending:
        return {"ok": False, "message": "いま確認待ちのお薬はないよ"}
    row = svc.confirm(log_id=pending[0]["id"], via=via)
    return {"ok": True, "message": f"服薬確認したよ:{pending[0]['med_name']}", "log": row}
