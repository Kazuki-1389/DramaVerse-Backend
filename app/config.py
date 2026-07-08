from __future__ import annotations

import os
import uuid
from dataclasses import dataclass


def _clean_base_url(value: str) -> str:
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    web_base: str = _clean_base_url(os.getenv("DRAMAHUB_WEB_BASE", "https://dramahub.me"))
    app_base: str = _clean_base_url(os.getenv("DRAMAHUB_APP_BASE", "https://api.dramatv.app"))
    cdn_base: str = _clean_base_url(os.getenv("DRAMAHUB_CDN_BASE", "https://ccdn.dramahub.me"))
    bearer_token: str | None = os.getenv("DRAMAHUB_BEARER_TOKEN") or None
    wrapper_token_secret: str = os.getenv("DRAMAHUB_WRAPPER_TOKEN_SECRET", os.getenv("DRAMAHUB_DEVICE_ID", f"dramahub-wrapper-{uuid.getnode():x}"))
    default_upstream: str = os.getenv("DRAMAHUB_DEFAULT_UPSTREAM", "web").lower()
    default_language: str = os.getenv("DRAMAHUB_DEFAULT_LANGUAGE", "hi")
    device_id: str = os.getenv("DRAMAHUB_DEVICE_ID", f"dramahub-wrapper-{uuid.getnode():x}")
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv("DRAMAHUB_CORS_ORIGINS", "*").split(",")
        if origin.strip()
    )

    def base_for(self, upstream: str) -> str:
        if upstream == "web":
            return self.web_base
        if upstream == "app":
            return self.app_base
        if upstream == "cdn":
            return self.cdn_base
        raise ValueError(f"Unsupported upstream: {upstream}")


settings = Settings()
