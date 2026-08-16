"""手机通知里点开的短链:无 API token,一次性/会话 token 即凭证。返回大字 HTML。"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from .. import clock
from ..audio.manager import audio_manager
from ..services import meds

router = APIRouter()

_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>health-hub</title>
<style>
body{{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#f9f9f7;color:#0b0b0b;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
@media (prefers-color-scheme:dark){{body{{background:#0d0d0d;color:#fff}}}}
.card{{text-align:center;padding:2rem}}
.icon{{font-size:64px}}
h1{{font-size:1.6rem;margin:.5rem 0}}
p{{color:#898781}}
</style></head><body><div class="card"><div class="icon">{icon}</div><h1>{title}</h1><p>{sub}</p></div></body></html>"""


def _page(icon: str, title: str, sub: str = "") -> HTMLResponse:
    return HTMLResponse(_PAGE.format(icon=icon, title=title, sub=sub))


@router.get("/c/med/{token}")
async def confirm_med(token: str):
    log = meds.get_log_by_token(token)
    if log is None:
        return _page("❓", "無効なリンクだよ")
    if log["status"] == "pending":
        log = meds.confirm(token=token, via="bark")
        t = clock.fmt_local(log["confirmed_at"], "%H:%M")
        return _page("✅", f"服薬確認したよ {t}")
    if log["status"] == "confirmed":
        return _page("✅", f"確認済みだよ({clock.fmt_local(log['confirmed_at'], '%H:%M')})")
    return _page("ℹ️", f"この記録の状態:{log['status']}", "後追い確認は画面からどうぞ")


@router.get("/s/stop")
async def stop_audio(t: str = ""):
    if not t:
        # 无 token 不给强停:强停走登录后的网页按钮 / 带 API token 的 /api/audio/stop
        return _page("❓", "停止トークンがないよ", "通知から開くか、画面から操作してね")
    stopped = await audio_manager.stop(token=t)
    if stopped:
        return _page("🔇", "再生を止めたよ")
    return _page("ℹ️", "いま何も再生してないよ", "もう止まったか、自動終了したかも")
