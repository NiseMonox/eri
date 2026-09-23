"""手机通知里点开的短链:无 API token,一次性/会话 token 即凭证。返回单屏结果页(templates/status.html)。"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from .. import clock
from ..audio.manager import audio_manager
from ..services import routines
from .pages import templates

router = APIRouter()

_STATUS_JA = {"pending": "予定", "notified": "確認待ち", "done": "完了", "dismissed": "スキップ", "missed": "未実施"}


def status_page(kind: str, title: str, sub: str = "", status_code: int = 200) -> HTMLResponse:
    """kind: ok | info | bad | mute(决定图标)。Withings 回跳也用这个。"""
    html = templates.get_template("status.html").render(kind=kind, title=title, sub=sub)
    return HTMLResponse(html, status_code=status_code)


@router.get("/c/r/{token}")
@router.get("/c/med/{token}")   # 旧路径别名
async def confirm_routine(token: str):
    inst = routines.get_instance_by_token(token)
    if inst is None:
        return status_page("bad", "無効なリンクだよ")
    if inst["status"] in ("pending", "notified", "missed"):
        row = routines.complete(inst["id"], via="bark")
        t = clock.fmt_local(row["done_at"], "%H:%M")
        return status_page("ok", f"{row['title']}、完了にしたよ {t}")
    if inst["status"] == "done":
        return status_page("ok", f"もう完了済みだよ({clock.fmt_local(inst['done_at'], '%H:%M')})")
    return status_page("info", f"この記録の状態:{_STATUS_JA.get(inst['status'], inst['status'])}",
                       "変更は画面からどうぞ")


@router.get("/s/stop")
async def stop_audio(t: str = ""):
    if not t:
        # 无 token 不给强停:强停走登录后的网页按钮 / 带 API token 的 /api/audio/stop
        return status_page("bad", "停止トークンがないよ", "通知から開くか、画面から操作してね")
    stopped = await audio_manager.stop(token=t)
    if stopped:
        return status_page("mute", "再生を止めたよ")
    return status_page("info", "いま何も再生してないよ", "もう止まったか、自動終了したかも")
