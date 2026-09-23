"""正则解析路径(不触发 LLM)。"""

from app.bot import parser


def test_weight_variants():
    for text, kg in [("62.5", 62.5), ("体重62.5", 62.5), ("体重 62.5 kg", 62.5),
                     ("62.5kg", 62.5), ("体重は62.5キロ", 62.5), ("102", 102.0)]:
        r = parser.parse_regex(text)
        assert r == {"intent": "weight", "weight_kg": kg, "measured_at": None}, text


def test_weight_rejects_out_of_range():
    assert parser.parse_regex("15") is None          # <20 不像体重
    assert parser.parse_regex("500") is None
    assert parser.parse_regex("2026") is None


def test_med_confirm():
    for text in ("吃过药了", "已服药", "薬飲んだ", "薬を飲んだ", "took my meds", "吃药了!"):
        assert parser.parse_regex(text) == {"intent": "med_confirm"}, text


def test_med_confirm_negations_fall_to_llm():
    """否定/疑问/条件句绝不能被正则误判为已服药(审查抓出的 high bug)。"""
    for text in ("忘记吃药了", "昨天忘了吃药了", "还没吃过药", "今天不用吃药了",
                 "提醒我吃药了吗", "薬を飲んだら眠い", "まだ薬を飲んだかわからない",
                 "薬飲んだ?"):
        assert parser.parse_regex(text) is None, text


def test_simple_commands():
    assert parser.parse_regex("今天")["intent"] == "today"
    assert parser.parse_regex("图")["intent"] == "chart"
    assert parser.parse_regex("周报")["intent"] == "report"


def test_free_text_falls_through():
    assert parser.parse_regex("昨晚十点量的62.3") is None   # 交给 LLM
    assert parser.parse_regex("明早9点倒垃圾") is None


def test_speech_recognition_output():
    """语音识别会加句号、可能出全角数字;问号不去掉(「薬飲んだ?」是在问)。"""
    assert parser.parse_regex("62.5キロ。")["weight_kg"] == 62.5
    assert parser.parse_regex("６２．５")["weight_kg"] == 62.5
    assert parser.parse_regex("薬飲んだ。") == {"intent": "med_confirm"}
    assert parser.parse_regex("薬飲んだ？") is None
    assert parser.parse_regex("今日。") == {"intent": "today"}


def test_stop_audio():
    for text in ("止めて", "アラーム止めて。", "アラームを止めてください", "ストップ", "关掉", "闹钟关了吧",
                 "别响了", "Stop"):
        assert parser.parse_regex(text) == {"intent": "audio_stop"}, text
    for text in ("明日の朝アラームを止めて", "止めてほしいことがある", "关掉提醒"):   # 长句 / 别的东西交给 LLM
        assert parser.parse_regex(text) is None, text
