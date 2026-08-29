"""iPhone 快捷指令推送的每日活动汇总(Apple Watch 步数/卡路里等)入库。
Body: {"date": "2026-08-29"(JST 日期,省略=今天), "steps": 8432, "active_kcal": 512.3,
       "exercise_min": 42, "distance_km": 5.2} —— 指标全部可选,但至少要有一个。
快捷指令按「開始日が今日である」检索、晚间自动化上报「今天到目前为止」;
同一天重复上报覆盖更新(取最后一次),所以自动化一天跑几次都无所谓。"""

import math
import re

from .. import clock, events
from ..services import body_metrics

# 输入字段 → (metric 名, 单位, 下限, 上限)
FIELDS = {
    "steps": ("steps", "歩", 0, 200_000),
    "active_kcal": ("active_energy", "kcal", 0, 20_000),
    "exercise_min": ("exercise_time", "分", 0, 1_440),
    "distance_km": ("distance", "km", 0, 500),
}

_NUM_TOKEN = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def _to_float(v) -> float:
    """快捷指令的变量可能是数值,也可能是「8,432 歩」这类带单位文本。
    健康サンプル検索「今日である+グループ日」会在真值后面跟一个 0 的空桶
    (实测 '80\\n0'),零值桶无信息量、任意位置都容忍;但出现两个不同的
    非零数(如昨天+今天两天的量)说明归属日期不明,整体拒绝。
    "1e9"/"5.2e3" 指数写法会被拆成两个数落进同一条拒绝逻辑,不会错存。"""
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError("not a number")
    if isinstance(v, (int, float)):
        if not math.isfinite(v):
            raise ValueError("not finite")
        return float(v)
    nums = [float(t.replace(",", "")) for t in _NUM_TOKEN.findall(v)]
    nonzero = [n for n in nums if n != 0]
    if len(nonzero) == 1:
        return nonzero[0]
    if nums and not nonzero:
        return 0.0
    raise ValueError("not a single number")


def ingest(payload: dict) -> dict:
    """入库并返回 {date, saved, errors}。date 非法/未来时抛 ValueError(日语文案,router 转 400)。"""
    date = str(payload.get("date") or "").strip()
    if not date:
        date = clock.now_local().strftime("%Y-%m-%d")
    try:
        # 传来的无论是纯日期还是带时刻,一律归一到 JST 当日 → 0 点锚点,保证同日 upsert 幂等
        day = clock.to_local(clock.parse_iso(clock.parse_flexible_jst(date))).strftime("%Y-%m-%d")
    except ValueError as e:
        raise ValueError("date が読めなかったよ(例: 2026-08-28)") from e
    if day > clock.now_local().strftime("%Y-%m-%d"):
        raise ValueError(f"{day} は未来の日付だよ")
    at = clock.parse_flexible_jst(day)
    saved, errors = {}, []
    for field, (metric, unit, lo, hi) in FIELDS.items():
        v = payload.get(field)
        if v is None or v == "":
            continue
        try:
            val = round(_to_float(v), 2)
        except ValueError:
            errors.append(f"{field}: 数値として読めないよ({repr(v)[:80]})")
            continue
        if not lo <= val <= hi:
            errors.append(f"{field}={val} は範囲外だよ")
            continue
        body_metrics.upsert(metric, val, at, unit=unit, source="shortcuts")
        saved[metric] = val
    if errors:
        events.log("ingest_error", {"source": "shortcuts", "errors": errors[:5]})
    events.log("ingest", {"source": "shortcuts", "date": day, "saved": list(saved)})
    return {"date": day, "saved": saved, "errors": errors}
