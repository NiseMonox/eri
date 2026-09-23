from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    api_token: str = "change-me"

    bark_url: str = "http://127.0.0.1:8380"
    bark_device_key: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    deepseek_api_key: str = ""

    withings_client_id: str = ""
    withings_client_secret: str = ""
    withings_redirect_uri: str = ""   # Withings 后台注册的公网回调;空=用 base_url 推导(私网多半被拒,走手动贴 code)

    # NAS 每日快照推送(smbclient);host 留空=功能关闭
    nas_smb_host: str = ""
    nas_smb_share: str = ""
    nas_smb_user: str = ""
    nas_smb_pass: str = ""
    nas_smb_dir: str = "Health"

    # 监听地址/端口由 systemd 单元与 Makefile 的 uvicorn 参数决定(8300);改端口要同步改 base_url
    base_url: str = "http://localhost:8300"
    db_path: Path = Path("data/healthhub.db")
    media_dir: Path = Path("media")


settings = Settings()
