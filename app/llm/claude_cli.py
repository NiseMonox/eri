"""claude -p(headless,计入 Claude 订阅额度)。
精简模式:禁全部工具、自带系统提示词替换 Claude Code 默认的 ~27k token 提示词、
不加载 MCP、不落盘会话(注:--bare 会跳过凭据读取导致未登录,不能用)——每次调用只剩我们自己的 prompt。串行信号量。"""

import asyncio
import json

from .. import store

# 两个串行槽:交互式(parse)与后台(report/maintain)各一,后台任务不再让用户下一句排队等 40 秒
_sems = {"interactive": asyncio.Semaphore(1), "background": asyncio.Semaphore(1)}
DEFAULT_TIMEOUT = 45   # 交互式解析等不起 2 分钟;周报这类后台任务由调用方放宽

FALLBACK_SYSTEM = "You are a concise assistant. Follow the user's formatting instructions exactly."


def model_for(purpose: str) -> str:
    """purpose ∈ parse|report。settings: llm.claude_model_parse / llm.claude_model_report(默认都 sonnet)。"""
    key = "llm.claude_model_report" if purpose == "report" else "llm.claude_model_parse"
    return str(store.get(key, "sonnet") or "sonnet")


def _lane(purpose: str) -> str:
    return "background" if purpose in ("report", "maintain") else "interactive"


async def complete(prompt: str, system: str = "", timeout: int = DEFAULT_TIMEOUT,
                   purpose: str = "parse") -> str | None:
    args = [
        "claude", "-p",
        "--output-format", "json",
        "--model", model_for(purpose),
        "--tools", "",                       # 艾莉不需要任何 Claude Code 工具
        "--system-prompt", system or FALLBACK_SYSTEM,
        "--no-session-persistence",
        "--strict-mcp-config",               # 不加载任何 MCP
    ]
    async with _sems[_lane(purpose)]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/tmp",   # 空目录:避免加载任何项目上下文
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"claude -p 超时(>{timeout}s)")
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p 退出码 {proc.returncode}: {err.decode()[:200]}")
    data = json.loads(out.decode())
    if data.get("is_error"):
        raise RuntimeError(f"claude -p 返回错误: {str(data.get('result'))[:200]}")
    return data.get("result")
