"""单一播放会话管理:同一时刻只放一个东西;渐强、停止 token、最长时长安全阀。
start/stop 加互斥锁防并发交错;player 实例钉在会话上(播放中切 backend 不影响停止)。"""

import asyncio
import secrets
from pathlib import Path

from .. import clock, events, store
from ..config import settings
from .cast_player import CastPlayer
from .mpv_player import MpvPlayer


def _resolve_source(payload: dict) -> str:
    action = payload.get("action", "white_noise")
    if payload.get("source"):
        root = settings.media_dir.parent.resolve()
        src = Path(payload["source"])
        p = (src if src.is_absolute() else root / src).resolve()
        if not src.is_absolute() and root not in p.parents and p != root:
            raise ValueError(f"相对路径越出项目目录: {payload['source']}")
        if not p.is_file():
            raise ValueError(f"音频文件不存在: {p}")
        return str(p)
    if action == "alarm":
        return str(settings.media_dir / "alarm.mp3")
    preset = payload.get("preset", "brown")
    return str(settings.media_dir / f"noise_{preset}.opus")


class AudioManager:
    def __init__(self) -> None:
        self._players: dict[str, object] = {}
        self.session: dict | None = None
        self._tasks: list[asyncio.Task] = []
        self._fade_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def _player(self):
        backend = store.get("audio.backend", "mpv")
        if backend not in self._players:
            self._players[backend] = CastPlayer() if backend == "cast" else MpvPlayer()
        return self._players[backend]

    async def start(self, payload: dict) -> dict:
        """payload: {action: alarm|white_noise|file, source?, preset?, volume?/target_volume?,
        fade_in_sec?, duration_min?, max_min?}。返回 session(含停止 token)。"""
        async with self._lock:
            await self._stop_locked(reason="replaced")
            action = payload.get("action", "white_noise")
            source = _resolve_source(payload)
            target = int(payload.get("target_volume", payload.get("volume", 70)))
            fade = int(payload.get("fade_in_sec", 0))
            limit_min = payload.get("duration_min") or payload.get("max_min")
            loop = bool(payload.get("loop", action == "white_noise"))

            player = self._player()
            token = secrets.token_urlsafe(8)
            await player.play(source, volume=0 if fade else target, loop=loop)
            self.session = {
                "token": token,
                "action": action,
                "source": source,
                "target_volume": target,
                "started_at": clock.now_iso(),
                "player": player,
                "payload": dict(payload),   # 播报打断后按此恢复
            }
            if fade:
                self._fade_task = asyncio.create_task(self._fade_in(player, target, fade))
                self._tasks.append(self._fade_task)
            if limit_min:
                self._tasks.append(asyncio.create_task(self._auto_stop(token, float(limit_min))))
            if hasattr(player, "wait"):
                self._tasks.append(asyncio.create_task(self._watch_end(token, player)))
            events.log("audio_start", {"action": action, "source": source, "fade": fade,
                                       "limit_min": limit_min})
            return {k: v for k, v in self.session.items() if k != "player"}

    async def _fade_in(self, player, target: int, seconds: int) -> None:
        steps = max(target // 5, 1)
        interval = seconds / steps
        vol = 0
        try:
            while vol < target:
                vol = min(vol + 5, target)
                await player.set_volume(vol)
                if vol < target:
                    await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 渐强断了要留痕,否则闹钟停在音量 0 无人知晓
            events.log("audio_error", {"at": "fade_in", "error": str(e)})

    async def _auto_stop(self, token: str, minutes: float) -> None:
        await asyncio.sleep(minutes * 60)
        await self.stop(token=token, reason="max_duration")

    async def _watch_end(self, token: str, player) -> None:
        """mpv 自然播完(非循环的闹钟音)后清理会话,status 不再误报 playing。"""
        await player.wait()
        await self.stop(token=token, reason="ended")

    async def set_volume(self, volume: int) -> dict | None:
        """实时调当前会话音量(mpv IPC,不中断播放)。渐强进行中会被取消——手动即最高优先。"""
        async with self._lock:
            if self.session is None:
                return None
            v = max(0, min(100, int(volume)))
            if self._fade_task and not self._fade_task.done():
                self._fade_task.cancel()
            await self.session["player"].set_volume(v)
            self.session["target_volume"] = v
            events.log("audio_volume", {"volume": v})
            return self.status()

    async def stop(self, token: str | None = None, reason: str = "manual") -> bool:
        async with self._lock:
            was = dict(self.session) if self.session else None
            stopped = await self._stop_locked(token, reason)
        # 闹钟被人为停掉 → 报时+今日待办(异步,不阻塞停止响应;auto_stop/替换/关机不报)
        if stopped and was and was["action"] == "alarm" and reason == "manual":
            from . import tts

            asyncio.create_task(tts.announce_morning())
        return stopped

    async def _stop_locked(self, token: str | None = None, reason: str = "manual") -> bool:
        """token=None 强制停;带 token 时仅当匹配当前会话才停(过期停止链接无效)。"""
        if self.session is None:
            return False
        if token is not None and token != self.session["token"]:
            return False
        for t in self._tasks:
            if not t.done() and t is not asyncio.current_task():
                t.cancel()
        self._tasks = []
        session, self.session = self.session, None
        try:
            await session["player"].stop()
        except Exception as e:  # noqa: BLE001
            events.log("audio_error", {"error": str(e), "at": "stop"})
        events.log("audio_stop", {"action": session["action"], "reason": reason})
        return True

    async def announce_file(self, path: str) -> bool:
        """播报一段短音频:暂停当前会话(如白噪音)→ 播完 → 自动恢复。"""
        prev: dict | None = None
        async with self._lock:
            if self.session is not None:
                prev = self.session.get("payload")
                await self._stop_locked(reason="announce")
            player = self._player()
            vol = int(store.get("tts.volume", 80) or 80)
            try:
                await player.play(path, volume=vol, loop=False)
            except Exception as e:  # noqa: BLE001
                events.log("tts_error", {"error": str(e)[:200], "at": "announce_play"})
                player = None
            self._announcing = player is not None
        if player is None:
            ok = False
        else:
            try:
                await asyncio.wait_for(player.wait(), timeout=60)
                ok = True
            except asyncio.TimeoutError:
                await player.stop()
                ok = False
            finally:
                self._announcing = False
        if prev is not None and self.session is None:  # 期间没人开新播放才恢复
            try:
                await self.start(prev)
            except Exception as e:  # noqa: BLE001
                events.log("audio_error", {"error": str(e)[:200], "at": "announce_resume"})
        return ok

    def status(self) -> dict:
        if getattr(self, "_announcing", False):
            return {"playing": True, "action": "announce"}
        if self.session is None:
            return {"playing": False}
        return {"playing": True,
                **{k: v for k, v in self.session.items() if k not in ("token", "player", "payload")}}


audio_manager = AudioManager()
