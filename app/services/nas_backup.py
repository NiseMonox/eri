"""每日把健康数据快照推送到 NAS(NAS 共享里的 Health 目录)。
非特权 LXC 挂不了 CIFS,走 smbclient 用户态推送(apt install smbclient),无需挂载。
凭据在 .env(NAS_SMB_*),host 留空=功能整体关闭。fail-soft:NAS 不在线只记事件,
第二天推的永远是最新全量,不需要补传。"""

import asyncio
import csv
import os
from pathlib import Path

from .. import clock, db
from ..config import settings

# 导出内容:全量 CSV(JST 时刻,UTF-8 BOM 让 Excel 直接识别)
_CSV_SPECS = {
    "weights.csv": (
        "SELECT measured_at, weight_kg, source FROM weights ORDER BY measured_at",
        ["日時(JST)", "体重kg", "ソース"],
    ),
    "body_metrics.csv": (
        "SELECT measured_at, metric, value, unit, source FROM body_metrics "
        "ORDER BY measured_at, metric",
        ["日時(JST)", "指標", "値", "単位", "ソース"],
    ),
}


def configured() -> bool:
    return bool(settings.nas_smb_host and settings.nas_smb_share and settings.nas_smb_user)


def export_csvs(dest_dir: Path) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    conn = db.get_db()
    out = []
    for name, (sql, header) in _CSV_SPECS.items():
        p = dest_dir / name
        with p.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(header)
            for row in conn.execute(sql):
                vals = list(row)
                vals[0] = clock.fmt_local(vals[0], "%Y-%m-%d %H:%M")
                w.writerow(vals)
        out.append(p)
    return out


async def _smb(commands: str, timeout: int = 120) -> tuple[int, str]:
    env = dict(os.environ, PASSWD=settings.nas_smb_pass)
    proc = await asyncio.create_subprocess_exec(
        "smbclient", f"//{settings.nas_smb_host}/{settings.nas_smb_share}",
        "-U", settings.nas_smb_user, "-c", commands,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return 124, "smbclient timeout"
    return proc.returncode or 0, (out or b"").decode(errors="replace")


async def push() -> dict | None:
    """DB 每日备份 + CSV 全量 → NAS。未配置返回 None(调用方静默跳过)。"""
    if not configured():
        return None
    backup_path = db.backup(settings.db_path.parent / "backups")
    files = [backup_path, *export_csvs(settings.db_path.parent / "exports")]
    d = settings.nas_smb_dir.strip("/") or "Health"
    # mkdir 单独跑且忽略结果(已存在会报错但无害),避免污染主命令的退出码
    await _smb(f'mkdir "{d}"', timeout=30)
    puts = "; ".join(f'put "{p}" "{p.name}"' for p in files)
    rc, out = await _smb(f'cd "{d}"; {puts}; put "{backup_path}" "healthhub-latest.db"')
    if rc != 0:
        return {"ok": False, "error": out[:300]}
    return {"ok": True, "files": [p.name for p in files] + ["healthhub-latest.db"]}
