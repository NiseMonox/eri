import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import clock, db
from .audio.manager import audio_manager
from .config import settings
from .bot import runner as bot_runner
from .routers import (
    audio,
    callbacks,
    ingest,
    memories,
    pages,
    reminders,
    routines,
    schedules,
    system,
    weights,
    withings,
)
from .scheduler import core

SEED_SCHEDULES = [
    ("示例·睡前白噪音", "audio", "30 23 * * *",
     {"action": "white_noise", "preset": "brown", "volume": 40, "duration_min": 60}),
    ("示例·早晨闹钟", "audio", "30 7 * * *",
     {"action": "alarm", "fade_in_sec": 120, "target_volume": 80, "max_min": 30}),
    ("示例·早晚记体重", "weight_prompt", "0 8,21 * * *",
     {"text": "该记体重了,上秤后发「体重62.5」或用 Siri 记录"}),
    ("示例·体重周报", "report", "0 21 * * 0", {"days": 7}),
]


def seed() -> None:
    conn = db.get_db()
    if conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] > 0:
        return
    now = clock.now_iso()
    for name, type_, cron, payload in SEED_SCHEDULES:
        conn.execute(
            "INSERT INTO schedules (name, type, cron, payload, enabled, created_at, updated_at) "
            "VALUES (?,?,?,?,0,?,?)",
            (name, type_, cron, json.dumps(payload, ensure_ascii=False), now, now),
        )
    conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    seed()
    if os.environ.get("HH_TEST") != "1":
        core.start()
        await bot_runner.start()
        from .audio import tts
        from .llm import embed

        asyncio.get_running_loop().create_task(tts.prewarm())
        asyncio.get_running_loop().create_task(embed.prewarm())   # bge-m3 冷加载 ~1.6s,别让第一句话超时
    yield
    await bot_runner.stop()
    core.shutdown()
    await audio_manager.stop(reason="shutdown")


app = FastAPI(title="Eri", lifespan=lifespan)

app.include_router(system.router)
app.include_router(schedules.router)
app.include_router(weights.router)
app.include_router(routines.router)
app.include_router(routines.legacy)
app.include_router(memories.router)
app.include_router(reminders.router)
app.include_router(audio.router)
app.include_router(ingest.router)
app.include_router(withings.router)
app.include_router(callbacks.router)
app.include_router(pages.router)

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
settings.media_dir.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(settings.media_dir)), name="media")
