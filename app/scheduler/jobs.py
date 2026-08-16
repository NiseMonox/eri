"""schedule 触发后的分发,以及内部固定 job(sweeper/备份/清理)。
任何异常捕获后写 event_log,绝不让单个 job 压垮 scheduler。"""

import json

from .. import clock, db, events
from ..audio import tts
from ..audio.manager import audio_manager
from ..config import settings
from ..notify.service import notify
from ..services import meds, reminders, weights


async def run_schedule(schedule_id: int, force: bool = False) -> None:
    row = db.get_db().execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    if row is None or (not row["enabled"] and not force):
        return
    payload = json.loads(row["payload"] or "{}")
    try:
        await dispatch(row["type"], payload, dict(row))
        conn = db.get_db()
        conn.execute("UPDATE schedules SET last_run_at=? WHERE id=?", (clock.now_iso(), schedule_id))
        conn.commit()
        events.log("schedule_fired", {"name": row["name"], "type": row["type"]},
                   "schedule", schedule_id)
    except Exception as e:  # noqa: BLE001
        events.log("schedule_error", {"name": row["name"], "error": str(e)},
                   "schedule", schedule_id)


async def run_oneshot(type_: str, payload: dict) -> None:
    """dev/oneshot 端到端测试用:无 schedule 行,直接分发。"""
    try:
        await dispatch(type_, payload, None)
        events.log("oneshot_fired", {"type": type_, "payload": payload})
    except Exception as e:  # noqa: BLE001
        events.log("oneshot_error", {"type": type_, "error": str(e)})


async def dispatch(type_: str, payload: dict, row: dict | None) -> None:
    name = row["name"] if row else payload.get("name", type_)
    schedule_id = row["id"] if row else None

    if type_ == "audio":
        is_alarm = payload.get("action") == "alarm"
        try:
            session = await audio_manager.start(payload)
        except Exception as e:
            if is_alarm:
                # 音箱坏了闹钟也得响:Bark critical 循环响铃本身就能叫醒
                await notify("alarm", "起床アラーム(音声再生に失敗)", f"スピーカー側エラー: {e}",
                             channel="bark", ref_type="schedule", ref_id=schedule_id)
            raise
        if is_alarm:
            stop_url = f"{settings.base_url}/s/stop?t={session['token']}"
            await notify("alarm", "起床アラーム", "タップで停止できるよ", url=stop_url,
                         channel="bark", ref_type="schedule", ref_id=schedule_id)

    elif type_ == "med":
        med_id = payload.get("med_id")
        med = meds.get_med(med_id) if med_id else None
        if med is None:
            raise ValueError(f"med schedule 缺少有效 med_id: {payload}")
        if not med["active"]:
            events.log("med_inactive_skipped", {"med_id": med["id"], "name": med["name"]},
                       "schedule", schedule_id)
            return
        log = meds.create_due_log(med["id"], schedule_id, params=payload)
        await _send_med_notice(log | {"med_name": med["name"], "med_dose": med["dose"]})
        await tts.announce_med(med["name"])

    elif type_ == "weight_prompt":
        await notify("med", name or "体重記録",
                     payload.get("text", "体重を測ろう!「62.5」って送るか、エリちゃんに言ってね"),
                     ref_type="schedule", ref_id=schedule_id)
        await tts.announce_weight()

    elif type_ == "reminder":
        title = payload.get("title") or name
        inst = reminders.create_instance(title, schedule_id, payload.get("nag"),
                                         body=payload.get("body", ""))
        if inst is None:
            return  # 当日已有实例(重复触发/补跑),不再打扰
        await notify("info", f"リマインダー:{title}", payload.get("body", ""),
                     ref_type="reminder", ref_id=inst["id"])
        # 先标 notified 再做慢速 TTS(LLM 翻译+合成+播放可达分钟级),
        # 否则 1 分钟一趟的 sweeper 会在 await 窗口里对同一实例二次首播
        reminders.mark_notified(inst["id"])
        await tts.announce_reminder(title)

    elif type_ == "report":
        await run_report(payload)

    else:
        raise ValueError(f"unknown schedule type: {type_}")


async def _send_med_notice(log: dict, nth: int = 0) -> None:
    title = f"お薬の時間:{log['med_name']}" + (f" {log['med_dose']}" if log.get("med_dose") else "")
    if nth:
        title = f"[{nth}回目] " + title
    confirm_url = f"{settings.base_url}/c/med/{log['token']}"
    # inline 按钮只在 bot 运行时挂(bot 是 callback 的唯一消费端,不然按钮假死)
    from ..bot import runner as bot_runner

    buttons = None
    if bot_runner.configured():
        buttons = [("✅ 飲んだよ", f"medconfirm:{log['id']}"), ("今回はスキップ", f"medskip:{log['id']}")]
    await notify(
        "med", title, "通知タップで服薬確認できるよ",
        url=confirm_url,
        tg_buttons=buttons,
        ref_type="med_log", ref_id=log["id"],
    )


async def run_report(payload: dict) -> None:
    from .. import charts
    from ..llm import base as llm

    days = int(payload.get("days", 7))
    st = weights.stats(days)
    if st is None:
        await notify("info", "体重週報", f"直近{days}日の体重記録はないよ")
        return
    text = (
        f"直近{days}日:{st['count']}回記録、平均 {st['avg']}kg、"
        f"範囲 {st['min']}–{st['max']}kg、変化 {st['delta']:+.2f}kg"
    )
    from ..services import body_metrics

    body_line = body_metrics.summary_ja()
    if body_line:
        text += f"\n最新の体組成:{body_line}"
    # LLM 文案是锦上添花:失败/关闭时纯统计照发
    narrative = await llm.complete(
        f"体重数据统计:{text}。逐条明细(东京时间):"
        + "; ".join(f"{clock.fmt_local(r['measured_at'])} {r['weight_kg']}kg"
                    for r in weights.recent_days(days))
        + "\n\n用日语写 2-3 句周报点评(友达口吻,だよ/ね系):先说趋势,再给一句轻量建议。不要说教,不要列表,只输出日语正文。",
        timeout=120,
        purpose="report",
    )
    if narrative:
        text = narrative.strip() + "\n\n" + text
    png = charts.weight_chart_png(max(days, 30))
    sent = await notify("info", "体重週報", text, photo=png)
    if not sent.get("telegram"):
        await notify("info", "体重週報", text, channel="bark")


# --- 内部固定 job(各自兜底 try/except:内部 job 没有 run_schedule 的保护壳)---

async def med_sweeper() -> None:
    try:
        for log, action in meds.sweep():
            if action == "resend":
                await _send_med_notice(log, nth=log["remind_count"])
            else:
                await notify("info", f"飲み忘れ扱いにしたよ:{log['med_name']}",
                             "猶予時間を過ぎたよ。飲んだら画面かチャットで教えてね",
                             ref_type="med_log", ref_id=log["id"])
    except Exception as e:  # noqa: BLE001
        events.log("sweeper_error", {"job": "med_sweeper", "error": str(e)})


async def reminder_sweeper() -> None:
    try:
        # 到点首播(含 snooze 回来的)
        for r in reminders.due_pending():
            await notify("info", f"リマインダー:{r['title']}", r.get("body") or "",
                         ref_type="reminder", ref_id=r["id"])
            reminders.mark_notified(r["id"])
            await tts.announce_reminder(r["title"])
        # 无回应追催 / 超时作罢
        for row, act in reminders.sweep_nag():
            if act == "renag":
                await notify("med", f"[{row['remind_count']}回目] リマインダー:{row['title']}",
                             "「終わったよ」か「後でね」って返してくれればOKだよ",
                             ref_type="reminder", ref_id=row["id"])
                await tts.announce_nag(row["title"], row["remind_count"])
            else:
                await notify("info", f"いったん諦めたよ:{row['title']}",
                             "何度か声かけたけど返事がなかったよ。終わってたら一言教えてね",
                             ref_type="reminder", ref_id=row["id"])
    except Exception as e:  # noqa: BLE001
        events.log("sweeper_error", {"job": "reminder_sweeper", "error": str(e)})


async def daily_backup() -> None:
    try:
        dest = db.backup(settings.db_path.parent / "backups")
        events.log("backup", {"dest": str(dest)})
    except Exception as e:  # noqa: BLE001
        events.log("backup_error", {"error": str(e)})


async def weekly_trim() -> None:
    try:
        n = events.trim(days=90)
        m = tts.trim_cache(keep=200)
        events.log("trim", {"deleted": n, "tts_cache_deleted": m})
    except Exception as e:  # noqa: BLE001
        events.log("trim_error", {"error": str(e)})
