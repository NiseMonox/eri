"""Cast 后端预留:将来加网络音箱时用 go-chromecast 实现(音源经 /media HTTP 提供)。"""


class CastPlayer:
    async def play(self, source: str, volume: int = 100, loop: bool = False) -> None:
        raise NotImplementedError(
            "cast 后端未实装(当前方案为 USB 音箱 + mpv)。需要时在 settings 里切回 audio.backend=mpv"
        )

    async def set_volume(self, volume: int) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        return None

    def running(self) -> bool:
        return False
