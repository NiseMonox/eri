from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import clock
from ..auth import require_token
from ..services import reminders as svc

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


class ReminderIn(BaseModel):
    title: str
    body: str = ""
    due_at: str                          # ISO(可带时区;无时区按 UTC)


@router.get("/reminders")
async def list_reminders(status: str | None = None, limit: int = 100):
    return svc.list_all(status, limit)


@router.post("/reminders")
async def create_reminder(body: ReminderIn):
    try:
        return svc.create(body.title, body.body, body.due_at)
    except ValueError as e:
        raise HTTPException(400, f"due_at 不是合法 ISO 时间: {e}")


@router.post("/reminders/{rid}/done")
async def done_reminder(rid: int):
    row = svc.set_status(rid, "done")
    if row is None:
        raise HTTPException(404, "not found")
    return row


@router.delete("/reminders/{rid}")
async def delete_reminder(rid: int):
    row = svc.set_status(rid, "dismissed")
    if row is None:
        raise HTTPException(404, "not found")
    return row


@router.get("/reminders/today")
async def today():
    """iOS 快捷指令每早拉这个生成当日提醒事项。"""
    return svc.today_agenda()


class ExternalReminderIn(BaseModel):
    title: str
    due_at: str    # "yyyy-MM-dd HH:mm"(按东京时间)或 ISO


@router.post("/reminders/ingest")
async def ingest_reminder(body: ExternalReminderIn):
    """反向同步:快捷指令把 Apple 提醒事项逐条 POST 进来。同标题±2分钟视为已存在,幂等。"""
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "title 为空")
    try:
        at = clock.parse_flexible_jst(body.due_at)
    except ValueError as e:
        raise HTTPException(400, f"due_at 无法解析: {e}")
    return svc.upsert_external(title, at)
