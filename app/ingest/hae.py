"""Health Auto Export(iOS App)REST 推送的解析入库。
格式: {"data": {"metrics": [{"name": "weight_body_mass", "units": "kg",
        "data": [{"date": "2026-08-14 07:01:00 +0900", "qty": 62.5}, ...]}]}}"""

from datetime import datetime

from .. import events
from ..services import weights

LB_TO_KG = 0.45359237


def ingest(payload: dict) -> dict:
    received = created = 0
    metrics = (payload.get("data") or {}).get("metrics") or []
    for metric in metrics:
        if metric.get("name") != "weight_body_mass":
            continue
        units = (metric.get("units") or "kg").lower()
        for point in metric.get("data") or []:
            qty = point.get("qty")
            date = point.get("date")
            if qty is None or not date:
                continue
            received += 1
            kg = float(qty) * LB_TO_KG if units in ("lb", "lbs") else float(qty)
            try:
                at = datetime.strptime(date, "%Y-%m-%d %H:%M:%S %z")
            except ValueError:
                events.log("ingest_error", {"source": "hae", "bad_date": date})
                continue
            try:
                r = weights.add_weight(round(kg, 2), measured_at=at, source="hae", raw=point)
                if r["created"]:
                    created += 1
            except weights.InvalidWeight as e:
                events.log("ingest_error", {"source": "hae", "error": str(e)})
    events.log("ingest", {"source": "hae", "received": received, "created": created})
    return {"received": received, "created": created}
