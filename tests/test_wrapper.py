from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.upstream import (
    build_upstream_url,
    create_wrapper_token,
    extract_device_id,
    fallback_path,
    prepare_headers,
    should_use_app_fallback,
)
from app.routes import episode_is_unlocked, find_episode


class DummyRequest:
    def __init__(self, query_params=None, headers=None, json_body=None):
        self.query_params = query_params or DummyQueryParams([])
        self.headers = headers or {}
        self._json_body = json_body or {}
        self.client = None

    async def json(self):
        return self._json_body


class DummyQueryParams:
    def __init__(self, items):
        self._items = items

    def multi_items(self):
        return list(self._items)

    def get(self, key, default=None):
        for item_key, value in self._items:
            if item_key == key:
                return value
        return default


class WrapperTests(unittest.TestCase):
    def test_build_upstream_url_keeps_repeated_query_and_removes_wrapper_keys(self):
        request = DummyRequest(
            DummyQueryParams(
                [
                    ("language", "hi"),
                    ("tag", "a"),
                    ("tag", "b"),
                    ("upstream", "app"),
                ]
            )
        )

        self.assertEqual(
            build_upstream_url("https://dramahub.me", "/api/list_films", request),
            "https://dramahub.me/api/list_films?language=hi&tag=a&tag=b",
        )

    def test_prepare_headers_removes_hop_headers_and_adds_browser_defaults(self):
        request = DummyRequest(
            headers={
                "host": "localhost:8000",
                "connection": "keep-alive",
                "accept-encoding": "gzip, deflate, br",
                "authorization": "Bearer incoming",
            }
        )

        headers = prepare_headers(request, "web")

        self.assertNotIn("host", headers)
        self.assertNotIn("connection", headers)
        self.assertEqual(headers["authorization"], "Bearer incoming")
        self.assertEqual(headers["accept"], "application/json, text/plain, */*")
        self.assertEqual(headers["accept-encoding"], "identity")
        self.assertEqual(headers["referer"], "https://dramahub.me/")

    def test_prepare_headers_replaces_wrapper_bearer_with_upstream_token(self):
        wrapper_token = create_wrapper_token("device-from-token")
        request = DummyRequest(headers={"authorization": f"Bearer {wrapper_token}"})

        headers = prepare_headers(request, "app", "upstream-token")

        self.assertEqual(headers["authorization"], "Bearer upstream-token")

    def test_web_home_feature_falls_back_to_working_app_route(self):
        request = DummyRequest()

        self.assertTrue(should_use_app_fallback("web", "api/home_feature"))
        self.assertEqual(fallback_path("api/home_feature"), "api/film_for_you")

    def test_extract_device_id_prefers_header(self):
        request = DummyRequest(
            query_params=DummyQueryParams([("device_id", "query-device")]),
            headers={"x-device-id": "header-device"},
            json_body={"device_id": "body-device"},
        )

        self.assertEqual(asyncio_run(extract_device_id(request)), "header-device")

    def test_extract_device_id_reads_body(self):
        request = DummyRequest(
            headers={"content-type": "application/json"},
            json_body={"device_id": "body-device"},
        )

        self.assertEqual(asyncio_run(extract_device_id(request)), "body-device")

    def test_extract_device_id_reads_wrapper_bearer_token(self):
        wrapper_token = create_wrapper_token("bearer-device")
        request = DummyRequest(headers={"authorization": f"Bearer {wrapper_token}"})

        self.assertEqual(asyncio_run(extract_device_id(request)), "bearer-device")

    def test_openapi_hides_generic_proxy_routes(self):
        paths = TestClient(app).get("/openapi.json").json()["paths"]

        self.assertIn("/client/auth/device", paths)
        self.assertIn("/client/home", paths)
        self.assertIn("/client/films/{film_id}", paths)
        self.assertIn("/client/films/{film_id}/episodes", paths)
        self.assertIn("/client/films/{film_id}/episodes/{episode_number}/play", paths)
        self.assertIn("/client/films/{film_id}/episodes/{episode_number}/watch", paths)
        self.assertIn("/client/films/{film_id}/episodes/{episode_number}/unlock", paths)
        self.assertIn("/client/films/{film_id}/follow", paths)
        self.assertIn("/client/films/{film_id}/unfollow", paths)
        self.assertIn("/client/films/{film_id}/like", paths)
        self.assertIn("/client/films/{film_id}/unlike", paths)
        self.assertIn("/client/films/{film_id}/episodes/{episode_number}/like", paths)
        self.assertIn("/client/films/{film_id}/episodes/{episode_number}/unlike", paths)
        self.assertIn("/client/films/{film_id}/reminder", paths)
        self.assertIn("/client/films/{film_id}/unreminder", paths)
        self.assertIn("/client/films/{film_id}/episodes/unlocked", paths)
        self.assertIn("/client/films/{film_id}/episodes/{episode_number}/unlock-state", paths)
        self.assertNotIn("/client/episodes/unlocked", paths)
        self.assertNotIn("/client/episodes/unlock", paths)
        self.assertIn("/client/tags/{tag_id}/films", paths)
        self.assertIn("/client/payments/packages/coins", paths)
        self.assertNotIn("post", paths["/client/history/watch"])
        self.assertIn("HTTPBearer", TestClient(app).get("/openapi.json").json()["components"]["securitySchemes"])
        self.assertNotIn("/client/me", {path for path, methods in paths.items() if "post" in methods})
        self.assertNotIn("/client/areas/{slug}/films", paths)
        self.assertNotIn("/client/video-page", paths)
        self.assertNotIn("/client/reminders/toggle", paths)
        self.assertNotIn("/api/info_film", paths)
        self.assertNotIn("/api/user/device_token", paths)
        self.assertNotIn("/web/{path}", paths)
        self.assertNotIn("/app/{path}", paths)
        self.assertNotIn("/{path}", paths)
        self.assertNotIn("/device/capture", paths)

    def test_client_me_requires_bearer_token(self):
        response = TestClient(app).get("/client/me")

        self.assertEqual(response.status_code, 401)

    def test_episode_one_is_always_unlocked_and_later_episodes_follow_upstream_flag(self):
        episodes = [
            {"episode": "1", "is_unlocked": 0},
            {"episode": "2", "is_unlocked": 0},
            {"episode": "3", "is_unlocked": 1},
        ]

        self.assertIs(find_episode(episodes, 1), episodes[0])
        self.assertTrue(episode_is_unlocked(episodes[0]))
        self.assertFalse(episode_is_unlocked(episodes[1]))
        self.assertTrue(episode_is_unlocked(episodes[2]))


def asyncio_run(awaitable):
    import asyncio

    return asyncio.run(awaitable)


if __name__ == "__main__":
    unittest.main()
