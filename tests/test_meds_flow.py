"""服药闭环状态机:pending → resend×N → missed;确认幂等。用假 clock 快进。"""

from datetime import datetime, timedelta, timezone

from app import clock
from app.services import meds

T0 = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def _advance(minutes):
    clock.set_override(T0 + timedelta(minutes=minutes))


def test_full_flow_to_missed(fresh_db):
    clock.set_override(T0)
    med = meds.create_med("测试药", "10mg")
    log = meds.create_due_log(med["id"], params={"resend_every_min": 30, "max_resends": 3,
                                                 "grace_min": 180})
    assert log["status"] == "pending"

    # 未到 30 分钟:无动作
    _advance(10)
    assert meds.sweep() == []

    # 30 分钟:第 1 次重发
    _advance(31)
    actions = meds.sweep()
    assert len(actions) == 1 and actions[0][1] == "resend"
    assert actions[0][0]["remind_count"] == 1

    # 同一时刻再 sweep:不重复发
    assert meds.sweep() == []

    _advance(61)
    assert meds.sweep()[0][1] == "resend"
    _advance(91)
    assert meds.sweep()[0][1] == "resend"

    # 已达 max_resends=3:不再重发
    _advance(121)
    assert meds.sweep() == []

    # 超过 grace_min=180:missed
    _advance(181)
    actions = meds.sweep()
    assert actions[0][1] == "missed"
    assert meds.get_log(log["id"])["status"] == "missed"

    # missed 可补确认(补服),并记录 late
    got = meds.confirm(log_id=log["id"], via="web")
    assert got["status"] == "confirmed"
    # 补确认后不再出现在漏服视图
    assert meds.list_logs(status="missed") == []


def test_confirm_idempotent(fresh_db):
    clock.set_override(T0)
    med = meds.create_med("A")
    log = meds.create_due_log(med["id"])
    got = meds.confirm(token=log["token"], via="bark")
    assert got["status"] == "confirmed" and got["confirm_via"] == "bark"
    # 第二次确认(换渠道)不覆盖
    again = meds.confirm(log_id=log["id"], via="web")
    assert again["confirm_via"] == "bark"
    # 确认后 sweep 无动作
    _advance(500)
    assert meds.sweep() == []


def test_skip(fresh_db):
    clock.set_override(T0)
    med = meds.create_med("B")
    log = meds.create_due_log(med["id"])
    assert meds.skip(log["id"])["status"] == "skipped"
    # skipped 不可再确认(与 missed 不同)
    assert meds.confirm(log_id=log["id"])["status"] == "skipped"
    _advance(500)
    assert meds.sweep() == []


def test_downtime_no_resend_burst(fresh_db):
    """停机 2 小时后恢复:remind_count 直接跳位,不会 5 分钟一条连环补发。"""
    clock.set_override(T0)
    med = meds.create_med("D")
    meds.create_due_log(med["id"], params={"resend_every_min": 30, "max_resends": 3,
                                           "grace_min": 300})
    _advance(120)  # 停机后第一次 sweep
    actions = meds.sweep()
    assert len(actions) == 1 and actions[0][1] == "resend"
    assert actions[0][0]["remind_count"] == 3      # 直接跳到上限
    assert actions[0][0]["med_dose"] is None or "med_dose" in actions[0][0]
    _advance(125)  # 5 分钟后:已到上限,不再发
    assert meds.sweep() == []


def test_params_snapshot_from_payload(fresh_db):
    """oneshot/schedule payload 的重发参数进快照,压缩测试用。"""
    clock.set_override(T0)
    med = meds.create_med("C")
    meds.create_due_log(med["id"], params={"resend_every_min": 1, "max_resends": 1,
                                           "grace_min": 3})
    _advance(1)
    assert meds.sweep()[0][1] == "resend"
    _advance(2)
    assert meds.sweep() == []
    _advance(4)
    assert meds.sweep()[0][1] == "missed"
