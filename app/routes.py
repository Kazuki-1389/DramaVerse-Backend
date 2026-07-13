from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
import io
import json
import os
import struct
from typing import Annotated
from typing import Any
from urllib.parse import quote
import zlib

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Security
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.schemas import DeviceAuthRequest, NotificationRequest, PlannerItemRequest, RewardActionRequest, WatchProgressRequest
from app.store import state_store, state_store_backend, state_store_error
from app.upstream import capture_device_session, device_id_for_wrapper_token, extract_device_id, get_device_session, proxy_request


router = APIRouter()
bearer_scheme = HTTPBearer(auto_error=False)


async def require_client_auth(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)] = None,
    x_device_id: Annotated[
        str | None,
        Header(
            alias="X-Device-Id",
            description="Legacy fallback. Prefer Authorization: Bearer token from POST /client/auth/device.",
            examples=["android-install-id-123"],
        ),
    ] = None,
) -> str:
    if credentials and credentials.scheme.lower() == "bearer":
        device_id = await device_id_for_wrapper_token(credentials.credentials)
        if device_id:
            return device_id
        raise HTTPException(status_code=401, detail="Invalid bearer token")

    if x_device_id:
        return x_device_id

    raise HTTPException(status_code=401, detail="Missing bearer token")


DEVICE_AUTH = [Depends(require_client_auth)]


async def proxy_json(request: Request, upstream: str, path: str, extra_query: dict[str, object] | None = None) -> dict[str, Any]:
    response = await proxy_request(request, upstream, path, extra_query, method_override="GET")
    body = getattr(response, "body", b"")
    if isinstance(body, memoryview):
        body = body.tobytes()
    if not body:
        return {}
    return json.loads(body.decode("utf-8"))


async def optional_proxy_json(
    request: Request,
    upstream: str,
    path: str,
    extra_query: dict[str, object] | None = None,
) -> dict[str, Any]:
    try:
        return await proxy_json(request, upstream, path, extra_query)
    except Exception:
        return {}


async def request_json_body_for_event(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {"value": payload}


def film_episodes(film: dict[str, Any]) -> list[dict[str, Any]]:
    episodes = film.get("episodes")
    if isinstance(episodes, list):
        return [episode for episode in episodes if isinstance(episode, dict)]

    episode = film.get("episode") or film.get("episode_watching")
    return [episode] if isinstance(episode, dict) else []


def public_episode(episode: dict[str, Any]) -> dict[str, Any]:
    unlocked = episode_is_unlocked(episode)
    episode_number = episode_number_value(episode)
    return {
        "episode_id": episode.get("episode_id"),
        "episode": episode.get("episode") or episode_number,
        "title": episode.get("title"),
        "is_vip": 1 if episode_is_paid(episode) else 0,
        "price": episode.get("price", 0),
        "is_unlocked": 1 if unlocked else 0,
        "unlock_required": not unlocked,
        "is_publish": episode.get("is_publish", 1),
        "is_like": episode.get("is_like", 0),
    }


def playback_episode(episode: dict[str, Any]) -> dict[str, Any]:
    return {
        **public_episode(episode),
        "playback": {
            "hls_url": episode.get("link"),
            "backup_hls_url": episode.get("backup_link"),
            "subtitles": episode.get("subtitles") if isinstance(episode.get("subtitles"), dict) else {},
        },
    }


EpisodeRef = Annotated[
    int,
    Path(
        description="Episode number like 1, 2, 3. Episode ids such as 8755 are also accepted.",
        examples=[1],
    ),
]

FilmId = Annotated[int, Path(description="Film/series id.", examples=[167])]


def find_episode(episodes: list[dict[str, Any]], episode_ref: int) -> dict[str, Any] | None:
    for episode in episodes:
        if episode_number_value(episode) == episode_ref or int_value(episode.get("episode_id"), -1) == episode_ref:
            return episode
    return None


def bool_state(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes"}:
            return True
        if normalized in {"0", "false", "no"}:
            return False
    return None


def int_value(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value.strip().isdigit() and int(value.strip()) or default
    return default


def episode_number_value(episode: dict[str, Any]) -> int:
    return int_value(episode.get("episode") or episode.get("episode_number") or episode.get("number"), 1)


def episode_is_paid(episode: dict[str, Any]) -> bool:
    is_vip = next(
        (parsed for key in ("is_vip", "vip", "is_paid") if (parsed := bool_state(episode.get(key))) is not None),
        False,
    )
    price = next(
        (parsed for key in ("price", "coin_price", "unlock_price") if (parsed := int_value(episode.get(key), -1)) >= 0),
        0,
    )
    return bool(is_vip) or price > 0


def episode_is_unlocked(episode: dict[str, Any]) -> bool:
    if episode_number_value(episode) <= 1:
        return True

    explicit = next(
        (parsed for key in ("is_unlocked", "unlocked") if (parsed := bool_state(episode.get(key))) is not None),
        None,
    )
    if explicit is not None:
        return explicit
    return False


def _state_set(state: dict[str, Any], key: str) -> set[Any]:
    values = state.get(key)
    if not isinstance(values, list):
        return set()
    return {tuple(value) if isinstance(value, list) else value for value in values}


def _dump_state_sets(state: dict[str, set[Any]]) -> dict[str, Any]:
    return {
        key: [list(value) if isinstance(value, tuple) else value for value in values]
        for key, values in state.items()
    }


def persist_in_background(coro: Coroutine[Any, Any, Any]) -> None:
    task = asyncio.create_task(coro)
    task.add_done_callback(_consume_background_exception)


def _consume_background_exception(task: asyncio.Task[Any]) -> None:
    try:
        task.exception()
    except asyncio.CancelledError:
        return


def first_string(value: dict[str, Any], *keys: str) -> str:
    for key in keys:
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return str(raw)
    return ""


def profile_payload(request: Request, device_id: str, user: dict[str, Any], upstream: dict[str, Any] | None = None) -> dict[str, Any]:
    guest_id = first_string(user, "id", "user_id", "uid", "auth_id", "guest_id").strip() or device_id
    display_name = first_string(user, "name", "username", "user_name", "nick_name", "nickname", "display_name")
    if not display_name:
        display_name = f"Guest {guest_id[-6:]}" if len(guest_id) > 6 else f"Guest {guest_id}"
    avatar_url = first_string(user, "avatar", "avatar_url", "profile_pic", "profile_picture", "photo", "image")
    if not avatar_url:
        avatar_url = f"{request.url_for('client_profile_avatar_png')}?device_id={quote(device_id)}"
    return {
        "status": True,
        "data": {
            "display_name": display_name,
            "username": display_name,
            "guest_id": guest_id,
            "device_id": device_id,
            "profile_pic_png": avatar_url,
            "avatar_url": avatar_url,
            "is_guest": True,
            "raw_user": user,
            "upstream": upstream or {},
        },
    }


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def generated_avatar_png(seed: str, size: int = 192) -> bytes:
    digest = zlib.crc32(seed.encode("utf-8"))
    base = (
        80 + digest % 120,
        70 + (digest >> 8) % 120,
        95 + (digest >> 16) % 110,
    )
    accent = (
        min(255, base[0] + 36),
        min(255, base[1] + 28),
        min(255, base[2] + 48),
    )
    rows = []
    center = (size - 1) / 2
    radius = size * 0.38
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            distance = ((x - center) ** 2 + (y - center) ** 2) ** 0.5
            if distance <= radius:
                mix = max(0.0, 1.0 - distance / radius)
                color = tuple(int(base[i] * (1 - mix) + accent[i] * mix) for i in range(3))
            else:
                color = (18, 16, 21)
            row.extend((*color, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)
    stream = io.BytesIO()
    stream.write(b"\x89PNG\r\n\x1a\n")
    stream.write(png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)))
    stream.write(png_chunk(b"IDAT", zlib.compress(raw, 9)))
    stream.write(png_chunk(b"IEND", b""))
    return stream.getvalue()


async def engagement_state(device_id: str) -> dict[str, set[Any]]:
    state = await state_store.get_engagement(device_id)
    return {
        "followed_films": _state_set(state, "followed_films"),
        "unfollowed_films": _state_set(state, "unfollowed_films"),
        "liked_episodes": _state_set(state, "liked_episodes"),
        "unliked_episodes": _state_set(state, "unliked_episodes"),
        "reminded_films": _state_set(state, "reminded_films"),
        "unreminded_films": _state_set(state, "unreminded_films"),
    }


async def save_engagement_state(device_id: str, state: dict[str, set[Any]]) -> None:
    await state_store.save_engagement(device_id, _dump_state_sets(state))


def episode_key(film_id: int, episode: dict[str, Any]) -> tuple[int, int | str | None]:
    return film_id, episode.get("episode_id") or episode.get("episode")


async def apply_engagement_overrides(device_id: str, film: dict[str, Any]) -> dict[str, Any]:
    state = await engagement_state(device_id)
    film_id = film.get("id")
    if isinstance(film_id, int):
        if film_id in state["followed_films"]:
            film["is_follow"] = 1
        elif film_id in state["unfollowed_films"]:
            film["is_follow"] = 0
        if film_id in state["reminded_films"]:
            film["is_reminder"] = 1
        elif film_id in state["unreminded_films"]:
            film["is_reminder"] = 0

        for episode in film_episodes(film):
            key = episode_key(film_id, episode)
            if key in state["liked_episodes"]:
                episode["is_like"] = 1
            elif key in state["unliked_episodes"]:
                episode["is_like"] = 0

    return film


async def film_payload_with_overrides(request: Request, film_id: int) -> dict[str, Any]:
    payload = await proxy_json(request, "app", "api/info_film", {"film_id": film_id})
    film = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    device_id = await extract_device_id(request)
    if film:
        await apply_engagement_overrides(device_id, film)
    return payload

WEB_ENDPOINTS = [
    "GET /api/home_feature -> app /api/film_for_you when no web token is configured",
    "GET /api/history_watching_film -> app fallback",
    "GET /api/list_films -> app fallback",
    "GET /api/user/get_info -> app fallback",
    "GET /api/film_hot_search -> app /api/film_for_you fallback",
    "GET /api/info_film -> app fallback",
    "GET /api/film_languages",
    "GET /payment/subscription_packages",
    "GET /payment/coin_packages",
    "GET /video",
]

APP_ENDPOINTS = [
    "POST /auth/user/login",
    "GET /api/user/get_info",
    "POST /api/user/update_info",
    "POST /api/user/device_token",
    "POST /api/user/user_feedback",
    "GET /api/get_tags",
    "GET /api/get_film_by_tags",
    "GET /api/list_films",
    "GET /api/film_for_you",
    "GET /api/info_film",
    "GET /api/more_like_this",
    "GET /api/film_list_area",
    "GET /api/follow_list_film",
    "POST /api/follow_film",
    "POST /api/like_film",
    "POST /api/watching_film",
    "GET /api/history_watching_film",
    "GET /api/history_follow_film",
    "POST /api/unlock_episode",
    "GET /api/episode_unlocked",
    "POST /api/toggle_reminder",
    "GET /api/user_reminders",
    "GET /api/payment_history_v2",
    "GET /api/recent_payments",
    "POST /api/payment_sub2",
    "GET /api/events",
    "GET /api/menus",
    "POST /api/action_events",
    "POST /api/event_ref",
    "POST /api/film_click_search",
]


@router.get("/health", include_in_schema=False)
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "upstreams": {
            "web": settings.web_base,
            "app": settings.app_base,
            "cdn": settings.cdn_base,
            "default": settings.default_upstream,
        },
        "auth": {
            "mode": "wrapper bearer token from /client/auth/device, env bearer, or generated per-device upstream guest token",
            "guest_login": f"{settings.app_base}/auth/user/login",
            "client_auth": "Authorization: Bearer <token>",
            "legacy_device_headers": ["X-Device-Id", "device_id"],
        },
        "store": {
            "backend": state_store_backend,
            "firestore_project": os.getenv("FIREBASE_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT"),
            "collection_prefix": os.getenv("FIRESTORE_COLLECTION_PREFIX", "dramaverse"),
            "error": state_store_error,
        },
    }


@router.get("/endpoints", include_in_schema=False)
async def endpoints() -> dict[str, object]:
    return {
        "web": {
            "base_route": "/web/{path}",
            "upstream": settings.web_base,
            "documented": WEB_ENDPOINTS,
        },
        "app": {
            "base_route": "/app/{path}",
            "upstream": settings.app_base,
            "documented": APP_ENDPOINTS,
        },
        "cdn": {
            "base_route": "/cdn/{path}",
            "upstream": settings.cdn_base,
            "examples": [
                "GET /cdn/films/{hash}/index.m3u8",
                "GET /cdn/subtitles/{hash}.vtt",
                "GET /cdn/thumbs/{hash}.webp",
            ],
        },
        "compatibility": {
            "route": "/{path}",
            "default_upstream": settings.default_upstream,
            "override_query": "?upstream=web or ?upstream=app",
        },
        "device_capture": {
            "routes": [
                "POST /device/capture",
                "POST /auth/device",
                "POST /api/user/device_token",
            ],
            "accepted_device_fields": ["device_id", "deviceId", "device", "uuid", "install_id"],
            "accepted_headers": ["Authorization: Bearer <token>", "X-Device-Id", "device-id"],
        },
    }


@router.post("/device/capture", include_in_schema=False)
async def device_capture(request: Request) -> dict[str, object]:
    return await capture_device_session(request)


@router.post("/auth/device", include_in_schema=False)
async def auth_device(request: Request) -> dict[str, object]:
    return await capture_device_session(request)


@router.post("/api/user/device_token", include_in_schema=False)
async def device_token_capture(request: Request) -> dict[str, object]:
    return await capture_device_session(request)


async def auth_user_login(request: Request) -> dict[str, object]:
    return await capture_device_session(request)


async def proxy_app_endpoint(path: str, request: Request) -> Response:
    return await proxy_request(request, "app", path)


async def proxy_web_endpoint(path: str, request: Request) -> Response:
    return await proxy_request(request, "web", path)


def make_proxy_endpoint(
    upstream: str,
    path: str,
) -> Callable[[Request], Coroutine[Any, Any, Response]]:
    async def endpoint(request: Request) -> Response:
        return await proxy_request(request, upstream, path)

    endpoint.__name__ = f"{upstream}_{path.replace('/', '_')}"
    return endpoint


def add_proxy_route(
    method: str,
    route_path: str,
    upstream: str,
    upstream_path: str | None = None,
    include_in_schema: bool = True,
) -> None:
    router.add_api_route(
        route_path,
        make_proxy_endpoint(upstream, upstream_path or route_path.lstrip("/")),
        methods=[method],
        name=f"{method} {route_path}",
        include_in_schema=include_in_schema,
    )


router.add_api_route(
    "/auth/user/login",
    auth_user_login,
    methods=["POST"],
    name="Create Device Guest User",
    include_in_schema=False,
)

for route in [
    "/api/user/update_info",
    "/api/user/user_feedback",
    "/api/follow_film",
    "/api/like_film",
    "/api/watching_film",
    "/api/unlock_episode",
    "/api/toggle_reminder",
    "/api/payment_sub2",
    "/api/action_events",
    "/api/event_ref",
    "/api/film_click_search",
]:
    add_proxy_route("POST", route, "app", include_in_schema=False)

add_proxy_route("POST", "/api/user/get_info", "app", include_in_schema=False)

for route in [
    "/api/user/get_info",
    "/api/history_follow_film",
    "/api/get_tags",
    "/api/get_film_by_tags",
    "/api/list_films",
    "/api/film_for_you",
    "/api/info_film",
    "/api/more_like_this",
    "/api/film_list_area",
    "/api/follow_list_film",
    "/api/history_watching_film",
    "/api/episode_unlocked",
    "/api/user_reminders",
    "/api/payment_history_v2",
    "/api/recent_payments",
    "/api/events",
    "/api/menus",
]:
    add_proxy_route("GET", route, "app", include_in_schema=False)

for route, upstream_path in [
    ("/api/home_feature", "api/home_feature"),
    ("/api/film_hot_search", "api/film_hot_search"),
    ("/api/film_languages", "api/film_languages"),
    ("/payment/subscription_packages", "payment/subscription_packages"),
    ("/payment/coin_packages", "payment/coin_packages"),
    ("/video", "video"),
]:
    add_proxy_route("GET", route, "web", upstream_path, include_in_schema=False)


@router.post(
    "/client/auth/device",
    tags=["Auth"],
    summary="Register or resume a device user",
)
async def client_auth_device(_: DeviceAuthRequest, request: Request) -> dict[str, object]:
    return await capture_device_session(request)


@router.get("/client/me", tags=["User"], summary="Get current device user", dependencies=DEVICE_AUTH)
async def client_me(request: Request) -> JSONResponse:
    device_id = await extract_device_id(request)
    session = await get_device_session(device_id)
    user = session.get("user") if isinstance(session.get("user"), dict) else {}
    upstream = await optional_proxy_json(request, "app", "api/user/get_info", {"auth_id": user.get("id")})
    upstream_user = upstream.get("data") if isinstance(upstream.get("data"), dict) else {}
    merged_user = {**user, **upstream_user}
    return JSONResponse(profile_payload(request, device_id, merged_user, upstream))


@router.get("/client/me/avatar.png", tags=["User"], summary="Generated current user avatar PNG")
async def client_profile_avatar_png(request: Request) -> Response:
    device_id = await extract_device_id(request)
    session = await get_device_session(device_id)
    user = session.get("user") if isinstance(session.get("user"), dict) else {}
    seed = first_string(user, "id", "user_id", "uid", "auth_id", "guest_id") or device_id
    return Response(
        content=generated_avatar_png(seed),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.post(
    "/client/me",
    tags=["User"],
    summary="Update current device user",
    dependencies=DEVICE_AUTH,
    include_in_schema=False,
)
async def client_update_me(request: Request) -> Response:
    return await proxy_request(request, "app", "api/user/update_info")


@router.post("/client/feedback", tags=["User"], summary="Send user feedback", dependencies=DEVICE_AUTH)
async def client_feedback(request: Request) -> Response:
    device_id = await extract_device_id(request)
    payload = await request_json_body_for_event(request)
    persist_in_background(state_store.save_feedback(device_id, payload))
    return await proxy_request(request, "app", "api/user/user_feedback")


@router.get("/client/home", tags=["Discovery"], summary="Home feed", dependencies=DEVICE_AUTH)
async def client_home(request: Request) -> Response:
    return await proxy_request(request, "web", "api/home_feature")


@router.get("/client/for-you", tags=["Discovery"], summary="Personalized film feed", dependencies=DEVICE_AUTH)
async def client_for_you(request: Request) -> Response:
    return await proxy_request(request, "app", "api/film_for_you")


@router.get("/client/search", tags=["Discovery"], summary="Search films", dependencies=DEVICE_AUTH)
async def client_search(
    request: Request,
    query: str = Query(..., min_length=1, description="Search text typed by the user."),
) -> Response:
    return await proxy_request(request, "app", "api/list_films", {"s": query})


@router.get("/client/search/hot", tags=["Discovery"], summary="Hot search films", dependencies=DEVICE_AUTH)
async def client_hot_search(request: Request) -> Response:
    return await proxy_request(request, "web", "api/film_hot_search")


@router.get("/client/tags", tags=["Discovery"], summary="List tags", dependencies=DEVICE_AUTH)
async def client_tags(request: Request) -> Response:
    return await proxy_request(request, "app", "api/get_tags")


@router.get(
    "/client/tags/{tag_id}/films",
    tags=["Discovery"],
    summary="Films for a selected tag",
    dependencies=DEVICE_AUTH,
)
async def client_tag_films(tag_id: int, request: Request) -> Response:
    return await proxy_request(request, "app", "api/get_film_by_tags", {"tag_id": tag_id})


@router.get(
    "/client/areas",
    tags=["Discovery"],
    summary="Film areas",
    dependencies=DEVICE_AUTH,
    include_in_schema=False,
)
async def client_areas(request: Request) -> Response:
    return await proxy_request(request, "app", "api/film_list_area")


@router.get(
    "/client/areas/{slug}/films",
    tags=["Discovery"],
    summary="Films for a selected area",
    dependencies=DEVICE_AUTH,
    include_in_schema=False,
)
async def client_area_films(slug: str, request: Request) -> Response:
    return await proxy_request(request, "app", "api/film_list_area", {"slug": slug})


@router.get("/client/films", tags=["Films"], summary="List films", dependencies=DEVICE_AUTH)
async def client_films(request: Request) -> Response:
    return await proxy_request(request, "app", "api/list_films")


@router.get("/client/films/{film_id}", tags=["Films"], summary="Film details", dependencies=DEVICE_AUTH)
async def client_film_detail(film_id: int, request: Request) -> JSONResponse:
    return JSONResponse(await film_payload_with_overrides(request, film_id))


@router.get(
    "/client/films/{film_id}/episodes",
    tags=["Playback"],
    summary="List episodes for a film",
    dependencies=DEVICE_AUTH,
)
async def client_film_episodes(film_id: FilmId, request: Request) -> JSONResponse:
    payload = await film_payload_with_overrides(request, film_id)
    film = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    episodes = film_episodes(film)
    return JSONResponse(
        {
            "status": payload.get("status", False),
            "film_id": film_id,
            "episode_total": film.get("episode_total") or len(episodes),
            "episodes": [public_episode(episode) for episode in episodes],
        }
    )


@router.get(
    "/client/films/{film_id}/episodes/{episode_number}/play",
    tags=["Playback"],
    summary="Get HLS playback info for an episode",
    dependencies=DEVICE_AUTH,
)
async def client_play_episode(film_id: FilmId, episode_number: EpisodeRef, request: Request) -> JSONResponse:
    payload = await proxy_json(request, "app", "api/info_film", {"film_id": film_id})
    film = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    episodes = film_episodes(film)
    episode = find_episode(episodes, episode_number)
    if not episode:
        return JSONResponse(
            {"status": False, "message": "Episode not found", "film_id": film_id, "episode": episode_number},
            status_code=404,
        )

    current_index = episodes.index(episode)
    next_episode = episodes[current_index + 1] if current_index + 1 < len(episodes) else None
    unlocked = episode_is_unlocked(episode)

    return JSONResponse(
        {
            "status": True,
            "film": {
                "id": film.get("id"),
                "title": film.get("title"),
                "description": film.get("description") or film.get("desc") or film.get("summary") or film.get("content"),
                "thumb": film.get("thumb"),
                "rating": film.get("rating"),
                "genre": film.get("genre") or film.get("category") or film.get("tag"),
                "episode_total": film.get("episode_total") or len(episodes),
            },
            "episode": playback_episode(episode),
            "next_episode": public_episode(next_episode) if isinstance(next_episode, dict) else None,
            "unlock_required": not unlocked,
        }
    )


@router.post(
    "/client/films/{film_id}/episodes/{episode_number}/watch",
    tags=["Playback"],
    summary="Save episode watch progress",
    dependencies=DEVICE_AUTH,
)
async def client_watch_episode(
    film_id: FilmId,
    episode_number: EpisodeRef,
    progress: WatchProgressRequest,
    request: Request,
) -> Response:
    device_id = await extract_device_id(request)
    await state_store.save_watch_progress(
        device_id,
        film_id,
        episode_number,
        {
            "progress_seconds": progress.progress_seconds,
            "duration_seconds": progress.duration_seconds,
            "completed": progress.completed,
        },
    )
    rewards = await state_store.get_rewards(device_id)
    rewards["watch_minutes_today"] = max(
        int_value(rewards.get("watch_minutes_today"), 0),
        int((progress.progress_seconds or 0) / 60),
    )
    rewards["watch_minutes_day"] = reward_day_key()
    await state_store.save_rewards(device_id, rewards)
    payload = await proxy_json(request, "app", "api/info_film", {"film_id": film_id})
    film = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    episode = find_episode(film_episodes(film), episode_number)
    return await proxy_request(
        request,
        "app",
        "api/watching_film",
        {
            "film_id": film_id,
            "episode": episode_number,
            "episode_id": episode.get("episode_id") if episode else None,
            "current_time": progress.progress_seconds,
            "time_watching": progress.progress_seconds,
            "watched_duration": progress.progress_seconds,
            "episode_duration": progress.duration_seconds,
            "is_completed": 1 if progress.completed else 0,
        },
    )


@router.post(
    "/client/films/{film_id}/episodes/{episode_number}/unlock",
    tags=["Playback"],
    summary="Unlock a locked episode",
    dependencies=DEVICE_AUTH,
)
async def client_unlock_film_episode(film_id: FilmId, episode_number: EpisodeRef, request: Request) -> Response:
    payload = await proxy_json(request, "app", "api/info_film", {"film_id": film_id})
    film = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    episode = find_episode(film_episodes(film), episode_number)
    if not episode:
        return JSONResponse(
            {"status": False, "message": "Episode not found", "film_id": film_id, "episode": episode_number},
            status_code=404,
        )

    is_unlocked = episode_is_unlocked(episode)
    if is_unlocked:
        return JSONResponse(
            {
                "status": True,
                "message": "Episode is already playable.",
                "film_id": film_id,
                "episode": public_episode(episode),
                "is_unlocked": True,
                "unlock_required": False,
            }
        )

    return await proxy_request(
        request,
        "app",
        "api/unlock_episode",
        {
            "film_id": film_id,
            "episode": episode_number,
            "episode_id": episode.get("episode_id") if episode else None,
        },
    )


@router.get(
    "/client/films/{film_id}/similar",
    tags=["Films"],
    summary="More like this",
    dependencies=DEVICE_AUTH,
)
async def client_more_like_this(film_id: int, request: Request) -> Response:
    return await proxy_request(request, "app", "api/more_like_this", {"film_id": film_id})


@router.post(
    "/client/films/{film_id}/follow",
    tags=["Engagement"],
    summary="Follow a film",
    dependencies=DEVICE_AUTH,
)
async def client_follow_film(film_id: int, request: Request) -> Response:
    return await set_film_follow_state(film_id, request, True)


@router.post(
    "/client/films/{film_id}/unfollow",
    tags=["Engagement"],
    summary="Unfollow a film",
    dependencies=DEVICE_AUTH,
)
async def client_unfollow_film(film_id: int, request: Request) -> Response:
    return await set_film_follow_state(film_id, request, False)


async def set_film_follow_state(film_id: int, request: Request, should_follow: bool) -> Response:
    payload = await film_payload_with_overrides(request, film_id)
    film = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    current = bool_state(film.get("is_follow"))
    if current is should_follow:
        return JSONResponse(
            {
                "status": True,
                "message": "Film already followed." if should_follow else "Film already unfollowed.",
                "data": film,
            }
        )

    await proxy_request(request, "app", "api/follow_film", {"film_id": film_id})
    device_id = await extract_device_id(request)
    state = await engagement_state(device_id)
    if should_follow:
        state["followed_films"].add(film_id)
        state["unfollowed_films"].discard(film_id)
        film["is_follow"] = 1
    else:
        state["unfollowed_films"].add(film_id)
        state["followed_films"].discard(film_id)
        film["is_follow"] = 0
    await save_engagement_state(device_id, state)

    return JSONResponse(
        {
            "status": True,
            "message": "Follow Film Success!" if should_follow else "Unfollow Film Success!",
            "data": film,
        }
    )


@router.post(
    "/client/films/{film_id}/like",
    tags=["Engagement"],
    summary="Like the first/current episode for a film",
    dependencies=DEVICE_AUTH,
)
async def client_like_film(film_id: int, request: Request) -> Response:
    return await set_film_episode_like_state(film_id, None, request, True)


@router.post(
    "/client/films/{film_id}/unlike",
    tags=["Engagement"],
    summary="Unlike the first/current episode for a film",
    dependencies=DEVICE_AUTH,
)
async def client_unlike_film(film_id: int, request: Request) -> Response:
    return await set_film_episode_like_state(film_id, None, request, False)


@router.post(
    "/client/films/{film_id}/episodes/{episode_number}/like",
    tags=["Engagement"],
    summary="Like one episode",
    dependencies=DEVICE_AUTH,
)
async def client_like_episode(film_id: FilmId, episode_number: EpisodeRef, request: Request) -> Response:
    return await set_film_episode_like_state(film_id, episode_number, request, True)


@router.post(
    "/client/films/{film_id}/episodes/{episode_number}/unlike",
    tags=["Engagement"],
    summary="Unlike one episode",
    dependencies=DEVICE_AUTH,
)
async def client_unlike_episode(film_id: FilmId, episode_number: EpisodeRef, request: Request) -> Response:
    return await set_film_episode_like_state(film_id, episode_number, request, False)


async def set_film_episode_like_state(
    film_id: int,
    episode_number: int | None,
    request: Request,
    should_like: bool,
) -> Response:
    payload = await film_payload_with_overrides(request, film_id)
    film = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    episodes = film_episodes(film)
    episode = find_episode(episodes, episode_number) if episode_number is not None else (episodes[0] if episodes else None)
    if not episode:
        return JSONResponse(
            {"status": False, "message": "Episode not found", "film_id": film_id, "episode": episode_number or 1},
            status_code=404,
        )

    current = bool_state(episode.get("is_like"))
    if current is should_like:
        return JSONResponse(
            {
                "status": True,
                "message": "Episode already liked." if should_like else "Episode already unliked.",
                "data": {
                    "film": film,
                    "episode": public_episode(episode) | {"is_like": episode.get("is_like")},
                },
            }
        )

    await proxy_request(
        request,
        "app",
        "api/like_film",
        {
            "film_id": film_id,
            "episode_id": episode.get("episode_id"),
        },
    )
    device_id = await extract_device_id(request)
    state = await engagement_state(device_id)
    key = episode_key(film_id, episode)
    if should_like:
        state["liked_episodes"].add(key)
        state["unliked_episodes"].discard(key)
        episode["is_like"] = 1
    else:
        state["unliked_episodes"].add(key)
        state["liked_episodes"].discard(key)
        episode["is_like"] = 0
    await save_engagement_state(device_id, state)

    return JSONResponse(
        {
            "status": True,
            "message": "Like Episode Success!" if should_like else "Unlike Episode Success!",
            "data": {
                "film": film,
                "episode": public_episode(episode),
            },
        }
    )


async def followed_library_response(request: Request) -> JSONResponse:
    device_id = await extract_device_id(request)
    state = await engagement_state(device_id)
    payload = await proxy_json(request, "app", "api/history_follow_film")
    upstream_items = payload.get("data") if isinstance(payload.get("data"), list) else []
    films: list[dict[str, Any]] = []
    seen: set[int] = set()

    for item in upstream_items:
        if not isinstance(item, dict):
            continue
        film_id = item.get("id") or item.get("film_id")
        if isinstance(film_id, int) and film_id in state["unfollowed_films"]:
            continue
        if isinstance(film_id, int):
            seen.add(film_id)
        films.append(await apply_engagement_overrides(device_id, item))

    for film_id in sorted(value for value in state["followed_films"] if isinstance(value, int)):
        if film_id in seen:
            continue
        film_payload = await film_payload_with_overrides(request, film_id)
        film = film_payload.get("data") if isinstance(film_payload.get("data"), dict) else None
        if film:
            films.append(film)

    return JSONResponse(
        {
            "status": True,
            "message": payload.get("message"),
            "data": films,
        }
    )


@router.get("/client/library/following", tags=["Library"], summary="Followed films", dependencies=DEVICE_AUTH)
async def client_following(request: Request) -> JSONResponse:
    return await followed_library_response(request)


@router.get("/client/history/watch", tags=["Library"], summary="Watch history", dependencies=DEVICE_AUTH)
async def client_watch_history(request: Request) -> Response:
    device_id = await extract_device_id(request)
    progress_rows = await state_store.list_watch_progress(device_id)
    items: list[dict[str, Any]] = []
    seen_films: set[int] = set()
    for progress in progress_rows:
        film_id = int_value(progress.get("film_id"), 0)
        if film_id == 0 or film_id in seen_films:
            continue
        seen_films.add(film_id)
        film_payload = await proxy_json(request, "app", "api/info_film", {"film_id": film_id})
        film = film_payload.get("data") if isinstance(film_payload.get("data"), dict) else {}
        if not film:
            continue
        episode = int_value(progress.get("episode"), 1)
        duration_seconds = int_value(progress.get("duration_seconds"), 0)
        progress_seconds = int_value(progress.get("progress_seconds"), 0)
        items.append(
            {
                "film": {
                    "id": film.get("id") or film_id,
                    "title": film.get("title"),
                    "description": film.get("description") or film.get("desc") or film.get("summary") or film.get("content"),
                    "thumb": film.get("thumb"),
                    "rating": film.get("rating"),
                    "genre": film.get("genre") or film.get("category") or film.get("tag"),
                    "episode_total": film.get("episode_total") or len(film_episodes(film)),
                },
                "film_id": film_id,
                "episode": episode,
                "progress_seconds": progress_seconds,
                "duration_seconds": duration_seconds,
                "completed": bool_state(progress.get("completed")) or False,
                "updated_at": progress.get("updated_at"),
            }
        )
    return JSONResponse({"status": True, "data": items})


@router.get("/client/history/follow", tags=["Library"], summary="Follow history", dependencies=DEVICE_AUTH)
async def client_follow_history(request: Request) -> JSONResponse:
    return await followed_library_response(request)


@router.post(
    "/client/films/{film_id}/reminder",
    tags=["Reminders"],
    summary="Set a film reminder",
    dependencies=DEVICE_AUTH,
)
async def client_set_film_reminder(film_id: FilmId, request: Request) -> Response:
    return await set_film_reminder_state(film_id, request, True)


@router.post(
    "/client/films/{film_id}/unreminder",
    tags=["Reminders"],
    summary="Remove a film reminder",
    dependencies=DEVICE_AUTH,
)
async def client_remove_film_reminder(film_id: FilmId, request: Request) -> Response:
    return await set_film_reminder_state(film_id, request, False)


async def set_film_reminder_state(film_id: int, request: Request, should_remind: bool) -> Response:
    payload = await film_payload_with_overrides(request, film_id)
    film = payload.get("data") if isinstance(payload.get("data"), dict) else {"id": film_id}
    current = bool_state(film.get("is_reminder"))
    if current is should_remind:
        return JSONResponse(
            {
                "status": True,
                "message": "Reminder already set." if should_remind else "Reminder already removed.",
                "data": {"film_id": film_id, "is_active": 1 if should_remind else 0, "film": film},
            }
        )

    await proxy_request(request, "app", "api/toggle_reminder", {"film_id": film_id})
    device_id = await extract_device_id(request)
    state = await engagement_state(device_id)
    if should_remind:
        state["reminded_films"].add(film_id)
        state["unreminded_films"].discard(film_id)
        film["is_reminder"] = 1
    else:
        state["unreminded_films"].add(film_id)
        state["reminded_films"].discard(film_id)
        film["is_reminder"] = 0
    await save_engagement_state(device_id, state)

    return JSONResponse(
        {
            "status": True,
            "message": "Reminder added successfully" if should_remind else "Reminder removed successfully",
            "data": {"film_id": film_id, "is_active": 1 if should_remind else 0, "film": film},
        }
    )


async def reminders_response(request: Request) -> JSONResponse:
    device_id = await extract_device_id(request)
    state = await engagement_state(device_id)
    payload = await proxy_json(request, "app", "api/user_reminders")
    upstream_items = payload.get("data") if isinstance(payload.get("data"), list) else []
    reminders: list[dict[str, Any]] = []
    seen: set[int] = set()

    for item in upstream_items:
        if not isinstance(item, dict):
            continue
        film_id = item.get("film_id") or item.get("id")
        if isinstance(film_id, int) and film_id in state["unreminded_films"]:
            continue
        if isinstance(film_id, int):
            seen.add(film_id)
            film_payload = await film_payload_with_overrides(request, film_id)
            film = film_payload.get("data") if isinstance(film_payload.get("data"), dict) else None
            reminders.append(
                {
                    **item,
                    "film_id": film_id,
                    "is_active": item.get("is_active", 1),
                    "film": film,
                }
            )
        else:
            reminders.append(item)

    for film_id in sorted(value for value in state["reminded_films"] if isinstance(value, int)):
        if film_id in seen:
            continue
        film_payload = await film_payload_with_overrides(request, film_id)
        film = film_payload.get("data") if isinstance(film_payload.get("data"), dict) else {"id": film_id}
        reminders.append({"film_id": film_id, "is_active": 1, "film": film})

    return JSONResponse({"status": True, "message": payload.get("message"), "data": reminders})


CHECK_IN_REWARDS = [20, 25, 30, 35, 40, 45, 60]
WATCH_TASKS = [
    {"id": "watch_5", "title": "Watch 5 minutes", "target_minutes": 5, "reward": 15},
    {"id": "watch_10", "title": "Watch 10 minutes", "target_minutes": 10, "reward": 20},
    {"id": "watch_15", "title": "Watch 15 minutes", "target_minutes": 15, "reward": 30},
]
SPIN_REWARDS = [0, 10, 15, 20, 30, 40, 60, 100]


def reward_day_key() -> str:
    return datetime.now(UTC).date().isoformat()


def reward_week_key() -> str:
    now = datetime.now(UTC).isocalendar()
    return f"{now.year}-W{now.week}"


def daily_tasks_for_device(device_id: str) -> list[dict[str, Any]]:
    offset = sum(ord(char) for char in f"{device_id}:{reward_day_key()}") % len(WATCH_TASKS)
    ordered = WATCH_TASKS[offset:] + WATCH_TASKS[:offset]
    return [dict(task) for task in ordered]


def reward_payload(device_id: str, rewards: dict[str, Any]) -> dict[str, Any]:
    today = reward_day_key()
    claimed_task_day = rewards.get("claimed_task_day")
    claimed_tasks = rewards.get("claimed_tasks") if claimed_task_day == today else []
    if not isinstance(claimed_tasks, list):
        claimed_tasks = []
    current_day = int_value(rewards.get("check_in_day"), 1) or 1
    current_day = min(max(current_day, 1), 7)
    last_claimed_check_in_day = int_value(rewards.get("last_claimed_check_in_day"), 0)
    last_spin_week = rewards.get("last_spin_week")
    last_spin_reward = int_value(rewards.get("last_spin_reward"), 0)
    spin_index = SPIN_REWARDS.index(last_spin_reward) if last_spin_reward in SPIN_REWARDS else 0
    watch_minutes = int_value(rewards.get("watch_minutes_today"), 0) if rewards.get("watch_minutes_day") == today else 0
    return {
        **rewards,
        "coins": int_value(rewards.get("coins"), 0),
        "check_in_day": current_day,
        "last_check_in": rewards.get("last_check_in"),
        "can_check_in": rewards.get("last_check_in") != today,
        "check_in_rewards": [
            {
                "day": index + 1,
                "reward": reward,
                "claimed": (
                    index + 1 < current_day
                    or (rewards.get("last_check_in") == today and index + 1 <= last_claimed_check_in_day)
                ),
                "current": rewards.get("last_check_in") != today and index + 1 == current_day,
                "status": (
                    "claimed"
                    if index + 1 < current_day
                    or (rewards.get("last_check_in") == today and index + 1 <= last_claimed_check_in_day)
                    else "today"
                    if rewards.get("last_check_in") != today and index + 1 == current_day
                    else "locked"
                ),
            }
            for index, reward in enumerate(CHECK_IN_REWARDS)
        ],
        "daily_tasks": [
            {
                **task,
                "progress_minutes": watch_minutes,
                "completed": watch_minutes >= int_value(task.get("target_minutes"), 0),
                "claimed": task["id"] in claimed_tasks,
            }
            for task in daily_tasks_for_device(device_id)
        ],
        "spin": {
            "available": last_spin_week != reward_week_key(),
            "week_key": reward_week_key(),
            "segments": SPIN_REWARDS,
            "selected_index": spin_index if last_spin_week == reward_week_key() else None,
            "last_reward": last_spin_reward if last_spin_week == reward_week_key() else None,
        },
        "rules": [
            "Balance starts at 0 coins.",
            "Daily check-in starts at +20 coins, increases through day 7, then resets to day 1.",
            "Daily watch tasks rotate every day and reward coins after the required watch time.",
            "Spin wheel can be used once per week and may land on Better luck next time.",
        ],
    }


@router.get(
    "/client/films/{film_id}/episodes/unlocked",
    tags=["Playback"],
    summary="Unlocked episodes for a film",
    dependencies=DEVICE_AUTH,
)
async def client_film_unlocked_episodes(film_id: FilmId, request: Request) -> Response:
    return await proxy_request(request, "app", "api/episode_unlocked", {"film_id": film_id})


@router.get(
    "/client/films/{film_id}/episodes/{episode_number}/unlock-state",
    tags=["Playback"],
    summary="Check unlock state for one episode",
    dependencies=DEVICE_AUTH,
)
async def client_episode_unlock_state(film_id: FilmId, episode_number: EpisodeRef, request: Request) -> JSONResponse:
    payload = await proxy_json(request, "app", "api/info_film", {"film_id": film_id})
    film = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    episode = find_episode(film_episodes(film), episode_number)
    if not episode:
        return JSONResponse(
            {"status": False, "message": "Episode not found", "film_id": film_id, "episode": episode_number},
            status_code=404,
        )

    is_paid = bool(episode.get("is_vip")) or int(episode.get("price") or 0) > 0
    is_unlocked = episode.get("is_unlocked", 1) == 1 or not is_paid
    return JSONResponse(
        {
            "status": True,
            "film_id": film_id,
            "episode": public_episode(episode),
            "is_unlocked": is_unlocked,
            "unlock_required": not is_unlocked,
        }
    )


@router.post(
    "/client/reminders/toggle",
    tags=["Reminders"],
    summary="Toggle reminder",
    dependencies=DEVICE_AUTH,
    include_in_schema=False,
)
async def client_toggle_reminder(request: Request) -> Response:
    return await proxy_request(request, "app", "api/toggle_reminder")


@router.get("/client/reminders", tags=["Reminders"], summary="User reminders", dependencies=DEVICE_AUTH)
async def client_reminders(request: Request) -> JSONResponse:
    return await reminders_response(request)


@router.get("/client/planner", tags=["Planner"], summary="Drama planner items", dependencies=DEVICE_AUTH)
async def client_planner(request: Request) -> JSONResponse:
    device_id = await extract_device_id(request)
    return JSONResponse({"status": True, "data": await state_store.list_planner_items(device_id)})


@router.post("/client/planner", tags=["Planner"], summary="Create or update a drama planner item", dependencies=DEVICE_AUTH)
async def client_save_planner_item(item: PlannerItemRequest, request: Request) -> JSONResponse:
    device_id = await extract_device_id(request)
    saved = await state_store.save_planner_item(
        device_id,
        {
            "film_id": item.film_id,
            "title": item.title,
            "episode": item.episode,
            "scheduled_at": item.scheduled_at,
            "note": item.note,
            "image_url": item.image_url,
            "remind_before_minutes": item.remind_before_minutes,
        },
    )
    # Planner items are mirrored into notifications so the home bell has immediate in-app state.
    await state_store.save_notification(
        device_id,
        {
            "title": "Drama planned",
            "body": f"{item.title} is scheduled for {item.scheduled_at}",
            "type": "planner",
            "metadata": {"planner_id": saved["id"], "film_id": item.film_id},
        },
    )
    return JSONResponse({"status": True, "data": saved})


@router.delete("/client/planner/{item_id}", tags=["Planner"], summary="Delete a drama planner item", dependencies=DEVICE_AUTH)
async def client_delete_planner_item(item_id: str, request: Request) -> JSONResponse:
    device_id = await extract_device_id(request)
    await state_store.delete_planner_item(device_id, item_id)
    return JSONResponse({"status": True, "data": {"id": item_id}})


@router.get("/client/notifications", tags=["Notifications"], summary="In-app notifications", dependencies=DEVICE_AUTH)
async def client_notifications(request: Request) -> JSONResponse:
    device_id = await extract_device_id(request)
    notifications = await state_store.list_notifications(device_id)
    return JSONResponse(
        {
            "status": True,
            "unread_count": sum(1 for item in notifications if not bool_state(item.get("read"))),
            "data": notifications,
        }
    )


@router.post("/client/notifications", tags=["Notifications"], summary="Track a notification", dependencies=DEVICE_AUTH)
async def client_save_notification(notification: NotificationRequest, request: Request) -> JSONResponse:
    device_id = await extract_device_id(request)
    saved = await state_store.save_notification(
        device_id,
        {
            "title": notification.title,
            "body": notification.body,
            "type": notification.type,
            "metadata": notification.metadata,
        },
    )
    return JSONResponse({"status": True, "data": saved})


@router.post("/client/notifications/{notification_id}/read", tags=["Notifications"], summary="Mark notification read", dependencies=DEVICE_AUTH)
async def client_read_notification(notification_id: str, request: Request) -> JSONResponse:
    device_id = await extract_device_id(request)
    await state_store.mark_notification_read(device_id, notification_id)
    return JSONResponse({"status": True, "data": {"id": notification_id, "read": True}})


@router.delete("/client/notifications", tags=["Notifications"], summary="Clear in-app notifications", dependencies=DEVICE_AUTH)
async def client_clear_notifications(request: Request) -> JSONResponse:
    device_id = await extract_device_id(request)
    await state_store.clear_notifications(device_id)
    return JSONResponse({"status": True, "unread_count": 0, "data": []})


@router.get("/client/rewards", tags=["Rewards"], summary="Reward wallet and missions", dependencies=DEVICE_AUTH)
async def client_rewards(request: Request) -> JSONResponse:
    device_id = await extract_device_id(request)
    rewards = await state_store.get_rewards(device_id)
    return JSONResponse({"status": True, "data": reward_payload(device_id, rewards)})


@router.post("/client/rewards/action", tags=["Rewards"], summary="Track reward action", dependencies=DEVICE_AUTH)
async def client_reward_action(action: RewardActionRequest, request: Request) -> JSONResponse:
    device_id = await extract_device_id(request)
    rewards = await state_store.get_rewards(device_id)
    actions = rewards.get("actions") if isinstance(rewards.get("actions"), list) else []
    today = reward_day_key()
    week = reward_week_key()
    earned = 0
    message = "Reward activity tracked."
    if action.action == "daily_check_in":
        if rewards.get("last_check_in") != today:
            current_day = min(max(int_value(rewards.get("check_in_day"), 1), 1), 7)
            earned = CHECK_IN_REWARDS[current_day - 1]
            rewards["last_check_in"] = today
            rewards["last_claimed_check_in_day"] = current_day
            rewards["check_in_day"] = 1 if current_day >= 7 else current_day + 1
            message = f"Daily check-in claimed: +{earned} coins."
        else:
            message = "Daily check-in already claimed today."
    elif action.action == "daily_task":
        task_id = str(action.metadata.get("task_id", ""))
        task = next((item for item in WATCH_TASKS if item["id"] == task_id), None)
        claimed_tasks = rewards.get("claimed_tasks") if rewards.get("claimed_task_day") == today else []
        if not isinstance(claimed_tasks, list):
            claimed_tasks = []
        watch_minutes = int_value(rewards.get("watch_minutes_today"), 0) if rewards.get("watch_minutes_day") == today else 0
        if task and task_id not in claimed_tasks and watch_minutes >= int_value(task["target_minutes"], 0):
            earned = int_value(task["reward"], 0)
            rewards["claimed_task_day"] = today
            rewards["claimed_tasks"] = claimed_tasks + [task_id]
            message = f"Daily task claimed: +{earned} coins."
        elif task_id in claimed_tasks:
            message = "Daily task already claimed."
        else:
            message = "Daily task is not complete yet."
    elif action.action == "weekly_spin":
        if rewards.get("last_spin_week") != week:
            seed = sum(ord(char) for char in f"{device_id}:{week}")
            earned = SPIN_REWARDS[seed % len(SPIN_REWARDS)]
            rewards["last_spin_week"] = week
            rewards["last_spin_reward"] = earned
            message = f"Weekly spin reward: +{earned} coins." if earned else "Better luck next time."
        else:
            message = "Weekly spin already used."
    elif action.action == "watch_minutes":
        minutes = int_value(action.metadata.get("minutes"), 0)
        rewards["watch_minutes_today"] = max(int_value(rewards.get("watch_minutes_today"), 0), minutes)
        message = "Watch progress updated."
    if earned > 0:
        rewards["coins"] = int_value(rewards.get("coins"), 0) + earned
    rewards["actions"] = actions + [{"action": action.action, "amount": earned, "metadata": action.metadata, "created_at": today}]
    await state_store.save_rewards(device_id, rewards)
    await state_store.save_notification(
        device_id,
        {
            "title": "Reward claimed",
            "body": f"+{earned} coins added to your balance." if earned else message,
            "type": "reward",
            "metadata": {"action": action.action},
        },
    )
    return JSONResponse({"status": True, "message": message, "data": reward_payload(device_id, rewards)})


@router.get("/client/payments/packages/coins", tags=["Payments"], summary="Coin packages", dependencies=DEVICE_AUTH)
async def client_coin_packages(request: Request) -> Response:
    return await proxy_request(request, "web", "payment/coin_packages")


@router.get(
    "/client/payments/packages/subscriptions",
    tags=["Payments"],
    summary="Subscription packages",
    dependencies=DEVICE_AUTH,
)
async def client_subscription_packages(request: Request) -> Response:
    return await proxy_request(request, "web", "payment/subscription_packages")


@router.get("/client/payments/history", tags=["Payments"], summary="Payment history", dependencies=DEVICE_AUTH)
async def client_payment_history(request: Request) -> Response:
    return await proxy_request(request, "app", "api/payment_history_v2")


@router.get("/client/payments/recent", tags=["Payments"], summary="Recent payments", dependencies=DEVICE_AUTH)
async def client_recent_payments(request: Request) -> Response:
    return await proxy_request(request, "app", "api/recent_payments")


@router.post(
    "/client/payments/subscribe",
    tags=["Payments"],
    summary="Create subscription payment",
    dependencies=DEVICE_AUTH,
)
async def client_payment_subscribe(request: Request) -> Response:
    return await proxy_request(request, "app", "api/payment_sub2")


@router.get("/client/events", tags=["Events"], summary="App events", dependencies=DEVICE_AUTH)
async def client_events(request: Request) -> Response:
    return await proxy_request(request, "app", "api/events")


@router.post("/client/events/action", tags=["Events"], summary="Send action event", dependencies=DEVICE_AUTH)
async def client_action_event(request: Request) -> Response:
    device_id = await extract_device_id(request)
    payload = await request_json_body_for_event(request)
    persist_in_background(state_store.save_event(device_id, "action", payload))
    return await proxy_request(request, "app", "api/action_events")


@router.post("/client/events/ref", tags=["Events"], summary="Send event ref", dependencies=DEVICE_AUTH)
async def client_event_ref(request: Request) -> Response:
    device_id = await extract_device_id(request)
    payload = await request_json_body_for_event(request)
    persist_in_background(state_store.save_event(device_id, "ref", payload))
    return await proxy_request(request, "app", "api/event_ref")


@router.get("/client/config/menus", tags=["Config"], summary="App menus", dependencies=DEVICE_AUTH)
async def client_menus(request: Request) -> Response:
    return await proxy_request(request, "app", "api/menus")


@router.get("/client/config/languages", tags=["Config"], summary="Film languages", dependencies=DEVICE_AUTH)
async def client_languages(request: Request) -> Response:
    return await proxy_request(request, "web", "api/film_languages")


@router.get(
    "/client/video-page",
    tags=["Video"],
    summary="Video page shell",
    dependencies=DEVICE_AUTH,
    include_in_schema=False,
)
async def client_video_page(request: Request) -> Response:
    return await proxy_request(request, "web", "video")


@router.api_route(
    "/web/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def web_proxy(path: str, request: Request) -> Response:
    return await proxy_request(request, "web", path)


@router.api_route(
    "/app/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def app_proxy(path: str, request: Request) -> Response:
    return await proxy_request(request, "app", path)


@router.api_route("/cdn/{path:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def cdn_proxy(path: str, request: Request) -> Response:
    return await proxy_request(request, "cdn", path)


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def compatibility_proxy(path: str, request: Request) -> Response:
    if path in {"", "favicon.ico"}:
        return JSONResponse({"status": "ok", "message": "DramaHub wrapper is running"})

    upstream = request.query_params.get("upstream") or request.query_params.get("source")
    if upstream not in {"web", "app"}:
        upstream = settings.default_upstream if settings.default_upstream in {"web", "app"} else "web"

    return await proxy_request(request, upstream, path)
