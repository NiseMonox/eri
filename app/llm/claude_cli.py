"""claude -p(headless,计入 Claude 订阅额度)。串行信号量:个人场景不需要并发 LLM。"""

import asyncio
import json

_sem = asyncio.Semaphore(1)
DEFAULT_TIMEOUT = 45   # 交互式解析等不起 2 分钟;周报这类后台任务由调用方放宽


async def complete(prompt: str, system: str = "", timeout: int = DEFAULT_TIMEOUT) -> str | None:
    full = (system + "\n\n" + prompt) if system else prompt
    async with _sem:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "--output-format", "json",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/tmp",   # 空目录:避免加载任何项目上下文,纯文本补全
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(full.encode()), timeout=timeout)
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
