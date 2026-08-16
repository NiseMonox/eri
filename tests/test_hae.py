from app.ingest import hae
from app.services import weights

SAMPLE = {
    "data": {
        "metrics": [
            {
                "name": "weight_body_mass",
                "units": "kg",
                "data": [
                    {"date": "2026-08-14 07:01:00 +0900", "qty": 62.5},
                    {"date": "2026-08-13 07:03:00 +0900", "qty": 62.8},
                ],
            },
            {"name": "step_count", "units": "count", "data": [{"date": "2026-08-14 00:00:00 +0900", "qty": 8000}]},
        ]
    }
}


def test_ingest_and_dedupe(fresh_db):
    r = hae.ingest(SAMPLE)
    assert r == {"received": 2, "created": 2}
    # 同批再推:幂等
    r2 = hae.ingest(SAMPLE)
    assert r2 == {"received": 2, "created": 0}
    rows = weights.list_weights()
    assert len(rows) == 2
    assert all(w["source"] == "hae" for w in rows)
    # +0900 正确转 UTC
    assert rows[0]["measured_at"] == "2026-08-13T22:01:00Z"


def test_lb_conversion(fresh_db):
    payload = {"data": {"metrics": [{"name": "weight_body_mass", "units": "lb",
                                     "data": [{"date": "2026-08-14 07:00:00 +0900", "qty": 137.8}]}]}}
    hae.ingest(payload)
    row = weights.list_weights()[0]
    assert abs(row["weight_kg"] - 62.5) < 0.1


def test_bad_data_skipped(fresh_db):
    payload = {"data": {"metrics": [{"name": "weight_body_mass", "units": "kg",
                                     "data": [{"date": "invalid", "qty": 62.5},
                                              {"date": "2026-08-14 07:00:00 +0900"}]}]}}
    r = hae.ingest(payload)
    assert r["created"] == 0
