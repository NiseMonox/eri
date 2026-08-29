"""快捷指令活动推送(/api/ingest/activity 的 ingest 层)+ 汇总函数。"""

from datetime import datetime, timezone

import pytest

from app import clock
from app.ingest import activity
from app.services import body_metrics

FAKE_NOW = datetime(2026, 8, 29, 1, 0, tzinfo=timezone.utc)  # JST 8/29 10:00


def test_ingest_defaults_to_today(fresh_db):
    """晚间自动化上报「今天到目前为止」:date 省略=今天(JST)。"""
    clock.set_override(FAKE_NOW)
    r = activity.ingest({"steps": 8432, "active_kcal": 512.3})
    assert r["date"] == "2026-08-29"
    assert r["saved"] == {"steps": 8432, "active_energy": 512.3}
    a = body_metrics.activity_latest()
    # JST 8/29 0:00 = UTC 8/28 15:00
    assert a["at"] == "2026-08-28T15:00:00Z"
    assert a["metrics"]["steps"]["value"] == 8432
    assert a["metrics"]["steps"]["unit"] == "歩"


def test_trailing_zero_bucket_tolerated(fresh_db):
    """「開始日が今日である+グループ日」实测在真值后跟一个 0 空桶('80\\n0'):容忍;
    两个不同非零数(跨天)仍拒绝。"""
    clock.set_override(FAKE_NOW)
    r = activity.ingest({"steps": "80\n0", "active_kcal": "52.97200000000001\n0"})
    assert r["saved"] == {"steps": 80, "active_energy": 52.97}
    assert r["errors"] == []
    r2 = activity.ingest({"steps": "0\n0"})          # 全零=真的没走路
    assert r2["saved"] == {"steps": 0}
    r3 = activity.ingest({"steps": "23528\n74"})     # 两天的量拼一起,归属不明
    assert r3["saved"] == {}
    assert len(r3["errors"]) == 1


def test_ingest_upsert_same_day(fresh_db):
    clock.set_override(FAKE_NOW)
    activity.ingest({"date": "2026-08-28", "steps": 5000})
    activity.ingest({"date": "2026-08-28", "steps": 8432, "exercise_min": 42})
    a = body_metrics.activity_latest()
    assert a["metrics"]["steps"]["value"] == 8432
    assert a["metrics"]["exercise_time"]["value"] == 42


def test_date_with_time_normalized_to_jst_midnight(fresh_db):
    """date 带时刻也要归一到 JST 当日 0 点——否则同日 upsert 幂等失效。"""
    clock.set_override(FAKE_NOW)
    activity.ingest({"date": "2026-08-28", "steps": 5000, "active_kcal": 400})
    r = activity.ingest({"date": "2026/08/28 22:15", "steps": 8432})
    assert r["date"] == "2026-08-28"
    a = body_metrics.activity_latest()
    assert a["at"] == "2026-08-27T15:00:00Z"
    assert a["metrics"]["steps"]["value"] == 8432          # 覆盖而非新增行
    assert a["metrics"]["active_energy"]["value"] == 400   # 早先指标不丢
    assert "(2日分)" not in body_metrics.activity_week_ja(7)


def test_future_date_rejected(fresh_db):
    clock.set_override(FAKE_NOW)
    with pytest.raises(ValueError, match="未来"):
        activity.ingest({"date": "2027-08-28", "steps": 100})
    activity.ingest({"date": "2026-08-29", "steps": 100})  # 今天(JST)本身 OK


def test_ingest_tolerant_values(fresh_db):
    clock.set_override(FAKE_NOW)
    # 快捷指令把变量当文本传:带千分位、带单位、日期带斜杠
    r = activity.ingest({"date": "2026/08/28", "steps": "8,432 歩", "active_kcal": "512.3 kcal"})
    assert r["saved"] == {"steps": 8432, "active_energy": 512.3}
    assert r["errors"] == []


def test_ingest_rejects_bad(fresh_db):
    clock.set_override(FAKE_NOW)
    r = activity.ingest({"date": "2026-08-28", "steps": 999_999, "active_kcal": "abc", "distance_km": 3.2})
    assert r["saved"] == {"distance": 3.2}
    assert len(r["errors"]) == 2
    with pytest.raises(ValueError, match="読めなかった"):
        activity.ingest({"date": "令和8年", "steps": 100})


def test_ingest_rejects_sneaky_values(fresh_db):
    """指数写法/非标量/非有限数不得静默存成错误值。"""
    clock.set_override(FAKE_NOW)
    r = activity.ingest({"date": "2026-08-28", "steps": "1e9", "active_kcal": {"start": 0, "end": 500},
                         "exercise_min": [42, 7], "distance_km": float("inf")})
    assert r["saved"] == {}
    assert len(r["errors"]) == 4
    # 超长垃圾值在 errors 里被截断
    r2 = activity.ingest({"date": "2026-08-28", "steps": "x" * 5000, "active_kcal": 100})
    assert all(len(e) < 200 for e in r2["errors"])


def test_summaries(fresh_db):
    clock.set_override(FAKE_NOW)
    activity.ingest({"date": "2026-08-27", "steps": 6000, "active_kcal": 400})
    activity.ingest({"date": "2026-08-28", "steps": 9000, "active_kcal": 600, "exercise_min": 42})
    s = body_metrics.activity_summary_ja()
    assert s == "8/28:歩数 9,000歩・消費 600kcal・運動 42分"
    w = body_metrics.activity_week_ja(7)
    assert "歩数 平均7,500歩/日(2日分)" in w
    assert "消費 平均500kcal/日" in w


def test_activity_excluded_from_body_metrics(fresh_db):
    clock.set_override(FAKE_NOW)
    activity.ingest({"date": "2026-08-28", "steps": 9000})
    body_metrics.add("fat_ratio", 22.5, "2026-08-27T22:00:00Z", unit="%")
    assert "steps" not in body_metrics.latest()            # 活动指标不占体成分名额
    assert "fat_ratio" in body_metrics.latest()
    assert "歩" not in body_metrics.summary_ja()           # 体成分摘要不混入步数
