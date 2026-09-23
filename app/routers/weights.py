from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from .. import charts, clock
from ..auth import require_token
from ..services import weights as svc

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


class WeightIn(BaseModel):
    weight_kg: float
    measured_at: str | None = None      # ISO;缺省=现在
    source: str = "manual"


@router.post("/weights")
async def add_weight(body: WeightIn):
    try:
        return svc.add_weight(body.weight_kg, body.measured_at, source=body.source)
    except svc.InvalidWeight as e:
        raise HTTPException(400, str(e))


@router.get("/weights")
async def list_weights(from_: str | None = None, to: str | None = None, limit: int = 200):
    return svc.list_weights(from_, to, limit)


@router.get("/weights/series")
async def weight_series(days: int = 30):
    """Chart.js 用:[{t: 东京时间, kg}]。"""
    rows = svc.recent_days(days)
    return [
        {"t": clock.to_local(clock.parse_iso(r["measured_at"])).strftime("%Y-%m-%d %H:%M"),
         "kg": r["weight_kg"]}
        for r in rows
    ]


@router.get("/weights/stats")
async def weight_stats(days: int = 7):
    return svc.stats(days) or {}


@router.get("/weights/today")
async def weights_today():
    """给「体重→Apple 健康」快捷指令:只回今天手动记的(网页/Telegram/Siri/语音入口…)。
    Withings/HAE 来源不含在内——那两路的健康写入由各自官方 App 负责,避免健康里重复。
    用排除法:手动的渠道会越加越多(voice-pc、voice-ios…),白名单会漏。"""
    rows = svc.recent_days(1)
    today = clock.now_local().date()
    out = []
    for r in rows:
        local = clock.to_local(clock.parse_iso(r["measured_at"]))
        if local.date() == today and r["source"] not in ("withings", "hae"):
            out.append({"kg": r["weight_kg"], "when": local.strftime("%Y-%m-%d %H:%M")})
    return out


@router.get("/body")
async def body_latest():
    """各体成分指标的最新值(Withings 全量数据)。"""
    from ..services import body_metrics

    return body_metrics.latest()


@router.get("/body/series")
async def body_series(metric: str, days: int = 90):
    from ..services import body_metrics

    rows = body_metrics.series(metric, days)
    return [{"t": clock.to_local(clock.parse_iso(r["measured_at"])).strftime("%Y-%m-%d %H:%M"),
             "v": r["value"]} for r in rows]


@router.get("/weights/chart.png")
async def weight_chart(days: int = 30):
    return Response(content=charts.weight_chart_png(days), media_type="image/png")


@router.delete("/weights/{wid}")
async def delete_weight(wid: int):
    if not svc.delete(wid):
        raise HTTPException(404, "not found")
    return {"deleted": wid}
