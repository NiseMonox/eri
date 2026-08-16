"""自由文本的正则快路径(零成本零延迟)。不命中的交给 services/conversation(带上下文的 LLM 大脑)。"""

import re

# 裸数字或「体重 62.5kg」式;20-300 之外不当体重
RE_WEIGHT = re.compile(
    r"^(?:体重|weight|たいじゅう|体重は?)?\s*(\d{2,3}(?:\.\d{1,2})?)\s*(?:kg|公斤|キロ|きろ)?$",
    re.IGNORECASE,
)
# 锚定全句匹配:「忘记吃药了」「还没吃药」「薬飲んだ?」这类否定/疑问句不会命中,落给 LLM 判断
RE_MED = re.compile(
    r"^(吃过药了?|吃药了|已服药|服药了|药吃了|薬(を)?飲んだ|飲みました|服薬した"
    r"|took (my )?meds?)[。.!!~✅👍🆗]*$",
    re.IGNORECASE,
)
RE_TODAY = re.compile(r"^(今天|今日|待办|日程|/?today)$", re.IGNORECASE)
RE_CHART = re.compile(r"^(图|图表|曲线|チャート|グラフ|/?chart)$", re.IGNORECASE)
RE_REPORT = re.compile(r"^(周报|週報|report)$", re.IGNORECASE)


def parse_regex(text: str) -> dict | None:
    t = text.strip()
    if m := RE_WEIGHT.match(t):
        kg = float(m.group(1))
        if 20 < kg < 300:
            return {"intent": "weight", "weight_kg": kg, "measured_at": None}
    if RE_MED.match(t):
        return {"intent": "med_confirm"}
    if RE_TODAY.match(t):
        return {"intent": "today"}
    if RE_CHART.match(t):
        return {"intent": "chart"}
    if RE_REPORT.match(t):
        return {"intent": "report"}
    return None
