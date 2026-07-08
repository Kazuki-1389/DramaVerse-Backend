from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from app.config import settings
from app.store import state_store


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "content-encoding",
    "accept-encoding",
}

WRAPPER_QUERY_KEYS = {"upstream", "source"}

WEB_TO_APP_FALLBACKS = {
    "api/home_feature": "api/film_for_you",
    "api/history_watching_film": "api/history_watching_film",
    "api/list_films": "api/list_films",
    "api/film_hot_search": "api/film_for_you",
    "api/info_film": "api/info_film",
    "api/user/get_info": "api/user/get_info",
}


_device_sessions: dict[str, dict[str, object]] = {}
_device_locks: dict[str, asyncio.Lock] = {}
_wrapper_token_devices: dict[str, str] = {}
_session_registry_lock = asyncio.Lock()


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=15.0, read=60.0),
        follow_redirects=True,
    )


def build_upstream_url(
    base_url: str,
    path: str,
    request: Request,
    extra_query: dict[str, object] | None = None,
) -> str:
    safe_path = path.lstrip("/")
    query_items = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key not in WRAPPER_QUERY_KEYS
    ]
    existing_keys = {key for key, _ in query_items}
    for key, value in (extra_query or {}).items():
        if value is not None and key not in existing_keys:
            query_items.append((key, str(value)))
    query = urlencode(query_items, doseq=True)
    url = f"{base_url}/{safe_path}" if safe_path else base_url
    return f"{url}?{query}" if query else url


def normalize_path(path: str) -> str:
    return path.lstrip("/")


def _string_value(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def bearer_token(headers: object) -> str | None:
    get_header = getattr(headers, "get", lambda _key: None)
    auth_header = _string_value(get_header("authorization")) or _string_value(get_header("Authorization"))
    if not auth_header:
        return None

    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _urlsafe_b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _urlsafe_unb64(value: str) -> str | None:
    try:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(f"{value}{padding}").decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def create_wrapper_token(device_id: str) -> str:
    nonce = secrets.token_urlsafe(24)
    device_part = _urlsafe_b64(device_id)
    signature = hmac.new(
        settings.wrapper_token_secret.encode("utf-8"),
        f"{device_id}:{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    token = f"dhw_{device_part}.{nonce}.{signature}"
    _wrapper_token_devices[token] = device_id
    return token


def _device_id_from_signed_wrapper_token(token: str) -> str | None:
    if not token.startswith("dhw_"):
        return None
    parts = token.removeprefix("dhw_").split(".")
    if len(parts) != 3:
        return None
    device_part, nonce, signature = parts
    device_id = _urlsafe_unb64(device_part)
    if not device_id:
        return None
    expected = hmac.new(
        settings.wrapper_token_secret.encode("utf-8"),
        f"{device_id}:{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return device_id if hmac.compare_digest(signature, expected) else None


async def device_id_for_wrapper_token(token: str | None) -> str | None:
    if not token:
        return None
    device_id = _device_id_from_signed_wrapper_token(token)
    if device_id:
        return device_id
    device_id = _wrapper_token_devices.get(token)
    if device_id:
        return device_id
    return await state_store.get_token_device(token)


async def request_wrapper_device_id(request: Request) -> str | None:
    return await device_id_for_wrapper_token(bearer_token(request.headers))


async def request_json_body(request: Request) -> dict[str, object]:
    content_type = request.headers.get("content-type", "")
    if "json" not in content_type.lower():
        return {}

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return {}

    return payload if isinstance(payload, dict) else {}


async def extract_device_id(request: Request) -> str:
    wrapper_device_id = await request_wrapper_device_id(request)
    if wrapper_device_id:
        return wrapper_device_id

    for header_name in ("x-device-id", "x-deviceid", "device-id", "device_id"):
        value = _string_value(request.headers.get(header_name))
        if value:
            return value

    for query_name in ("device_id", "deviceId", "device", "uuid", "install_id"):
        value = _string_value(request.query_params.get(query_name))
        if value:
            return value

    payload = await request_json_body(request)
    for body_name in ("device_id", "deviceId", "device", "uuid", "install_id"):
        value = _string_value(payload.get(body_name))
        if value:
            return value

    client_host = request.client.host if request.client else "unknown"
    return f"{settings.device_id}-{client_host}"


async def device_lock(device_id: str) -> asyncio.Lock:
    async with _session_registry_lock:
        lock = _device_locks.get(device_id)
        if lock is None:
            lock = asyncio.Lock()
            _device_locks[device_id] = lock
        return lock


async def login_guest_device(device_id: str, language: str | None = None) -> dict[str, object]:
    async with new_client() as auth_client:
        response = await auth_client.post(
            f"{settings.app_base}/auth/user/login",
            json={
                "provider": "guest",
                "device_id": device_id,
                "language": language or settings.default_language,
            },
            headers={
                "accept": "application/json, text/plain, */*",
                "accept-encoding": "identity",
                "content-type": "application/json",
                "user-agent": "okhttp/4.12.0",
            },
        )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Guest login did not return a bearer token")

    session: dict[str, object] = {
        "device_id": device_id,
        "token": token,
        "user": payload.get("data") if isinstance(payload.get("data"), dict) else {},
    }
    _device_sessions[device_id] = session
    await state_store.save_device_session(device_id, session)
    return session


async def get_device_session(
    device_id: str,
    force_refresh: bool = False,
    language: str | None = None,
) -> dict[str, object]:
    session = _device_sessions.get(device_id)
    if session and not force_refresh:
        return session

    if not force_refresh:
        stored_session = await state_store.get_device_session(device_id)
        if stored_session and isinstance(stored_session.get("token"), str):
            _device_sessions[device_id] = stored_session
            wrapper_token = stored_session.get("wrapper_token")
            if isinstance(wrapper_token, str):
                _wrapper_token_devices[wrapper_token] = device_id
            return stored_session

    lock = await device_lock(device_id)
    async with lock:
        session = _device_sessions.get(device_id)
        if session and not force_refresh:
            return session

        if not force_refresh:
            stored_session = await state_store.get_device_session(device_id)
            if stored_session and isinstance(stored_session.get("token"), str):
                _device_sessions[device_id] = stored_session
                wrapper_token = stored_session.get("wrapper_token")
                if isinstance(wrapper_token, str):
                    _wrapper_token_devices[wrapper_token] = device_id
                return stored_session

        return await login_guest_device(device_id, language)


async def capture_device_session(request: Request) -> dict[str, object]:
    device_id = await extract_device_id(request)
    language = request.query_params.get("language")
    payload = await request_json_body(request)
    if not language:
        language = _string_value(payload.get("language"))

    session = await get_device_session(device_id, language=language)
    wrapper_token = session.get("wrapper_token")
    if not isinstance(wrapper_token, str):
        wrapper_token = create_wrapper_token(device_id)
        session["wrapper_token"] = wrapper_token
        await state_store.save_device_session(device_id, session)
    await state_store.save_token_device(wrapper_token, device_id)

    return {
        "status": True,
        "message": "Device captured",
        "device_id": device_id,
        "token_type": "bearer",
        "token": wrapper_token,
        "user": session.get("user", {}),
    }


def should_use_app_fallback(upstream: str, path: str) -> bool:
    return upstream == "web" and normalize_path(path) in WEB_TO_APP_FALLBACKS and not settings.bearer_token


def fallback_path(path: str) -> str:
    return WEB_TO_APP_FALLBACKS.get(normalize_path(path), normalize_path(path))


def prepare_headers(request: Request, upstream: str, token: str | None = None) -> dict[str, str]:
    wrapper_token = bearer_token(request.headers)
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
        and not (key.lower() == "authorization" and wrapper_token and wrapper_token.startswith("dhw_"))
    }

    headers.setdefault("accept", "application/json, text/plain, */*")
    headers["accept-encoding"] = "identity"
    headers.setdefault("accept-language", "en-US,en;q=0.9")

    if upstream == "web":
        headers.setdefault("referer", f"{settings.web_base}/")
        headers.setdefault("origin", settings.web_base)
    elif upstream == "app":
        headers.setdefault("referer", f"{settings.app_base}/")
        headers.setdefault("origin", settings.app_base)
    elif upstream == "cdn":
        headers.setdefault("referer", f"{settings.web_base}/")

    lower_header_keys = {key.lower() for key in headers}
    if "authorization" not in lower_header_keys:
        if token:
            headers["authorization"] = f"Bearer {token}"
        elif settings.bearer_token:
            headers["authorization"] = f"Bearer {settings.bearer_token}"

    return headers


def response_headers(upstream_headers: httpx.Headers) -> dict[str, str]:
    return {
        key: value
        for key, value in upstream_headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


def is_invalid_auth_response(content: bytes, content_type: str) -> bool:
    if "json" not in content_type.lower():
        return False

    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False

    return payload.get("code") == "E_INVALID_AUTH_TOKEN"


async def send_upstream_request(
    request: Request,
    upstream: str,
    path: str,
    token: str | None = None,
    extra_query: dict[str, object] | None = None,
    method_override: str | None = None,
) -> tuple[httpx.AsyncClient, httpx.Response]:
    base_url = settings.base_for(upstream)
    url = build_upstream_url(base_url, path, request, extra_query)
    headers = prepare_headers(request, upstream, token)
    body = await request.body()
    method = method_override or request.method
    if extra_query and method.upper() in {"POST", "PUT", "PATCH"}:
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}

        if isinstance(payload, dict):
            for key, value in extra_query.items():
                if value is not None and key not in payload:
                    payload[key] = value
            body = json.dumps(payload).encode("utf-8")
            headers["content-type"] = "application/json"

    upstream_client = new_client()
    upstream_request = upstream_client.build_request(
        method,
        url,
        headers=headers,
        content=body if body else None,
    )
    return upstream_client, await upstream_client.send(upstream_request, stream=True)


async def close_stream(upstream_client: httpx.AsyncClient, upstream_response: httpx.Response) -> None:
    await upstream_response.aclose()
    await upstream_client.aclose()


async def proxy_request(
    request: Request,
    upstream: str,
    path: str,
    extra_query: dict[str, object] | None = None,
    method_override: str | None = None,
) -> Response:
    token: str | None = None
    device_id = await extract_device_id(request)

    if upstream == "app" or should_use_app_fallback(upstream, path):
        session = await get_device_session(device_id)
        token = session.get("token") if isinstance(session.get("token"), str) else None
        if should_use_app_fallback(upstream, path):
            upstream = "app"
            path = fallback_path(path)

    upstream_client, upstream_response = await send_upstream_request(
        request,
        upstream,
        path,
        token,
        extra_query,
        method_override,
    )

    content_type = upstream_response.headers.get("content-type", "")
    should_stream = (
        upstream == "cdn"
        or "video" in content_type
        or "mpegurl" in content_type
        or "octet-stream" in content_type
    )

    if should_stream:
        return StreamingResponse(
            upstream_response.aiter_bytes(),
            status_code=upstream_response.status_code,
            headers=response_headers(upstream_response.headers),
            background=BackgroundTask(close_stream, upstream_client, upstream_response),
            media_type=content_type or None,
        )

    content = await upstream_response.aread()
    await close_stream(upstream_client, upstream_response)

    if is_invalid_auth_response(content, content_type):
        retry_upstream = upstream
        retry_path = path
        retry_token = token

        if upstream == "app":
            retry_session = await get_device_session(device_id, force_refresh=True)
            retry_token = retry_session.get("token") if isinstance(retry_session.get("token"), str) else None
        elif upstream == "web" and normalize_path(path) in WEB_TO_APP_FALLBACKS:
            retry_upstream = "app"
            retry_path = fallback_path(path)
            retry_session = await get_device_session(device_id, force_refresh=True)
            retry_token = retry_session.get("token") if isinstance(retry_session.get("token"), str) else None
        else:
            retry_token = None

        if retry_token:
            retry_client, retry_response = await send_upstream_request(
                request,
                retry_upstream,
                retry_path,
                retry_token,
                extra_query,
                method_override,
            )
            retry_content_type = retry_response.headers.get("content-type", "")
            retry_content = await retry_response.aread()
            await close_stream(retry_client, retry_response)
            return Response(
                content=retry_content,
                status_code=retry_response.status_code,
                headers=response_headers(retry_response.headers),
                media_type=retry_content_type or None,
            )

    return Response(
        content=content,
        status_code=upstream_response.status_code,
        headers=response_headers(upstream_response.headers),
        media_type=content_type or None,
    )


async def close_client() -> None:
    return None
