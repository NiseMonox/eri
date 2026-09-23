"""语音入口的 HTTP 接口(Windows / iOS 客户端用)。约定见 docs/voice/eri-voice-api.md,改接口先改那份文档。"""

import re

from fastapi import APIRouter, Depends, HTTPException, Request

from ..audio import stt, tts
from ..auth import require_token
from ..services import voice

router = APIRouter(prefix="/api/voice")

MAX_BODY = 16 * 1024 * 1024
RE_DEVICE = re.compile(r"[a-z0-9]{1,12}")
TRIGGERS = ("ptt", "intent", "wake")


@router.post("/turn")
async def turn(request: Request):
    """multipart:file(必填)、device、speak、trigger。
    故意不声明 File/Form 参数:FastAPI 会先把整个上传解析完才跑依赖(鉴权),所以这里手动按
    「鉴权 → 看 Content-Length → 解析表单」的顺序来。识别失败也回 200(ok=false + error),客户端统一处理。"""
    require_token(request)
    try:
        length = int(request.headers.get("content-length") or 0)
    except ValueError:
        length = 0
    if length > MAX_BODY:
        raise HTTPException(413, "too large")
    async with request.form(max_files=1, max_fields=8) as form:
        f = form.get("file")
        if f is None or isinstance(f, str):
            raise HTTPException(400, "file がないよ")
        data = await f.read(MAX_BODY + 1)
        filename, ctype = f.filename or "audio", f.content_type
        device = str(form.get("device") or "app").strip().lower()
        speak = str(form.get("speak", "true")).strip().lower() not in ("false", "0", "no", "off")
        trigger = str(form.get("trigger") or "ptt").strip().lower()
    if len(data) > MAX_BODY:
        raise HTTPException(413, "too large")
    if not RE_DEVICE.fullmatch(device):
        device = "app"
    if trigger not in TRIGGERS:
        trigger = "ptt"
    res = await voice.turn(data, source=device, filename=filename, content_type=ctype,
                           speak=speak, trigger=trigger)
    return {"ok": res["ok"], "heard": res["heard"], "reply": res["reply"], "spoken": res["spoken"],
            "language": res["language"], "error": res["error"], "has_photo": res["photo"] is not None}


@router.get("/ping", dependencies=[Depends(require_token)])
async def ping():
    """客户端设置页的「接続テスト」:token 对不对、识别和语音合成在不在。"""
    return {"ok": True, "stt": await stt.alive(), "tts": tts.enabled() and await tts.engine_alive()}
