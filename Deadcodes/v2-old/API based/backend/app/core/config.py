from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Paths
    db_path: str = "mfd_local.db"
    nav_text_dir: str = "nav_data"
    reports_dir: str = "Reports"
    bse_scheme_dir: str = "Reports/Bse/scheme_master_auto_download"
    mailback_base_dir: str = "mailback_sync"
    logs_dir: str = "logs"

    # JWT (used in Step 3)
    jwt_secret: str = "CHANGE_ME_BEFORE_PROD"
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 60 * 24

    # App
    app_name: str = "MFD Portfolio Intelligence"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    tz: str = "Asia/Kolkata"

    # Bootstrap admin (seeded by CLI in Step 3)
    admin_username: str = "admin"
    admin_password: str = "changeme"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{Path(self.db_path).resolve()}"


settings = Settings()