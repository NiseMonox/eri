"""网页(cookie 登录):dashboard / schedules / routines / memories / weights / reminders / settings。
表单走经典 POST + 302 回跳,零前端构建;图表用 vendored Chart.js。"""

import json
import secrets as pysecrets
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import clock, events, store
from ..audio.manager import audio_manager
from ..auth import COOKIE_NAME, require_page_login
from ..config import settings
from ..notify.service import notify
from ..scheduler import jobs
from ..services import memories, reminders, routines, weights
from ..services import schedules as sched_svc

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
templates.env.filters["fmt_local"] = clock.fmt_local
templates.env.filters["tojson_cn"] = lambda v: json.dumps(v, ensure_ascii=False)


def _render(request: Request, name: str, **ctx):
    ctx.setdefault("msg", request.query_params.get("msg", ""))
    ctx.setdefault("err", request.query_params.get("err", ""))
    return templates.TemplateResponse(request, name, ctx)


def _back(request: Request, path: str, msg: str = "", err: str = "") -> RedirectResponse:
    q = f"?msg={quote(msg)}" if msg else f"?err={quote(err)}" if err else ""
    return RedirectResponse(path + q, status_code=302)


# --- 登录 ---

@router.get("/login")
async def login_page(request: Request):
    return _render(request, "login.html")


@router.post("/login")
async def login(request: Request, token: str = Form(...)):
    from ..auth import token_ok

    if not token_ok(token):
        return _back(request, "/login", err="トークンが違うよ")
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(COOKIE_NAME, token, max_age=90 * 24 * 3600, httponly=True)
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# --- dashboard ---

@router.get("/")
async def dashboard(request: Request):
    if r := require_page_login(request):
        return r
    from ..services import body_metrics

    today_r = routines.today_instances()
    return _render(
        request, "dashboard.html",
        agenda=reminders.today_agenda(),
        done_today=reminders.done_today(),
        routine_open=[i for i in today_r if i["status"] in ("pending", "notified")],
        routine_missed=[i for i in routines.instances(status="missed", limit=10)],
        routine_done=[i for i in today_r if i["status"] == "done"],
        stats=weights.stats(7),
        body=body_metrics.latest(),
        activity=body_metrics.activity_latest(),
        body_labels=body_metrics.LABELS_JA,
        recent=weights.list_weights(limit=5),
        audio=audio_manager.status(),
        errors=events.recent(5, "notify_error") + events.recent(5, "schedule_error"),
    )


# --- schedules ---

@router.get("/schedules")
async def schedules_page(request: Request):
    if r := require_page_login(request):
        return r
    return _render(request, "schedules.html", rows=sched_svc.list_all(), routines=routines.list_all())


@router.post("/schedules/create")
async def schedules_create(request: Request, name: str = Form(...), type: str = Form(...),
                           cron: str = Form(...), payload: str = Form("{}"),
                           enabled: str = Form("0")):
    if r := require_page_login(request):
        return r
    try:
        sched_svc.create(name, type, cron.strip(), json.loads(payload or "{}"), enabled == "1")
        return _back(request, "/schedules", msg="作成したよ")
    except (ValueError, json.JSONDecodeError) as e:
        return _back(request, "/schedules", err=str(e))


@router.post("/schedules/{sid}/update")
async def schedules_update(request: Request, sid: int, name: str = Form(...),
                           cron: str = Form(...), payload: str = Form("{}")):
    if r := require_page_login(request):
        return r
    try:
        row = sched_svc.update(sid, name=name, cron=cron.strip(),
                               payload=json.loads(payload or "{}"))
        if row is None:
            return _back(request, "/schedules", err="そのタスクは見つからなかったよ(削除済みかも)")
        return _back(request, "/schedules", msg="保存したよ")
    except (ValueError, json.JSONDecodeError) as e:
        return _back(request, "/schedules", err=str(e))


@router.post("/schedules/{sid}/toggle")
async def schedules_toggle(request: Request, sid: int):
    if r := require_page_login(request):
        return r
    sched_svc.toggle(sid)
    return _back(request, "/schedules")


@router.post("/schedules/{sid}/delete")
async def schedules_delete(request: Request, sid: int):
    if r := require_page_login(request):
        return r
    sched_svc.delete(sid)
    return _back(request, "/schedules", msg="削除したよ")


@router.post("/schedules/{sid}/run")
async def schedules_run(request: Request, sid: int):
    if r := require_page_login(request):
        return r
    if sched_svc.get(sid) is None:
        return _back(request, "/schedules", err="そのタスクは見つからなかったよ(削除済みかも)")
    await jobs.run_schedule(sid, force=True)
    return _back(request, "/schedules", msg="1回実行したよ。スマホとスピーカーを確認してね")


# --- routines ---

@router.get("/routines")
async def routines_page(request: Request):
    if r := require_page_login(request):
        return r
    from datetime import timedelta as _td

    weeks = 12
    days = [(clock.now_local() - _td(days=i)).strftime("%Y-%m-%d") for i in range(weeks * 7 - 1, -1, -1)]
    return _render(request, "routines.html",
                   routines=routines.list_all(include_inactive=True),
                   categories=routines.CATEGORIES,
                   heat=routines.heatmap(weeks), days=days,
                   rates={r["id"]: routines.completion_rate(r["id"], 7) for r in routines.list_all()},
                   instances=routines.instances(limit=30))


@router.post("/routines/create")
async def routines_create(request: Request, name: str = Form(...), category: str = Form("other"),
                          detail: str = Form("")):
    if r := require_page_login(request):
        return r
    routines.create(name, category, detail)
    return _back(request, "/routines", msg="追加したよ")


@router.post("/routines/{rid}/toggle")
async def routines_toggle(request: Request, rid: int):
    if r := require_page_login(request):
        return r
    row = routines.get(rid)
    if row:
        routines.update(rid, active=0 if row["active"] else 1)
    return _back(request, "/routines")


@router.post("/routines/instances/{iid}/done")
async def routines_inst_done(request: Request, iid: int):
    if r := require_page_login(request):
        return r
    row = routines.complete(iid, via="web")
    if row is None:
        return _back(request, "/routines", err="その記録は見つからなかったよ")
    # dashboard 与 routines 页都有完成按钮:回到来源页
    back_to = "/" if request.headers.get("referer", "").rstrip("/").endswith(":8300") else "/routines"
    return _back(request, back_to, msg="完了にしたよ")


@router.post("/routines/instances/{iid}/skip")
async def routines_inst_skip(request: Request, iid: int):
    if r := require_page_login(request):
        return r
    routines.skip(iid, via="web")
    return _back(request, "/routines", msg="スキップしたよ")


# --- memories ---

@router.get("/memories")
async def memories_page(request: Request):
    if r := require_page_login(request):
        return r
    return _render(request, "memories.html", rows=memories.list_all(),
                   inactive=[m for m in memories.list_all(include_inactive=True) if not m["active"]][:30],
                   kinds=memories.KIND_JA)


@router.post("/memories/{mid}/reactivate")
async def memories_reactivate(request: Request, mid: int):
    if r := require_page_login(request):
        return r
    memories.reactivate(mid)
    return _back(request, "/memories", msg="思い出したよ")


@router.post("/memories/create")
async def memories_create(request: Request, kind: str = Form("fact"), text: str = Form(...),
                          valid_until: str = Form("")):
    if r := require_page_login(request):
        return r
    until = clock.local_input_to_utc_iso(valid_until) if valid_until else None
    memories.add(kind, text, until, source="user")
    return _back(request, "/memories", msg="覚えたよ")


@router.post("/memories/{mid}/delete")
async def memories_delete(request: Request, mid: int):
    if r := require_page_login(request):
        return r
    memories.deactivate(mid, reason="user")
    return _back(request, "/memories", msg="忘れたよ")


@router.post("/memories/{mid}/update")
async def memories_update(request: Request, mid: int, text: str = Form(...),
                          valid_until: str = Form("")):
    if r := require_page_login(request):
        return r
    until = clock.local_input_to_utc_iso(valid_until) if valid_until else None
    memories.update(mid, text, until)
    return _back(request, "/memories", msg="更新したよ")


# --- weights ---

@router.get("/weights")
async def weights_page(request: Request):
    if r := require_page_login(request):
        return r
    return _render(request, "weights.html", rows=weights.list_weights(limit=60),
                   stats=weights.stats(30))


@router.post("/weights/create")
async def weights_create(request: Request, weight_kg: float = Form(...),
                         measured_at: str = Form("")):
    if r := require_page_login(request):
        return r
    try:
        at = clock.local_input_to_utc_iso(measured_at) if measured_at else None
        r2 = weights.add_weight(weight_kg, at, source="manual")
        if not r2["created"]:
            return _back(request, "/weights",
                         err="同時刻の手動記録がすでにあるよ。直したい場合は先に古い記録を削除してね")
        return _back(request, "/weights", msg="記録したよ")
    except (ValueError, weights.InvalidWeight) as e:
        return _back(request, "/weights", err=str(e))


@router.post("/weights/{wid}/delete")
async def weights_delete(request: Request, wid: int):
    if r := require_page_login(request):
        return r
    weights.delete(wid)
    return _back(request, "/weights", msg="削除したよ")


# --- reminders ---

@router.get("/reminders")
async def reminders_page(request: Request):
    if r := require_page_login(request):
        return r
    return _render(request, "reminders.html", rows=reminders.list_all(limit=50))


@router.post("/reminders/create")
async def reminders_create(request: Request, title: str = Form(...), body: str = Form(""),
                           due_at: str = Form(...)):
    if r := require_page_login(request):
        return r
    try:
        reminders.create(title, body, clock.local_input_to_utc_iso(due_at))
        return _back(request, "/reminders", msg="作成したよ")
    except ValueError as e:
        return _back(request, "/reminders", err=str(e))


@router.post("/reminders/{rid}/done")
async def reminders_done(request: Request, rid: int):
    if r := require_page_login(request):
        return r
    reminders.set_status(rid, "done")
    return _back(request, "/reminders")


@router.post("/reminders/{rid}/delete")
async def reminders_delete(request: Request, rid: int):
    if r := require_page_login(request):
        return r
    reminders.set_status(rid, "dismissed")
    return _back(request, "/reminders")


# --- settings / 工具 ---

@router.get("/settings")
async def settings_page(request: Request):
    if r := require_page_login(request):
        return r
    return _render(
        request, "settings.html",
        values=store.editable(),
        bark_ok=bool(settings.bark_device_key),
        tg_ok=bool(settings.telegram_bot_token and settings.telegram_chat_id),
    )


@router.post("/settings/update")
async def settings_update(request: Request):
    if r := require_page_login(request):
        return r
    form = await request.form()
    # 先全部解析,全对才落库——避免「前几个已生效、后面报错」的半保存状态
    parsed: dict = {}
    for key in store.EDITABLE_KEYS:
        if key in form:
            try:
                parsed[key] = json.loads(str(form[key]))
            except json.JSONDecodeError as e:
                return _back(request, "/settings", err=f"{key} の JSON が不正だよ({e})。今回は何も保存してないよ")
    for k, v in parsed.items():
        store.set(k, v)
    return _back(request, "/settings", msg="保存したよ")


@router.post("/settings/tts-test")
async def settings_tts_test(request: Request, text: str = Form("こんにちは、エリだよ。テスト成功!")):
    if r := require_page_login(request):
        return r
    from ..audio import tts

    ok = await tts.announce(text, force=True)
    if ok:
        return _back(request, "/settings", msg="再生したよ。スピーカーを聞いてね")
    alive = await tts.engine_alive()
    return _back(request, "/settings",
                 err="再生失敗:" + ("TTSがオフか合成エラーだよ(イベントログ参照)" if alive else "VOICEVOXエンジンがオフラインだよ"))


@router.post("/settings/test-notify")
async def settings_test_notify(request: Request, profile: str = Form("info")):
    if r := require_page_login(request):
        return r
    sent = await notify(profile, "health-hub 测试通知", f"profile={profile}")
    ok = [k for k, v in sent.items() if v]
    return _back(request, "/settings",
                 msg=f"送信したよ: {','.join(ok)}" if ok else "", err="" if ok else "どのチャンネルにも送れなかったよ。.env の設定を確認してね")


@router.post("/audio/play-test")
async def audio_play_test(request: Request, action: str = Form("white_noise")):
    if r := require_page_login(request):
        return r
    payload = ({"action": "alarm", "fade_in_sec": 10, "target_volume": 70, "max_min": 5}
               if action == "alarm" else
               {"action": "white_noise", "volume": 40, "duration_min": 5})
    try:
        await audio_manager.start(payload)
        return _back(request, "/", msg="再生開始したよ(5分で自動停止)")
    except Exception as e:  # noqa: BLE001
        return _back(request, "/", err=f"再生失敗: {e}")


@router.post("/audio/stop-web")
async def audio_stop_web(request: Request):
    if r := require_page_login(request):
        return r
    await audio_manager.stop()
    return _back(request, "/", msg="停止したよ")
