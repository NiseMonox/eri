"""settings 表读写。运行时可变参数放这里(网页可改、即时生效);secrets 放 .env。"""

import json
from typing import Any

from . import clock, db

# 网页 /settings 允许编辑的 key 白名单(值均为 JSON)
EDITABLE_KEYS = {
    "audio.backend": "mpv",
    "audio.alsa_device": "",          # 如 alsa/plughw:CARD=Device;空=系统默认
    "audio.cast_ip": "",
    "llm.provider": "deepseek",       # deepseek|off(off = 只走正则快路径,LLM 相关功能全部降级)
    "llm.model": "deepseek-flash",    # DeepSeek 模型 id;GET https://api.deepseek.com/models 可查(如 deepseek-v4-pro)
    "report.enabled": True,
    "withings.poll_minutes": 15,
    "tts.enabled": True,
    "tts.quiet": "23:00-08:00",       # 静音时段(跨午夜可),期间只发 Bark 不出声
    "tts.engine_url": "http://127.0.0.1:50021",   # VOICEVOX 兼容引擎;自训音色走 AivisSpeech :10101(deploy/aivisspeech)
    "tts.speaker": 3,                 # 3=ずんだもん ノーマル,GET /speakers 枚举
    "tts.volume": 80,
    "tts.speak_replies": True,        # 对话确认(推迟/完成)也用音箱念出来
    "reminder.nag_defaults": {"every_min": 30, "max": 3, "grace_min": 120},   # 提醒/routine 无回应追催
    "chat.context_budget_tokens": 3000,  # 对话原文注入上限(粗估 token;窗口 = 昨天 04:00 起 ∪ 还没整理的)
    "memory.enabled": True,            # 每天 04:00 把对话整理进长期记忆库(只管整理;注入/记住/忘掉不受影响)
    "memory.core_budget_tokens": 400,  # 核心档案(每次都带)上限
    "memory.core_max": 20,             # 核心档案条数上限(夜间整理每晚最多提名 2 条)
    "memory.rag_top_k": 6,             # 每句话自动带上的相关记忆条数
    "memory.rag_min_sim": 0.60,        # 相关记忆相似度门槛(bge-m3 中文问日语:无关闲聊最高 ~0.57)
    "memory.rag_budget_tokens": 400,   # 相关记忆注入上限
    "memory.embed_url": "http://127.0.0.1:11434",   # Ollama(deploy/ollama)
    "memory.embed_model": "bge-m3",    # 换模型后到 /memories 点「ベクトル再計算」
    "memory.llm_model": "",            # 夜间整理用的模型;空 = 和 llm.model 一样
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
