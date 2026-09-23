"""单一播放会话管理:同一时刻只放一个东西;渐强、停止 token、最长时长安全阀。
start/stop 加互斥锁防并发交错;player 实例钉在会话上(播放中切 backend 不影响停止)。
播报(announce_file)会先暂停当前会话、念完再「从原处」恢复:同一个停止 token(Bark 里的停止链接照样有效)、
同样的开始/结束时刻、当前音量,渐强没走完接着走;播报期间的 stop / set_volume 作用在被暂停的会话上。"""

import asyncio
import secrets
import time
from datetime import timedelta
from pathlib import Path

from .. import bg, clock, events, store
from ..config import settings
from .cast_player import CastPlayer
from .mpv_player import MpvPlayer

_NOISE_JA = {"brown": "ブラウン", "pink": "ピンク"}


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


def _label(payload: dict) -> str:
    """给人(和 LLM)看的名字;不暴露文件路径。"""
    if payload.get("label"):
        return str(payload["label"])
    action = payload.get("action", "white_noise")
    if action == "alarm":
        return "アラーム"
    if action == "white_noise":
        if payload.get("source"):
            return "ホワイトノイズ"
        preset = payload.get("preset", "brown")
        return f"ホワイトノイズ({_NOISE_JA.get(preset, preset)})"
    return "音声ファイル"


class AudioManager:
    def __init__(self) -> None:
        self._players: dict[str, object] = {}
        self.session: dict | None = None
        self._tasks: list[asyncio.Task] = []
        self._fade_task: asyncio.Task | None = None
        self._fade_until = 0.0              # 渐强预计结束的 monotonic 时刻(暂停时算剩余)
        self._paused: dict | None = None    # 播报期间被暂停的会话快照
        self._announcing = False
        self._lock = asyncio.Lock()

    def _player(self):
        backend = store.get("audio.backend", "mpv")
        if backend not in self._players:
            self._players[backend] = CastPlayer() if backend == "cast" else MpvPlayer()
        return self._players[backend]

    async def start(self, payload: dict) -> dict:
        """payload: {action: alarm|white_noise|file, source?, preset?, label?, volume?/target_volume?,
        fade_in_sec?, duration_min?, max_min?}。返回 session(含停止 token)。"""
        async with self._lock:
            return await self._start_locked(payload)

    async def _start_locked(self, payload: dict, resume: dict | None = None) -> dict:
        """resume=暂停快照时:沿用原 token / 开始结束时刻 / 音量,渐强只走剩下的部分。"""
        await self._stop_locked(reason="replaced")
        self._paused = None       # 新的播放取代被暂停的会话,播报结束后不再恢复它
        action = payload.get("action", "white_noise")
        source = _resolve_source(payload)
        loop = bool(payload.get("loop", action == "white_noise"))
        if resume:
            token, started_at, ends_at = resume["token"], resume["started_at"], resume["ends_at"]
            target, vol, fade = resume["target_volume"], resume["volume_now"], resume["fade_left"]
        else:
            target = int(payload.get("target_volume", payload.get("volume", 70)))
            fade = float(payload.get("fade_in_sec", 0))
            limit_min = payload.get("duration_min") or payload.get("max_min")
            token, started_at = secrets.token_urlsafe(8), clock.now_iso()
            ends_at = clock.iso(clock.now_utc() + timedelta(minutes=float(limit_min))) if limit_min else None
            vol = 0 if fade else target

        player = self._player()
        await player.play(source, volume=vol, loop=loop)
        self.session = {
            "token": token,
            "action": action,
            "label": _label(payload),
            "source": source,
            "target_volume": target,
            "volume_now": vol,
            "started_at": started_at,
            "ends_at": ends_at,
            "player": player,
            "payload": dict(payload),
        }
        if fade and vol < target:
            self._fade_until = time.monotonic() + fade
            self._fade_task = asyncio.create_task(self._fade(player, vol, target, fade))
            self._tasks.append(self._fade_task)
        if ends_at:
            left = (clock.parse_iso(ends_at) - clock.now_utc()).total_seconds()
            self._tasks.append(asyncio.create_task(self._auto_stop(token, max(left, 0.0))))
        if hasattr(player, "wait"):
            self._tasks.append(asyncio.create_task(self._watch_end(token, player)))
        if resume:
            events.log("audio_resume", {"action": action, "volume": vol, "fade_left": round(fade, 1)})
        else:
            events.log("audio_start", {"action": action, "source": source, "fade": fade,
                                       "ends_at": ends_at})
        return {k: v for k, v in self.session.items() if k != "player"}

    async def _fade(self, player, start: int, target: int, seconds: float) -> None:
        steps = max((target - start) // 5, 1)
        interval = seconds / steps
        vol = start
        try:
            while vol < target:
                vol = min(vol + 5, target)
                await player.set_volume(vol)
                if self.session is not None and self.session["player"] is player:
                    self.session["volume_now"] = vol
                if vol < target:
                    await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 渐强断了要留痕,否则闹钟停在音量 0 无人知晓
            events.log("audio_error", {"at": "fade_in", "error": str(e)})

    async def _auto_stop(self, token: str, seconds: float) -> None:
        await asyncio.sleep(seconds)
        await self.stop(token=token, reason="max_duration")

    async def _watch_end(self, token: str, player) -> None:
        """mpv 自然播完(非循环的闹钟音)后清理会话,status 不再误报 playing。"""
        await player.wait()
        await self.stop(token=token, reason="ended")

    async def set_volume(self, volume: int) -> dict | None:
        """实时调当前会话音量(mpv IPC,不中断播放)。渐强进行中会被取消——手动即最高优先。
        播报期间调的是被暂停的会话,恢复时生效。什么都没在放返回 None。"""
        async with self._lock:
            v = max(0, min(100, int(volume)))
            if self.session is None:
                if self._paused is None:
                    return None
                self._paused.update(target_volume=v, volume_now=v, fade_left=0.0)
                events.log("audio_volume", {"volume": v, "paused": True})
                return self.status()
            if self._fade_task and not self._fade_task.done():
                self._fade_task.cancel()
            await self.session["player"].set_volume(v)
            self.session["target_volume"] = self.session["volume_now"] = v
            events.log("audio_volume", {"volume": v})
            return self.status()

    async def stop(self, token: str | None = None, reason: str = "manual") -> bool:
        async with self._lock:
            was = dict(self.session) if self.session else None
            stopped = await self._stop_locked(token, reason)
            if not stopped and self._paused is not None and token in (None, self._paused["token"]):
                # 播报期间停:丢掉快照,念完不再恢复
                was, self._paused = self._paused, None
                events.log("audio_stop", {"action": was["action"], "reason": reason, "paused": True})
                stopped = True
        # 闹钟被人为停掉 → 报时+今日待办(异步,不阻塞停止响应;auto_stop/替换/关机不报)
        if stopped and was and was["action"] == "alarm" and reason == "manual":
            from . import tts

            bg.spawn(tts.announce_morning(), name="announce_morning")
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

    def _snapshot(self) -> dict:
        s = self.session
        fading = self._fade_task is not None and not self._fade_task.done()
        return {"payload": s["payload"], "action": s["action"], "label": s["label"], "token": s["token"],
                "started_at": s["started_at"], "ends_at": s["ends_at"],
                "target_volume": s["target_volume"], "volume_now": s["volume_now"],
                "fade_left": max(0.0, self._fade_until - time.monotonic()) if fading else 0.0}

    async def announce_file(self, path: str) -> bool:
        """播报一段短音频:暂停当前会话(如白噪音)→ 播完 → 从原处恢复(见模块说明)。"""
        async with self._lock:
            if self.session is not None:
                self._paused = self._snapshot()
                await self._stop_locked(reason="announce")
            player = self._player()
            vol = int(store.get("tts.volume", 80) or 80)
            try:
                await player.play(path, volume=vol, loop=False)
            except Exception as e:  # noqa: BLE001
                events.log("tts_error", {"error": str(e)[:200], "at": "announce_play"})
                player = None
            self._announcing = player is not None
        ok = False
        if player is not None:
            try:
                await asyncio.wait_for(player.wait(), timeout=60)
                ok = True
            except asyncio.TimeoutError:
                await player.stop()
        async with self._lock:
            self._announcing = False
            snap, self._paused = self._paused, None
            if snap is not None and self.session is None:  # 期间有人开了新播放就不恢复
                if snap["ends_at"] and clock.parse_iso(snap["ends_at"]) <= clock.now_utc():
                    events.log("audio_stop", {"action": snap["action"], "reason": "max_duration",
                                              "paused": True})
                else:
                    try:
                        await self._start_locked(snap["payload"], resume=snap)
                    except Exception as e:  # noqa: BLE001
                        events.log("audio_error", {"error": str(e)[:200], "at": "announce_resume"})
        return ok

    def status(self) -> dict:
        if self._announcing:
            return {"playing": True, "action": "announce",
                    "paused": self._paused["action"] if self._paused else None,
                    "paused_label": self._paused["label"] if self._paused else None,
                    "paused_volume": self._paused["target_volume"] if self._paused else None}
        if self.session is None:
            return {"playing": False}
        return {"playing": True,
                **{k: v for k, v in self.session.items() if k not in ("token", "player", "payload")}}


audio_manager = AudioManager()
