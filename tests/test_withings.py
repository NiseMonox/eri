from app.ingest.withings import parse_measuregrps

SAMPLE = [
    {  # 正常实测体重:78.5kg(value*10^unit)
        "category": 1, "date": 1755123456,
        "measures": [{"type": 1, "value": 78500, "unit": -3},
                     {"type": 6, "value": 220, "unit": -1}],  # 体脂率,忽略
    },
    {  # category=2 目标值,跳过
        "category": 2, "date": 1755123000,
        "measures": [{"type": 1, "value": 70000, "unit": -3}],
    },
]


def test_parse_measuregrps():
    out = parse_measuregrps(SAMPLE)
    assert out == [{"ts": 1755123456, "kg": 78.5}]


def test_parse_body_metrics():
    from app.ingest.withings import parse_body_metrics

    grps = [{
        "category": 1, "date": 1755123456,
        "measures": [{"type": 1, "value": 78500, "unit": -3},     # 体重,不在此函数输出
                     {"type": 6, "value": 2250, "unit": -2},      # 体脂率 22.5%
                     {"type": 76, "value": 60100, "unit": -3},    # 肌肉量 60.1kg
                     {"type": 999, "value": 5, "unit": 0}],       # 未知 type 也保留
    }]
    out = parse_body_metrics(grps)
    by = {o["metric"]: o for o in out}
    assert by["fat_ratio"]["value"] == 22.5 and by["fat_ratio"]["unit"] == "%"
    assert by["muscle_mass"]["value"] == 60.1
    assert "type_999" in by
    assert "weight" not in by and 1 not in [o["type"] for o in out]


def test_body_metrics_store(fresh_db):
    from datetime import datetime, timezone

    from app.services import body_metrics

    at = datetime(2026, 8, 16, 5, 0, tzinfo=timezone.utc)
    assert body_metrics.add("fat_ratio", 22.5, at, unit="%") is True
    assert body_metrics.add("fat_ratio", 22.5, at, unit="%") is False   # 幂等
    body_metrics.add("muscle_mass", 60.1, at, unit="kg")
    lat = body_metrics.latest()
    assert lat["fat_ratio"]["value"] == 22.5 and lat["muscle_mass"]["value"] == 60.1
    assert "体脂肪率 22.5%" in body_metrics.summary_ja()
    from app import clock

    clock.set_override(datetime(2026, 8, 20, tzinfo=timezone.utc))
    assert len(body_metrics.series("fat_ratio", 30)) == 1
    assert len(body_metrics.series("fat_ratio", 1)) == 0    # 窗口外
