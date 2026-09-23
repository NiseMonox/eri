import secrets as pysecrets

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from .. import store
from ..auth import require_token
from ..ingest import withings
from .callbacks import status_page

router = APIRouter()


@router.get("/api/withings/authorize", dependencies=[Depends(require_token)])
async def authorize():
    if not withings.configured():
        raise HTTPException(400, ".env 里先填 WITHINGS_CLIENT_ID / WITHINGS_CLIENT_SECRET")
    state = pysecrets.token_urlsafe(16)
    store.set("withings.oauth_state", state)
    return RedirectResponse(withings.authorize_url(state))


@router.get("/api/withings/callback")
async def callback(code: str = "", state: str = ""):
    """Withings 授权后的回跳。LAN 内 iPhone/电脑浏览器发起授权时可直达。"""
    if not code:
        return status_page("bad", "缺少 code", status_code=400)
    if state != store.get("withings.oauth_state"):
        return status_page("bad", "state 不匹配,重新发起授权", status_code=400)
    await withings.exchange_code(code)
    return status_page("ok", "Withings 已连接,体重会自动同步")


class CodeIn(BaseModel):
    code: str


@router.post("/api/withings/exchange", dependencies=[Depends(require_token)])
async def exchange(body: CodeIn):
    """回跳打不通时的兜底:从跳转 URL 里手动复制 code 贴进来。"""
    await withings.exchange_code(body.code)
    return {"connected": True}


@router.post("/api/withings/poll", dependencies=[Depends(require_token)])
async def poll_now():
    if not withings.connected():
        raise HTTPException(400, "尚未授权,先访问 /api/withings/authorize")
    return await withings.poll()


@router.get("/api/withings/status", dependencies=[Depends(require_token)])
async def status():
    return {
        "configured": withings.configured(),
        "connected": withings.connected(),
        "last_sync": store.get("withings.last_sync", 0),
        "poll_minutes": store.get("withings.poll_minutes", 15),
    }
