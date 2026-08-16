"""mpv 后端:subprocess + --input-ipc-server IPC(音量渐强/随时停止用)。ALSA 直用,无会话依赖。"""

import asyncio
import json

from .. import store
from ..config import settings


class MpvPlayer:
    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.sock = str(settings.db_path.parent / "mpv.sock")

    async def play(self, source: str, volume: int = 100, loop: bool = False) -> None:
        await self.stop()
        args = [
            "mpv",
            "--no-video",
            "--really-quiet",
            "--idle=no",
            f"--volume={volume}",
            f"--input-ipc-server={self.sock}",
        ]
        dev = store.get("audio.alsa_device", "")
        if dev:
            args.append(f"--audio-device={dev}")
        if loop:
            args.append("--loop-file=inf")
        args += ["--", source]        # -- 终止选项解析,source 永远当文件名
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        # 等 IPC socket 就绪(最多 5 秒);起不来必须抛错,否则渐强闹钟会停在音量 0 没人知道
        for _ in range(50):
            if not self.running():
                raise RuntimeError(f"mpv exited immediately (source={source})")
            try:
                await self._ipc({"command": ["get_property", "volume"]})
                return
            except (OSError, ConnectionError):
                await asyncio.sleep(0.1)
        await self.stop()
        raise RuntimeError("mpv IPC socket 5 秒内未就绪,已放弃本次播放")

    async def set_volume(self, volume: int) -> None:
        await self._ipc({"command": ["set_property", "volume", max(0, min(130, volume))]})

    async def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        self.proc = None

    async def wait(self) -> None:
        """阻塞到当前 mpv 进程退出(自然播完或被停)。"""
        if self.proc is not None:
            await self.proc.wait()

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _ipc(self, cmd: dict) -> dict:
        reader, writer = await asyncio.open_unix_connection(self.sock)
        try:
            writer.write((json.dumps(cmd) + "\n").encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=2)
            return json.loads(line) if line else {}
        finally:
            writer.close()
