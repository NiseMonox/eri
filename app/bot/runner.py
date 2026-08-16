"""PTB long-polling,随 FastAPI lifespan 启停。
bot 是附属通道:启动失败绝不拖垮核心服务(fail-soft + 后台重试)。"""

import asyncio

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .. import events
from ..config import settings
from . import handlers

_app: Application | None = None
_retry_task: asyncio.Task | None = None

RETRY_SEC = 300


def configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


async def _on_error(update, context) -> None:
    events.log("bot_error", {"at": "handler", "error": str(context.error)[:300]})
    try:
        if update and getattr(update, "effective_message", None):
            await update.effective_message.reply_text("出错了,这条没处理成功(已记日志)")
    except Exception:  # noqa: BLE001
        pass


async def _try_start() -> bool:
    global _app
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)   # LLM 解析最长几十秒,不能让服药按钮排队假死
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], handlers.cmd_start))
    app.add_handler(CommandHandler("today", handlers.cmd_today))
    app.add_handler(CommandHandler("chart", handlers.cmd_chart))
    app.add_handler(CommandHandler("report", handlers.cmd_report))
    app.add_handler(CommandHandler(["routine", "med"], handlers.cmd_routine))
    app.add_handler(CallbackQueryHandler(handlers.on_callback))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE, handlers.on_text))
    app.add_error_handler(_on_error)
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True, bootstrap_retries=-1)
    except Exception as e:  # noqa: BLE001 — token 无效/断网等:清理半初始化状态,核心服务照常跑
        events.log("bot_error", {"at": "startup", "error": str(e)[:300]})
        try:
            await app.shutdown()
        except Exception:  # noqa: BLE001
            pass
        return False
    _app = app
    events.log("bot_started", {"chat_id": settings.telegram_chat_id})
    return True


async def _retry_loop() -> None:
    while _app is None:
        await asyncio.sleep(RETRY_SEC)
        if _app is None and await _try_start():
            return


async def start() -> None:
    global _retry_task
    if not configured():
        return
    if not await _try_start():
        _retry_task = asyncio.create_task(_retry_loop())


async def stop() -> None:
    global _app, _retry_task
    if _retry_task is not None and not _retry_task.done():
        _retry_task.cancel()
    _retry_task = None
    if _app is None:
        return
    try:
        await _app.updater.stop()
        await _app.stop()
        await _app.shutdown()
    except Exception as e:  # noqa: BLE001
        events.log("bot_error", {"at": "shutdown", "error": str(e)[:300]})
    _app = None
