"""标准意图执行层(体重/新建提醒/今日/图表/周报)。routine 确认与对话动作在 conversation 里。"""

from .. import clock, emoji
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
        return {"ok": True, "reply": f"{r['row']['weight_kg']}kg 記録したよ({t}){extra}",
                "photo": None}

    if kind == "reminder":
        try:
            title = str(intent["title"])
            due = clock.jst_default_iso(intent["due_at"])
            if due is None:
                raise ValueError("due_at 为空")
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "reply": "時間が読み取れなかったよ。「明日の朝9時にゴミ出し」みたいに言ってみて",
                    "photo": None}
        # LLM 算错日期(如晚上说「8点」)时别建出立刻就响的提醒:退回让它改正或问清楚
        if clock.parse_iso(due) <= clock.now_utc():
            return {"ok": False, "reply": f"{clock.fmt_local(due)} はもう過ぎてるよ。いつにする?",
                    "photo": None}
        r = reminders.create(title, str(intent.get("body", "")), due)
        return {"ok": True,
                "reply": f"リマインダー作ったよ:{r['title']} @ "
                         f"{clock.fmt_local(r['due_at'], '%m-%d %H:%M')}",
                "photo": None}

    if kind == "today":
        return {"ok": True, "reply": today_text(), "photo": None}

    if kind == "chart":
        from .. import charts

        days = max(7, min(365, int(intent.get("days") or 30)))
        return {"ok": True, "reply": f"直近{days}日の体重グラフだよ", "photo": charts.weight_chart_png(days)}

    if kind == "report":
        from ..scheduler.jobs import run_report

        await run_report({"days": 7})
        return {"ok": True, "reply": "週報を作って送ったよ", "photo": None}

    return {"ok": False, "reply": intent.get("reply") or "ごめん、よくわからなかったよ", "photo": None}


def today_text() -> str:
    agenda = reminders.today_agenda()
    if not agenda:
        return "今日の予定はないよ"
    return emoji.strip("今日の予定:\n" + "\n".join(f"・{a['time']} {a['title']}" for a in agenda))
