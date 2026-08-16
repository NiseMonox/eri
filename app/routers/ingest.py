from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_token
from ..ingest import hae

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


@router.post("/ingest/hae")
async def ingest_hae(body: dict):
    """Health Auto Export 的 REST 推送目标(App 里把 URL 配成
    http://<server>:8300/api/ingest/hae?token=... )。"""
    return hae.ingest(body)


class TextIn(BaseModel):
    text: str


@router.post("/ingest/text")
async def ingest_text(body: TextIn):
    """Siri 听写入口:自由文本 → 与 Telegram 同一个对话大脑。返回 reply 供快捷指令读出。"""
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "text 为空")
    from ..services import conversation

    res = await conversation.handle(text, via="siri")
    return {"ok": res["ok"], "reply": res["reply"]}
