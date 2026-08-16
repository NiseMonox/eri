"""settings 表读写。运行时可变参数放这里(网页可改、即时生效);secrets 放 .env。"""

import json
from typing import Any

from . import clock, db

# 网页 /settings 允许编辑的 key 白名单(值均为 JSON)
EDITABLE_KEYS = {
    "audio.backend": "mpv",
    "audio.alsa_device": "",          # 如 alsa/plughw:CARD=Device;空=系统默认
    "audio.cast_ip": "",
    "llm.provider": "claude",         # claude|deepseek|off
    "llm.claude_model_parse": "sonnet",    # 对话决策/解析/翻译:sonnet 够用且快(可填 opus/fable 或完整 model id)
    "llm.claude_model_report": "sonnet",   # 周报文案:想要更好文笔可改 opus
    "report.enabled": True,
    "withings.poll_minutes": 15,
    "tts.enabled": True,
    "tts.quiet": "23:00-08:00",       # 静音时段(跨午夜可),期间只发 Bark 不出声
    "tts.engine_url": "http://127.0.0.1:50021",   # VOICEVOX 兼容引擎;将来换 AivisSpeech 自训音色改这里
    "tts.speaker": 3,                 # 3=ずんだもん ノーマル,GET /speakers 枚举
    "tts.volume": 80,
    "tts.speak_replies": True,        # 对话确认(推迟/完成)也用音箱念出来
    "reminder.nag_defaults": {"every_min": 30, "max": 3, "grace_min": 120},   # 提醒/routine 无回应追催
    "chat.context_hours": 72,          # 短期对话窗口(小时)
    "chat.context_budget_tokens": 1500,  # 短期对话注入上限(粗估 token)
    "memory.enabled": True,            # 长期记忆自动维护开关
}


def get(key: str, default: Any = None) -> Any:
    row = db.get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        if default is None and key in EDITABLE_KEYS:
            return EDITABLE_KEYS[key]
        return default
    return json.loads(row["value"])


def set(key: str, value: Any) -> None:
    conn = db.get_db()
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False), clock.now_iso()),
    )
    conn.commit()


def editable() -> dict:
    return {k: get(k, EDITABLE_KEYS[k]) for k in EDITABLE_KEYS}
