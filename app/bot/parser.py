"""自由文本的正则快路径(零成本零延迟)。不命中的交给 services/conversation(带上下文的 LLM 大脑)。"""

import re
import unicodedata

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
# 停掉音箱(闹钟/白噪音):只认短短一整句,LLM 挂了也能用语音叫停闹钟;长句交给 LLM
RE_STOP = re.compile(
    r"^(?:(?:アラーム|目覚まし|ホワイトノイズ|音楽|闹钟|白噪音|音乐)(?:を|は)?\s*)?"
    r"(?:止めて|とめて|ストップ|停止|止まって|关掉|关了|关闭|停下|停一下|别响了|不要响了|stop)"
    r"(?:ください|くれ|吧|了)?$",
    re.IGNORECASE,
)
# 语音识别会在句尾加句号(「62.5キロ。」);问号不去——「薬飲んだ?」是在问,不能当确认
_TRAILING = "。.!、,~〜 "


def _normalize(text: str) -> str:
    """全角转半角(NFKC)再去掉句尾的句号类标点。"""
    return unicodedata.normalize("NFKC", text).strip().rstrip(_TRAILING)


def parse_regex(text: str) -> dict | None:
    t = text.strip()
    n = _normalize(t)
    return _match(t) or (_match(n) if n != t else None)


def _match(t: str) -> dict | None:
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
    if RE_STOP.match(t):
        return {"intent": "audio_stop"}
    return None
