from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import clock, events, store
from ..auth import require_token
from ..notify.service import notify

router = APIRouter()


@router.get("/api/health")
async def health():
    return {"ok": True, "time": clock.now_iso()}


class NotifyTest(BaseModel):
    profile: str = "info"
    channel: str = "both"


@router.post("/api/notify/test", dependencies=[Depends(require_token)])
async def notify_test(body: NotifyTest):
    sent = await notify(body.profile, "エリのテスト通知",
                        f"profile={body.profile} channel={body.channel}", channel=body.channel)
    return {"sent": sent}


@router.get("/api/settings", dependencies=[Depends(require_token)])
async def get_settings():
    return store.editable()


@router.put("/api/settings", dependencies=[Depends(require_token)])
async def put_settings(body: dict):
    unknown = set(body) - set(store.EDITABLE_KEYS)
    if unknown:
        return {"error": f"unknown keys: {sorted(unknown)}", "allowed": sorted(store.EDITABLE_KEYS)}
    for k, v in body.items():
        store.set(k, v)
    return store.editable()


@router.get("/api/events", dependencies=[Depends(require_token)])
async def list_events(limit: int = 50, kind: str | None = None):
    return events.recent(limit=limit, kind_prefix=kind)
