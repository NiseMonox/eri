import pytest

from app.services import weights


def test_add_and_dedupe(fresh_db):
    r1 = weights.add_weight(62.5, "2026-08-14T12:00:00Z", source="hae")
    assert r1["created"] is True
    r2 = weights.add_weight(62.5, "2026-08-14T12:00:00Z", source="hae")
    assert r2["created"] is False
    assert len(weights.list_weights()) == 1
    # 同时刻不同来源不算重复
    r3 = weights.add_weight(62.5, "2026-08-14T12:00:00Z", source="manual")
    assert r3["created"] is True


def test_range_validation(fresh_db):
    with pytest.raises(weights.InvalidWeight):
        weights.add_weight(10)
    with pytest.raises(weights.InvalidWeight):
        weights.add_weight(500)


def test_stats(fresh_db):
    weights.add_weight(62.0, "2026-08-14T00:00:00Z")
    weights.add_weight(63.0, "2026-08-14T12:00:00Z")
    from datetime import datetime, timezone

    from app import clock

    clock.set_override(datetime(2026, 8, 15, tzinfo=timezone.utc))
    st = weights.stats(7)
    assert st["count"] == 2
    assert st["delta"] == 1.0
    assert st["avg"] == 62.5
