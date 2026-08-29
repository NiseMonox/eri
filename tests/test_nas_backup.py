"""NAS 快照推送:CSV 导出与未配置时的静默跳过(smb 推送本体在真机验证)。"""

import asyncio

from app.services import nas_backup, weights


def test_export_csvs_jst_and_bom(fresh_db, tmp_path):
    weights.add_weight(62.5, measured_at="2026-08-28T22:30:00Z", source="withings")  # JST 8/29 7:30
    from app.services import body_metrics

    body_metrics.upsert("steps", 8432, "2026-08-28T15:00:00Z", unit="歩")
    files = nas_backup.export_csvs(tmp_path)
    assert [f.name for f in files] == ["weights.csv", "body_metrics.csv"]
    w = (tmp_path / "weights.csv").read_bytes()
    assert w.startswith(b"\xef\xbb\xbf")                      # UTF-8 BOM,Excel 直读
    assert "2026-08-29 07:30,62.5,withings" in w.decode("utf-8-sig")
    b = (tmp_path / "body_metrics.csv").read_text("utf-8-sig")
    assert "2026-08-29 00:00,steps,8432.0,歩,shortcuts" in b


def test_push_skipped_when_unconfigured(fresh_db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "nas_smb_host", "")
    assert asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        nas_backup.push()) is None
