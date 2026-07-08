from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routes import router
from app.upstream import close_client


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await close_client()


app = FastAPI(
    title="Drama Short Android API",
    description=(
        "Clean Android-client backend for Drama Short. "
        "Send Authorization: Bearer <token> on authenticated requests after registering the device."
    ),
    version="1.0.0",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "Auth", "description": "Device registration and per-device guest identity."},
        {"name": "User", "description": "Current user profile and feedback."},
        {"name": "Discovery", "description": "Home, search, tags, and discovery feeds."},
        {"name": "Films", "description": "Film lists, details, episodes, and recommendations."},
        {"name": "Playback", "description": "Episode lists, HLS playback URLs, watch progress, and unlocks."},
        {"name": "Engagement", "description": "Follow, like, and film interactions."},
        {"name": "Library", "description": "Watch history and followed films."},
        {"name": "Episodes", "description": "Episode unlock state and unlock actions."},
        {"name": "Reminders", "description": "Reminder list and reminder toggles."},
        {"name": "Payments", "description": "Coin packages, subscription packages, and payment history."},
        {"name": "Events", "description": "Analytics/event forwarding for the Android app."},
        {"name": "Config", "description": "App menus and supported languages."},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
