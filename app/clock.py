"""统一时间入口:DB 一律存 UTC ISO8601,展示/cron 一律 Asia/Tokyo。测试通过 set_override 注入假时间。"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TOKYO = ZoneInfo("Asia/Tokyo")

_override: datetime | None = None


def set_override(dt: datetime | None) -> None:
    global _override
    _override = dt


def now_utc() -> datetime:
    if _override is not None:
        return _override
    return datetime.now(timezone.utc)


def now_local() -> datetime:
    return now_utc().astimezone(TOKYO)


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(TOKYO)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(now_utc())


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_local(s: str | None, fmt: str = "%m-%d %H:%M") -> str:
    """ISO UTC 字符串 → 东京时间显示。模板用。"""
    if not s:
        return "—"
    return to_local(parse_iso(s)).strftime(fmt)


def local_input_to_utc_iso(s: str) -> str:
    """网页 datetime-local 输入(naive,按东京时间理解)→ UTC ISO。"""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TOKYO)
    return iso(dt)


def jst_default_iso(s: str | None) -> str | None:
    """LLM 给的时间串:无时区时按东京补(prompt 要求带 +09:00 但模型不总遵守),
    → UTC ISO。None 原样返回。非法格式抛 ValueError。"""
    if s is None:
        return None
    return local_input_to_utc_iso(s)


def parse_flexible_jst(s: str) -> str:
    """宽容解析快捷指令/人手输入的日期串(无时区按东京):
    2026-08-24 10:00 / 2026/08/24 10:00 / 2026年8月24日 10:00 / ISO 全支持。→ UTC ISO。"""
    import re as _re

    t = s.strip()
    t = t.replace("年", "-").replace("月", "-").replace("日", " ").replace("/", "-")
    t = _re.sub(r"\s+", " ", t).strip()
    m = _re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?(.*)$", t)
    if not m:
        raise ValueError(f"无法解析日期: {s!r}")
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh, mm, ss = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
    tail = (m.group(7) or "").strip()
    dt = datetime(y, mo, d, hh, mm, ss)
    if tail:  # 带时区偏移的 ISO 交给标准解析
        return iso(datetime.fromisoformat(s.strip()))
    return iso(dt.replace(tzinfo=TOKYO))
