"""Withings Public API:OAuth2 + getmeas 轮询(家庭 NAT,不做 webhook)。
token 存 settings 表 withings.oauth(会被自动刷新回写,只能放 DB 不能放 .env)。"""

import time

import httpx

from .. import clock, events, store
from ..config import settings
from ..services import weights

AUTH_URL = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
MEASURE_URL = "https://wbsapi.withings.net/measure"
SCOPE = "user.metrics"


def configured() -> bool:
    return bool(settings.withings_client_id and settings.withings_client_secret)


def connected() -> bool:
    return bool(store.get("withings.oauth"))


def redirect_uri() -> str:
    # Withings 后台大概率拒收私网 http 回调:注册公网 https 地址填 WITHINGS_REDIRECT_URI,
    # 授权跳转后从 URL 复制 code 走 POST /api/withings/exchange 兜底
    return settings.withings_redirect_uri or f"{settings.base_url}/api/withings/callback"


def authorize_url(state: str) -> str:
    return (
        f"{AUTH_URL}?response_type=code&client_id={settings.withings_client_id}"
        f"&scope={SCOPE}&redirect_uri={redirect_uri()}&state={state}"
    )


async def _token_request(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(TOKEN_URL, data={"action": "requesttoken", **payload})
        r.raise_for_status()
        data = r.json()
    if data.get("status") != 0:
        raise RuntimeError(f"Withings token 接口返回 status={data.get('status')}: {data}")
    body = data["body"]
    store.set("withings.oauth", {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_at": int(time.time()) + int(body.get("expires_in", 10800)) - 60,
    })
    return body


async def exchange_code(code: str) -> dict:
    return await _token_request({
        "grant_type": "authorization_code",
        "client_id": settings.withings_client_id,
        "client_secret": settings.withings_client_secret,
        "code": code,
        "redirect_uri": redirect_uri(),
    })


async def _access_token() -> str:
    oauth = store.get("withings.oauth")
    if not oauth:
        raise RuntimeError("Withings 未授权:先访问 /api/withings/authorize")
    if int(time.time()) >= oauth["expires_at"]:
        body = await _token_request({
            "grant_type": "refresh_token",
            "client_id": settings.withings_client_id,
            "client_secret": settings.withings_client_secret,
            "refresh_token": oauth["refresh_token"],
        })
        return body["access_token"]
    return oauth["access_token"]


def parse_measuregrps(grps: list) -> list[dict]:
    """getmeas 的 measuregrps → [{measured_at_ts, kg}](type=1 体重,category=1 实测)。"""
    out = []
    for g in grps:
        if g.get("category") != 1:
            continue
        for m in g.get("measures", []):
            if m.get("type") == 1:
                kg = m["value"] * (10 ** m["unit"])
                out.append({"ts": g["date"], "kg": round(kg, 2)})
    return out


def parse_body_metrics(grps: list) -> list[dict]:
    """非体重指标(体脂/肌肉/心率/血管年龄…)→ [{ts, metric, value, unit, type}]。
    未知 type 也保留(metric=type_N),将来对照文档补名字即可,数据不丢。"""
    from ..services.body_metrics import WITHINGS_TYPES

    out = []
    for g in grps:
        if g.get("category") != 1:
            continue
        for m in g.get("measures", []):
            t = m.get("type")
            if t == 1:
                continue
            name, unit = WITHINGS_TYPES.get(t, (f"type_{t}", ""))
            out.append({"ts": g["date"], "metric": name, "unit": unit, "type": t,
                        "value": round(m["value"] * (10 ** m["unit"]), 3)})
    return out


async def _getmeas_page(token: str, lastupdate: int, offset: int | None) -> dict:
    # 不带 meastypes:全量拉取秤给的所有指标(体重之外的进 body_metrics 表)
    payload = {"action": "getmeas", "category": "1", "lastupdate": lastupdate or 1}
    if offset is not None:
        payload["offset"] = offset
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(MEASURE_URL, headers={"Authorization": f"Bearer {token}"},
                              data=payload)
        r.raise_for_status()
        return r.json()


async def poll() -> dict:
    """拉取新增体重入库。带 more/offset 分页(首次全量同步历史必须);
    游标 = 本轮实收分组的 max(modified),全部入库后才推进——中途失败下轮从旧游标重拉,幂等去重兜底。"""
    token = await _access_token()
    last = int(store.get("withings.last_sync", 0) or 0)

    grps: list = []
    offset: int | None = None
    refreshed = False
    while True:
        data = await _getmeas_page(token, last, offset)
        if data.get("status") == 401 and not refreshed:
            # access token 失效但本地未过期(如授权被撤销后重授权):强制 refresh 重试一次
            store.set("withings.oauth", {**store.get("withings.oauth"), "expires_at": 0})
            token = await _access_token()
            refreshed = True
            continue
        if data.get("status") != 0:
            raise RuntimeError(f"getmeas status={data.get('status')}: {str(data)[:200]}")
        body = data.get("body", {})
        grps += body.get("measuregrps", [])
        if body.get("more") and body.get("offset") is not None:
            offset = body["offset"]
        else:
            break

    created = 0
    metrics_created = 0
    from datetime import datetime, timezone

    from ..services import body_metrics

    for p in parse_measuregrps(grps):
        at = datetime.fromtimestamp(p["ts"], tz=timezone.utc)
        try:
            r2 = weights.add_weight(p["kg"], measured_at=at, source="withings", raw=p)
            if r2["created"]:
                created += 1
        except weights.InvalidWeight:
            continue
    for p in parse_body_metrics(grps):
        at = datetime.fromtimestamp(p["ts"], tz=timezone.utc)
        if body_metrics.add(p["metric"], p["value"], at, unit=p["unit"], raw=p):
            metrics_created += 1

    if grps:
        max_modified = max(int(g.get("modified") or g.get("date") or 0) for g in grps)
        if max_modified > last:
            store.set("withings.last_sync", max_modified)
    # 没有新数据:游标原样保留(绝不用本地时钟,快钟会跳过数据)
    events.log("ingest", {"source": "withings", "created": created,
                          "metrics_created": metrics_created, "groups": len(grps)})
    return {"created": created, "metrics_created": metrics_created, "groups": len(grps)}


async def poll_if_due(force: bool = False) -> None:
    """内部 job 每 5 分钟跑一次;按 settings 的间隔与上次时间决定是否真的拉。
    force=True 不看间隔马上拉:体重提醒/追催发出前,先把刚上秤的那条同步进来。"""
    if not (configured() and connected()):
        return
    interval_min = int(store.get("withings.poll_minutes", 15) or 15)
    last_at = store.get("withings.last_poll_at", 0)
    if not force and time.time() - last_at < interval_min * 60:
        return
    store.set("withings.last_poll_at", int(time.time()))
    try:
        await poll()
    except Exception as e:  # noqa: BLE001
        events.log("ingest_error", {"source": "withings", "error": str(e)[:300]})
