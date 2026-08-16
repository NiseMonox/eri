import secrets

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from .config import settings

COOKIE_NAME = "hh_token"


def _extract(request: Request) -> str | None:
    return (
        request.headers.get("X-Token")
        or request.query_params.get("token")
        or request.cookies.get(COOKIE_NAME)
    )


def token_ok(t: str | None) -> bool:
    # bytes 比较:含非 ASCII 的输入不会让 compare_digest 抛 TypeError
    return bool(t) and secrets.compare_digest(t.encode(), settings.api_token.encode())


def require_token(request: Request) -> None:
    """API 鉴权:header / query / cookie 三选一。"""
    if not token_ok(_extract(request)):
        raise HTTPException(status_code=401, detail="unauthorized")


def require_page_login(request: Request):
    """页面鉴权:未登录 302 到 /login。在页面路由里手动调用并检查返回值。"""
    if not token_ok(request.cookies.get(COOKIE_NAME)):
        return RedirectResponse("/login", status_code=302)
    return None
