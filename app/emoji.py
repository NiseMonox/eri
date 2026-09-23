"""去掉 emoji。艾莉对外的文字(Telegram / Bark / Siri / 音箱 / 网页)一律不带 emoji:
固定文案里不写,LLM 生成的内容在出口兜底删掉。「→ ・ 〜 ○ ◎ △ ×」这类日文常用符号保留。"""

import re

_CLASS = (
    "\U0001F000-\U0001FAFF"                                   # 表情、图形、国旗(区域指示符)、肤色
    "\u2600-\u27BF"                                            # 杂项符号 + Dingbats(☀ ✅ ✨ ❤;♪ ☆ 这类装饰也去掉)
    "\u2B00-\u2BFF"                                            # ⬆ ⭐ ⭕
    "\u231A\u231B\u2328\u23CF\u23E9-\u23F3\u23F8-\u23FA"         # ⌚ ⏰ ⏭ ⏳
    "\u25AA\u25AB\u25B6\u25C0\u25FB-\u25FE"                      # ▪ ▶ ◀ ◼(○ ◎ △ ■ 不在内)
    "\u2139\u24C2\u2934\u2935\u3030\u303D\u3297\u3299\u203C\u2049"   # ℹ Ⓜ ⤴ 〰 ㊗ ‼
    "\uFE0F\u200D\u20E3"                                       # 变体选择符、ZWJ、键帽
    "\U000E0020-\U000E007F"                                   # 旗帜标签序列
)
_RE = re.compile(f"[{_CLASS}]+[ \u3000]?")
_RE_SPACES = re.compile("[ \u3000]{2,}")


def strip(text: str) -> str:
    """删 emoji(连同紧跟的一个空格,「✅ 完了」→「完了」),再收拾删出来的连续空格与行尾空格。
    没有 emoji 时原样返回同一个对象。"""
    if not text:
        return text
    out = _RE.sub("", text)
    if out == text:
        return text
    out = _RE_SPACES.sub(" ", out)
    return "\n".join(line.rstrip() for line in out.split("\n")).strip()


def find(text: str) -> list[str]:
    """文中出现的 emoji(测试 / 排查用)。"""
    return [m.group().strip() for m in _RE.finditer(text or "")]
