from fastapi import APIRouter, Depends, HTTPException

from ..audio.manager import audio_manager
from ..auth import require_token

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


@router.post("/audio/play")
async def play(body: dict):
    """body 同 audio 类型 schedule 的 payload,如 {"action":"white_noise","volume":40}。"""
    try:
        session = await audio_manager.start(body)
    except Exception as e:  # noqa: BLE001 — 设备未直通等情况直接把原因给前端
        raise HTTPException(500, f"播放失败: {e}")
    return {k: v for k, v in session.items() if k != "token"}


@router.post("/audio/stop")
async def stop():
    stopped = await audio_manager.stop()
    return {"stopped": stopped}


@router.post("/audio/volume")
async def set_volume(body: dict):
    try:
        status = await audio_manager.set_volume(int(body.get("volume", -1)))
    except (TypeError, ValueError):
        raise HTTPException(400, "volume 需要 0-100 的整数")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"调音量失败: {e}")
    if status is None:
        return {"playing": False, "message": "当前没有在播放"}
    return status


@router.get("/audio/status")
async def status():
    return audio_manager.status()


@router.post("/tts/test")
async def tts_test(body: dict):
    from ..audio import tts

    text = (body.get("text") or "こんにちは、エリだよ。テスト成功!").strip()
    ok = await tts.announce(text, force=True)
    return {"ok": ok, "engine_alive": await tts.engine_alive()}
