from __future__ import annotations

from pydantic import BaseModel, Field


class DeviceAuthRequest(BaseModel):
    device_id: str = Field(..., examples=["android-install-id-123"])
    language: str = Field("hi", examples=["hi"])


class WatchProgressRequest(BaseModel):
    progress_seconds: int = Field(..., ge=0, examples=[42])
    duration_seconds: int | None = Field(None, ge=0, examples=[180])
    completed: bool = Field(False, examples=[False])
