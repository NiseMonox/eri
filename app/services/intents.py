"""标准意图执行层(体重/新建提醒/今日/图表/周报)。routine 确认与对话动作在 conversation 里。"""

from .. import clock
from . import reminders, weights


async def execute(intent: dict, *, via: str) -> dict:
    """返回 {"ok": bool, "reply": str, "photo": bytes|None}。via ∈ telegram|siri|web。"""
    kind = intent.get("intent")

    if kind == "weight":
        try:
            kg = float(intent["weight_kg"])
            at = clock.jst_default_iso(intent.get("measured_at"))
            r = weights.add_weight(kg, at, source=via)
        except weights.InvalidWeight:
            return {"ok": False, "reply": "その数字は体重っぽくないよ(20〜300kg)。記録してないよ", "photo": None}
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "reply": "体重が読み取れなかったよ。「62.5」か「昨夜10時に測って62.3」みたいに言ってね",
                    "photo": None}
        t = clock.fmt_local(r["row"]["measured_at"], "%m-%d %H:%M")
        if not r["created"]:
            return {"ok": False, "reply": f"その時刻({t})はもう記録があるよ。二重登録はしなかったよ", "photo": None}
        st = weights.stats(7)
        extra = f"、直近7日で {st['delta']:+.2f}kg" if st and st["count"] > 1 else ""
        return {"ok": True, "reply": f"⚖️ {r['row']['weight_kg']}kg 記録したよ({t}){extra}",
                "photo": None}

    if kind == "reminder":
        try:
            r = reminders.create(str(intent["title"]), str(intent.get("body", "")),
                                 clock.jst_default_iso(intent["due_at"]))
            return {"ok": True,
                    "reply": f"🔔 リマインダー作ったよ:{r['title']} @ "
                             f"{clock.fmt_local(r['due_at'], '%m-%d %H:%M')}",
                    "photo": None}
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "reply": "時間が読み取れなかったよ。「明日の朝9時にゴミ出し」みたいに言ってみて",
                    "photo": None}

    if kind == "today":
        return {"ok": True, "reply": today_text(), "photo": None}

    if kind == "chart":
        from .. import charts

        return {"ok": True, "reply": "直近30日の体重グラフだよ", "photo": charts.weight_chart_png(30)}

    if kind == "report":
        from ..scheduler.jobs import run_report

        await run_report({"days": 7})
        return {"ok": True, "reply": "週報を作って送ったよ", "photo": None}

    return {"ok": False, "reply": intent.get("reply") or "ごめん、よくわからなかったよ", "photo": None}


def today_text() -> str:
    agenda = reminders.today_agenda()
    if not agenda:
        return "今日の予定はないよ"
    icon = {"routine": "✅", "med": "💊", "weight_prompt": "⚖️", "reminder": "🔔"}
    return "今日の予定:\n" + "\n".join(
        f"{a['time']} {a.get('icon') or icon.get(a['kind'], '•')} {a['title']}" for a in agenda
    )
