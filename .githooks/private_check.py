#!/usr/bin/env python3
"""提交前的个人信息检查(本仓库是公开的)。
词表在仓库根的 .private-terms(已 gitignore,只在本机):每行一个 Python 正则,# 开头是注释。
pre-commit 查暂存区里新增/改动的行,commit-msg 查提交信息;命中就拒绝提交并指出位置。
确认是误报时用 git commit --no-verify 跳过。"""

import re
import subprocess
import sys
from pathlib import Path


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=True).stdout


def load_terms() -> list[re.Pattern]:
    path = Path(git("rev-parse", "--show-toplevel").strip()) / ".private-terms"
    if not path.is_file():
        print("[private-check] 没有 .private-terms,跳过个人信息检查", file=sys.stderr)
        return []
    return [re.compile(line.strip()) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def staged_additions():
    """(文件, 新文件里的行号, 行内容)。"""
    fname, lineno = None, 0
    for line in git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff").splitlines():
        if line.startswith("+++ "):
            fname = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            lineno = int(m.group(1)) if m else 0
        elif line.startswith("+") and fname:
            yield fname, lineno, line[1:]
            lineno += 1


def main() -> int:
    terms = load_terms()
    if not terms:
        return 0
    if len(sys.argv) > 1:   # commit-msg:参数是提交信息文件
        text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
        items = [("提交信息", i, l) for i, l in enumerate(text.splitlines(), 1) if not l.startswith("#")]
    else:
        items = list(staged_additions())
    hits = [(f, n, l, t.pattern) for f, n, l in items for t in terms if t.search(l)]
    for f, n, l, pat in hits:
        print(f"[private-check] {f}:{n} 命中 /{pat}/: {l.strip()[:120]}", file=sys.stderr)
    if hits:
        print("[private-check] 公开仓库不能提交个人信息,请换成中性的例子或占位符"
              "(确认是误报时用 git commit --no-verify 跳过)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
