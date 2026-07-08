from __future__ import annotations

import asyncio
import base64
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.env import load_env_file


load_env_file()

JsonMap = dict[str, Any]
state_store_backend = "memory"
state_store_error: str | None = None


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def safe_doc_id(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


class StateStore:
    async def get_device_session(self, device_id: str) -> JsonMap | None:
        raise NotImplementedError

    async def save_device_session(self, device_id: str, session: JsonMap) -> None:
        raise NotImplementedError

    async def get_token_device(self, token: str) -> str | None:
        raise NotImplementedError

    async def save_token_device(self, token: str, device_id: str) -> None:
        raise NotImplementedError

    async def get_engagement(self, device_id: str) -> JsonMap:
        raise NotImplementedError

    async def save_engagement(self, device_id: str, state: JsonMap) -> None:
        raise NotImplementedError

    async def save_watch_progress(self, device_id: str, film_id: int, episode_ref: int, progress: JsonMap) -> None:
        raise NotImplementedError

    async def save_event(self, device_id: str, event_type: str, payload: JsonMap) -> None:
        raise NotImplementedError


def empty_engagement_state() -> JsonMap:
    return {
        "followed_films": [],
        "unfollowed_films": [],
        "liked_episodes": [],
        "unliked_episodes": [],
        "reminded_films": [],
        "unreminded_films": [],
    }


class MemoryStateStore(StateStore):
    def __init__(self) -> None:
        self._sessions: dict[str, JsonMap] = {}
        self._tokens: dict[str, str] = {}
        self._engagement: dict[str, JsonMap] = {}
        self._watch_progress: dict[str, JsonMap] = {}
        self._events: list[JsonMap] = []
        self._lock = asyncio.Lock()

    async def get_device_session(self, device_id: str) -> JsonMap | None:
        async with self._lock:
            session = self._sessions.get(device_id)
            return dict(session) if session else None

    async def save_device_session(self, device_id: str, session: JsonMap) -> None:
        async with self._lock:
            self._sessions[device_id] = {**session, "updated_at": utc_now_iso()}

    async def get_token_device(self, token: str) -> str | None:
        async with self._lock:
            return self._tokens.get(token)

    async def save_token_device(self, token: str, device_id: str) -> None:
        async with self._lock:
            self._tokens[token] = device_id

    async def get_engagement(self, device_id: str) -> JsonMap:
        async with self._lock:
            state = self._engagement.setdefault(device_id, empty_engagement_state())
            return {key: list(value) for key, value in state.items()}

    async def save_engagement(self, device_id: str, state: JsonMap) -> None:
        async with self._lock:
            self._engagement[device_id] = {**state, "updated_at": utc_now_iso()}

    async def save_watch_progress(self, device_id: str, film_id: int, episode_ref: int, progress: JsonMap) -> None:
        key = f"{device_id}:{film_id}:{episode_ref}"
        async with self._lock:
            self._watch_progress[key] = {
                **progress,
                "device_id": device_id,
                "film_id": film_id,
                "episode": episode_ref,
                "updated_at": utc_now_iso(),
            }

    async def save_event(self, device_id: str, event_type: str, payload: JsonMap) -> None:
        async with self._lock:
            self._events.append(
                {
                    "device_id": device_id,
                    "event_type": event_type,
                    "payload": payload,
                    "created_at": utc_now_iso(),
                }
            )


@dataclass
class FirestoreStateStore(StateStore):
    db: Any
    prefix: str = "dramaverse"

    def _collection(self, name: str) -> Any:
        return self.db.collection(f"{self.prefix}_{name}")

    async def _to_thread(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(func, *args, **kwargs)

    async def get_device_session(self, device_id: str) -> JsonMap | None:
        snapshot = await self._to_thread(self._collection("device_sessions").document(safe_doc_id(device_id)).get)
        data = snapshot.to_dict() if snapshot.exists else None
        return data if isinstance(data, dict) else None

    async def save_device_session(self, device_id: str, session: JsonMap) -> None:
        await self._to_thread(
            self._collection("device_sessions").document(safe_doc_id(device_id)).set,
            {**session, "updated_at": utc_now_iso()},
            merge=True,
        )

    async def get_token_device(self, token: str) -> str | None:
        snapshot = await self._to_thread(self._collection("wrapper_tokens").document(safe_doc_id(token)).get)
        data = snapshot.to_dict() if snapshot.exists else None
        device_id = data.get("device_id") if isinstance(data, dict) else None
        return device_id if isinstance(device_id, str) else None

    async def save_token_device(self, token: str, device_id: str) -> None:
        await self._to_thread(
            self._collection("wrapper_tokens").document(safe_doc_id(token)).set,
            {"device_id": device_id, "updated_at": utc_now_iso()},
            merge=True,
        )

    async def get_engagement(self, device_id: str) -> JsonMap:
        snapshot = await self._to_thread(self._collection("engagement").document(safe_doc_id(device_id)).get)
        data = snapshot.to_dict() if snapshot.exists else None
        return data if isinstance(data, dict) else empty_engagement_state()

    async def save_engagement(self, device_id: str, state: JsonMap) -> None:
        await self._to_thread(
            self._collection("engagement").document(safe_doc_id(device_id)).set,
            {**state, "updated_at": utc_now_iso()},
            merge=True,
        )

    async def save_watch_progress(self, device_id: str, film_id: int, episode_ref: int, progress: JsonMap) -> None:
        doc_id = safe_doc_id(f"{device_id}:{film_id}:{episode_ref}")
        await self._to_thread(
            self._collection("watch_progress").document(doc_id).set,
            {
                **progress,
                "device_id": device_id,
                "film_id": film_id,
                "episode": episode_ref,
                "updated_at": utc_now_iso(),
            },
            merge=True,
        )

    async def save_event(self, device_id: str, event_type: str, payload: JsonMap) -> None:
        await self._to_thread(
            self._collection("events").document().set,
            {
                "device_id": device_id,
                "event_type": event_type,
                "payload": payload,
                "created_at": utc_now_iso(),
            },
        )


def build_state_store() -> StateStore:
    global state_store_backend, state_store_error
    use_firestore = os.getenv("FIRESTORE_ENABLED", "").lower() in {"1", "true", "yes"} or bool(
        os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("FIREBASE_PROJECT_ID")
    )
    if not use_firestore:
        state_store_backend = "memory"
        state_store_error = None
        return MemoryStateStore()

    try:
        from google.cloud import firestore
    except ImportError:
        state_store_backend = "memory"
        state_store_error = "google-cloud-firestore is not installed"
        return MemoryStateStore()

    project = os.getenv("FIREBASE_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT") or None
    database = os.getenv("FIRESTORE_DATABASE")
    client_kwargs = {"project": project} if project else {}
    if database:
        client_kwargs["database"] = database
    try:
        client = firestore.Client(**client_kwargs)
    except Exception as exc:
        state_store_backend = "memory"
        state_store_error = f"{type(exc).__name__}: {exc}"
        return MemoryStateStore()
    state_store_backend = "firestore"
    state_store_error = None
    return FirestoreStateStore(client, os.getenv("FIRESTORE_COLLECTION_PREFIX", "dramaverse"))


state_store = build_state_store()
