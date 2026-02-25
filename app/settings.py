# app/settings.py
from __future__ import annotations
import os, secrets
from typing import Literal, Optional
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EnvName = Literal["dev", "prod"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EXP_",
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    env: EnvName = Field(default="dev")
    bind_host: str = Field(default="127.0.0.1")
    bind_port: int = Field(default=8001, ge=1, le=65535)

    # NOTE: reads DATABASE_URL directly (no EXP_ prefix) — Railway injects this.
    database_url: Optional[str] = Field(default=None, validation_alias="DATABASE_URL")

    secret_key: Optional[str] = Field(default=None, repr=False)
    jwt_secret: Optional[str] = Field(default=None, repr=False)
    jwt_issuer: str = Field(default="ogx-expedition")
    jwt_audience: str = Field(default="ogx-oracle-users")  # shared with ogx-oraclev2
    jwt_access_minutes: int = Field(default=60 * 24, ge=5)

    # Shared user table prefix — same DB as ogx-oraclev2
    # Set EXP_ORACLE_DB_URL if expedition DB != oracle DB (optional)
    oracle_database_url: Optional[str] = Field(default=None, repr=False)

    allow_registration: bool = Field(default=False)  # registration handled by ogx-oraclev2
    max_paste_bytes: int = Field(default=500_000)
    max_expeditions_per_import: int = Field(default=500)

    bcrypt_rounds: int = Field(default=12, ge=10, le=16)
    username_min_len: int = Field(default=3)
    username_max_len: int = Field(default=32)
    password_min_len: int = Field(default=10)

    @model_validator(mode="after")
    def _finalize(self) -> "Settings":
        if self.env == "dev":
            if not self.secret_key:
                self.secret_key = secrets.token_urlsafe(32)
            if not self.jwt_secret:
                self.jwt_secret = secrets.token_urlsafe(48)
        if self.env == "prod":
            if not self.database_url:
                raise ValueError("DATABASE_URL required in prod")
            if not self.secret_key:
                raise ValueError("EXP_SECRET_KEY required in prod")
            if not self.jwt_secret:
                raise ValueError("EXP_JWT_SECRET required in prod")
            if self.bind_host == "0.0.0.0" and os.getenv("EXP_ALLOW_PUBLIC_BIND") != "1":
                raise ValueError("Set EXP_ALLOW_PUBLIC_BIND=1 to bind 0.0.0.0 in prod")
        return self


settings = Settings()
