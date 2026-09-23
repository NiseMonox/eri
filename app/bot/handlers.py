"""Telegram 命令与消息处理。业务动作全部走 services 层(intents.execute 与网页/Bark/Siri 共用)。"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from .. import charts, clock, emoji, events, store
from ..config import settings
from ..services import intents, routines

HELP = (
    "なんでも気軽に話しかけてね。一度にいくつ頼んでもOKだよ。たとえば:\n"
    "「62.5」→ 体重記録\n"
    "「昨夜10時に測って62.3」→ 時刻つきで記録\n"
    "「薬飲んだ」→ 服薬確認\n"
    "「今日」→ 今日の予定 /「グラフ」→ 体重グラフ /「週報」→ 週報作成\n"
    "「明日の朝9時にゴミ出し」→ リマインダー作成\n"
    "ボイスメッセージでもOK(返事は家のスピーカーでも読み上げるよ)\n"
    "コマンド:/today /chart /report /routine /help"
)
TG_MAX_DOWNLOAD = 20 * 1024 * 1024     # Bot API 的下载上限


def _authorized(update: Update) -> bool:
    return (
        update.effective_chat is not None
        and str(update.effective_chat.id) == settings.telegram_chat_id
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update) or not update.effective_message:
        return
    await update.effective_message.reply_text("エリだよ、おうちのAIアシスタント。\n\n" + HELP)


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update) or not update.effective_message:
        return
    await update.effective_message.reply_text(intents.today_text())


async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update) or not update.effective_message:
        return
    days = 30
    if context.args:
        try:
            days = max(7, min(365, int(context.args[0])))
        except ValueError:
            pass
    await update.effective_message.reply_photo(charts.weight_chart_png(days))


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update) or not update.effective_message:
        return
    await update.effective_message.reply_text("作ってるよ…")
    await intents.execute({"intent": "report"}, via="telegram")


async def cmd_routine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update) or not update.effective_message:
        return
    opens = [i for i in routines.today_instances() if i["status"] in ("pending", "notified", "missed")]
    if not opens:
        await update.effective_message.reply_text("いま確認待ちのルーティンはないよ")
        return
    for inst in opens[:5]:
        await update.effective_message.reply_text(
            emoji.strip(f"{inst['routine_name']}(予定 {clock.fmt_local(inst['due_at'])})"),
            reply_markup=_routine_buttons(inst["id"], inst["category"]),
        )


def _routine_buttons(inst_id: int, category: str = "other") -> InlineKeyboardMarkup:
    done_label = "飲んだよ" if category == "med" else "やったよ"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(done_label, callback_data=f"rdone:{inst_id}"),
        InlineKeyboardButton("今回はスキップ", callback_data=f"rskip:{inst_id}"),
    ]])


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    # 先 answer 停掉客户端转圈,再做 DB 动作(过期 query 的 answer 失败也无妨)
    try:
        await q.answer()
    except Exception:  # noqa: BLE001
        pass
    if not _authorized(update):
        return
    action, _, sid = q.data.partition(":")
    row = None
    if action in ("rdone", "medconfirm"):
        row = routines.complete(int(sid), via="telegram")
    elif action in ("rskip", "medskip"):
        row = routines.skip(int(sid), via="telegram")
    if row is None:
        await q.edit_message_text("その記録は見つからなかったよ(削除されたかも)")
        return
    t = clock.fmt_local(row.get("done_at"), "%H:%M")
    label = {"done": f"完了 {t}", "dismissed": f"スキップ {t}",
             "missed": "期限切れ扱いだよ。やったら「やった」って言ってね"}.get(row["status"], row["status"])
    await q.edit_message_text(emoji.strip(f"{row['title']} {label}".strip()))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not _authorized(update) or not msg or not msg.text:
        return
    from ..services import conversation

    res = await conversation.handle(msg.text, via="telegram")
    if res.get("photo") is not None:
        await msg.reply_photo(res["photo"], caption=res["reply"])
    else:
        await msg.reply_text(res["reply"])


def _seconds(duration) -> float:
    """PTB 22 的 duration 可能是 int(秒,带弃用警告)也可能是 timedelta(设了 PTB_TIMEDELTA)。"""
    if duration is None:
        return 0.0
    return duration.total_seconds() if hasattr(duration, "total_seconds") else float(duration)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """语音消息 / 音频文件 → 服务器识别 → 和文字一样的对话;回复发在聊天里(带上听到的原话),
    也在家里音箱念(voice.speak_telegram)。"""
    msg = update.effective_message
    if not _authorized(update) or not msg:
        return
    media = msg.voice or msg.audio
    if media is None:
        return
    if msg.forward_origin is not None:     # 转发来的是别人的声音,不当成用户说的话
        await msg.reply_text("転送されたボイスは聞かないでおくね。自分の声で話しかけてね")
        return
    from ..services import voice

    limit = voice.max_sec()
    if _seconds(media.duration) > limit or (media.file_size or 0) > TG_MAX_DOWNLOAD:
        await msg.reply_text(f"長すぎるよ({limit}秒まで)")
        return
    try:
        await msg.reply_chat_action(ChatAction.TYPING)
    except Exception:  # noqa: BLE001 — 只是「正在输入」提示,失败无所谓
        pass
    try:
        tg_file = await media.get_file()
        data = bytes(await tg_file.download_as_bytearray())
    except Exception as e:  # noqa: BLE001
        events.log("bot_error", {"at": "voice_download", "error": str(e)[:200]})
        await msg.reply_text("ボイスをダウンロードできなかったよ。もう一回送ってね")
        return
    filename = getattr(media, "file_name", None) or ("voice.ogg" if msg.voice else "audio")
    res = await voice.turn(data, source="tg", filename=filename, content_type=media.mime_type,
                           speak=bool(store.get("voice.speak_telegram", True)))
    text = f"「{res['heard']}」\n\n{res['reply']}" if res["heard"] else res["reply"]
    if res.get("photo") is not None:
        await msg.reply_photo(res["photo"], caption=text[:1024])
    else:
        await msg.reply_text(text)
