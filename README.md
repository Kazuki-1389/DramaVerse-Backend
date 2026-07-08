# DramaHub FastAPI Wrapper

Small FastAPI wrapper for the DramaHub web API (`https://dramahub.me`) and the reference DramaTV app API (`https://api.dramatv.app`).

The wrapper exposes a clean Android-friendly `/client/...` API. It does not hardcode leaked upstream tokens. Register a device once, then send the wrapper token as `Authorization: Bearer ...` on protected endpoints.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.\.venv\Scripts\uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open API docs:

```text
http://localhost:8000/docs
http://localhost:8000/openapi.json
```

## Environment

```env
DRAMAHUB_WEB_BASE=https://dramahub.me
DRAMAHUB_APP_BASE=https://api.dramatv.app
DRAMAHUB_CDN_BASE=https://ccdn.dramahub.me
DRAMAHUB_BEARER_TOKEN=
DRAMAHUB_DEFAULT_UPSTREAM=web
DRAMAHUB_DEFAULT_LANGUAGE=hi
DRAMAHUB_DEVICE_ID=local-device-id
DRAMAHUB_WRAPPER_TOKEN_SECRET=change-me-in-production
DRAMAHUB_CORS_ORIGINS=*
FIRESTORE_ENABLED=false
FIREBASE_PROJECT_ID=
GOOGLE_APPLICATION_CREDENTIALS=
FIRESTORE_DATABASE=(default)
FIRESTORE_COLLECTION_PREFIX=dramaverse
```

## Firebase Firestore

The backend runs without Firebase locally and stores device sessions, wrapper tokens, engagement overrides, watch progress, and client events in memory. For production, enable Firestore so multiple API workers share the same state:

```env
FIRESTORE_ENABLED=true
FIREBASE_PROJECT_ID=your-firebase-project-id
GOOGLE_APPLICATION_CREDENTIALS=C:\secure\firebase-service-account.json
FIRESTORE_COLLECTION_PREFIX=dramaverse
```

The service account should have Cloud Firestore read/write access. The app writes to prefixed collections such as `dramaverse_device_sessions`, `dramaverse_wrapper_tokens`, `dramaverse_engagement`, `dramaverse_watch_progress`, and `dramaverse_events`.

Network calls remain async. Firestore's sync SDK is isolated with `asyncio.to_thread`, and progress/event writes are dispatched in background tasks so client screens do not wait on analytics or persistence writes.

## Authentication

Register or resume a device user first:

```http
POST /client/auth/device
Content-Type: application/json

{
  "device_id": "android-install-id-123",
  "language": "hi"
}
```

Example response:

```json
{
  "status": true,
  "message": "Device captured",
  "device_id": "android-install-id-123",
  "token_type": "bearer",
  "token": "dhw_hashed-wrapper-token",
  "user": {
    "id": 123
  }
}
```

Send that token on all authenticated `/client/...` requests:

```http
Authorization: Bearer dhw_hashed-wrapper-token
```

The returned token is a signed wrapper token mapped to the original device session. It is used only by this backend; when the request is proxied upstream, the backend replaces it with the correct upstream guest bearer token.

`X-Device-Id` is still accepted as a legacy fallback, and device ids are also accepted from `device_id`, `deviceId`, `device`, `uuid`, or `install_id` in the query/body for helper and proxy routes.

## Endpoint Summary

| Method | Endpoint | Description |
| --- | --- | --- |
| POST | `/client/auth/device` | Register or resume a device guest user. |
| GET | `/client/me` | Get current device user. |
| POST | `/client/me` | Update current device user. Hidden from Swagger. |
| POST | `/client/feedback` | Send user feedback. |
| GET | `/client/home` | Home feed. |
| GET | `/client/for-you` | Personalized feed. |
| GET | `/client/search?query=love` | Search films. |
| GET | `/client/search/hot` | Hot search films. |
| GET | `/client/tags` | List tags. |
| GET | `/client/tags/{tag_id}/films` | Films for a selected tag. |
| GET | `/client/areas` | Film areas. Hidden from Swagger. |
| GET | `/client/areas/{slug}/films` | Films for an area. Hidden from Swagger. |
| GET | `/client/films` | List films. |
| GET | `/client/films/{film_id}` | Film details. |
| GET | `/client/films/{film_id}/similar` | More like this. |
| GET | `/client/films/{film_id}/episodes` | List episodes. |
| GET | `/client/films/{film_id}/episodes/{episode_number}/play` | HLS playback info. |
| POST | `/client/films/{film_id}/episodes/{episode_number}/watch` | Save watch progress. |
| GET | `/client/films/{film_id}/episodes/{episode_number}/unlock-state` | Check episode unlock state. |
| GET | `/client/films/{film_id}/episodes/unlocked` | List unlocked paid episodes. |
| POST | `/client/films/{film_id}/episodes/{episode_number}/unlock` | Unlock a locked episode. |
| POST | `/client/films/{film_id}/follow` | Follow a film. |
| POST | `/client/films/{film_id}/unfollow` | Unfollow a film. |
| POST | `/client/films/{film_id}/like` | Like first/current episode. |
| POST | `/client/films/{film_id}/unlike` | Unlike first/current episode. |
| POST | `/client/films/{film_id}/episodes/{episode_number}/like` | Like one episode. |
| POST | `/client/films/{film_id}/episodes/{episode_number}/unlike` | Unlike one episode. |
| GET | `/client/library/following` | Followed films. |
| GET | `/client/history/watch` | Watch history. |
| GET | `/client/history/follow` | Follow history. |
| POST | `/client/films/{film_id}/reminder` | Set a film reminder. |
| POST | `/client/films/{film_id}/unreminder` | Remove a film reminder. |
| GET | `/client/reminders` | User reminders. |
| POST | `/client/reminders/toggle` | Raw reminder toggle proxy. Hidden from Swagger. |
| GET | `/client/payments/packages/coins` | Coin packages. |
| GET | `/client/payments/packages/subscriptions` | Subscription packages. |
| GET | `/client/payments/history` | Payment history. |
| GET | `/client/payments/recent` | Recent payments. |
| POST | `/client/payments/subscribe` | Create subscription payment. |
| GET | `/client/events` | App events. |
| POST | `/client/events/action` | Send action event. |
| POST | `/client/events/ref` | Send event ref. |
| GET | `/client/config/menus` | App menus. |
| GET | `/client/config/languages` | Film languages. |
| GET | `/client/video-page` | Video page shell. Hidden from Swagger. |

## Auth Endpoints

### `POST /client/auth/device`

Creates or resumes a per-device guest session.

```bash
curl -X POST "http://localhost:8000/client/auth/device" \
  -H "Content-Type: application/json" \
  -d "{\"device_id\":\"android-install-id-123\",\"language\":\"hi\"}"
```

## User Endpoints

### `GET /client/me`

```bash
curl "http://localhost:8000/client/me" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `POST /client/me`

Updates the current app user by proxying to `/api/user/update_info`.

```bash
curl -X POST "http://localhost:8000/client/me" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"Demo User\",\"language\":\"hi\"}"
```

### `POST /client/feedback`

```bash
curl -X POST "http://localhost:8000/client/feedback" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token" \
  -H "Content-Type: application/json" \
  -d "{\"content\":\"Playback is smooth\",\"rating\":5}"
```

## Discovery Endpoints

### `GET /client/home`

```bash
curl "http://localhost:8000/client/home?language=hi" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/for-you`

```bash
curl "http://localhost:8000/client/for-you?language=hi&page=1" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/search`

Required query parameter: `query`.

```bash
curl "http://localhost:8000/client/search?query=love&language=hi&page=1" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/search/hot`

```bash
curl "http://localhost:8000/client/search/hot?language=hi" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/tags`

```bash
curl "http://localhost:8000/client/tags" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/tags/{tag_id}/films`

```bash
curl "http://localhost:8000/client/tags/1/films?language=hi&page=1" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/areas`

Hidden from Swagger, but available.

```bash
curl "http://localhost:8000/client/areas" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/areas/{slug}/films`

Hidden from Swagger, but available.

```bash
curl "http://localhost:8000/client/areas/india/films?language=hi" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

## Film Endpoints

### `GET /client/films`

```bash
curl "http://localhost:8000/client/films?language=hi&page=1" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/films/{film_id}`

```bash
curl "http://localhost:8000/client/films/167?language=hi" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/films/{film_id}/similar`

```bash
curl "http://localhost:8000/client/films/167/similar?language=hi" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

## Playback Endpoints

### `GET /client/films/{film_id}/episodes`

```bash
curl "http://localhost:8000/client/films/167/episodes?language=hi" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

Example response shape:

```json
{
  "status": true,
  "film_id": 167,
  "episode_total": 20,
  "episodes": [
    {
      "episode_id": 8755,
      "episode": 1,
      "title": "Episode 1",
      "is_vip": 0,
      "price": 0,
      "is_unlocked": 1,
      "is_publish": 1,
      "is_like": 0
    }
  ]
}
```

### `GET /client/films/{film_id}/episodes/{episode_number}/play`

`episode_number` can be the visible episode number, such as `1`, or an upstream episode id.

```bash
curl "http://localhost:8000/client/films/167/episodes/1/play?language=hi" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

Example response shape:

```json
{
  "status": true,
  "film": {
    "id": 167,
    "title": "Example Drama",
    "thumb": "https://ccdn.dramahub.me/thumbs/example.webp",
    "episode_total": 20
  },
  "episode": {
    "episode_id": 8755,
    "episode": 1,
    "title": "Episode 1",
    "playback": {
      "hls_url": "https://ccdn.dramahub.me/films/example/index.m3u8",
      "backup_hls_url": null,
      "subtitles": {}
    }
  },
  "next_episode": {
    "episode_id": 8756,
    "episode": 2,
    "title": "Episode 2"
  },
  "unlock_required": false
}
```

### `POST /client/films/{film_id}/episodes/{episode_number}/watch`

```bash
curl -X POST "http://localhost:8000/client/films/167/episodes/1/watch" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token" \
  -H "Content-Type: application/json" \
  -d "{\"progress_seconds\":60,\"duration_seconds\":180,\"completed\":false}"
```

### `GET /client/films/{film_id}/episodes/{episode_number}/unlock-state`

```bash
curl "http://localhost:8000/client/films/167/episodes/2/unlock-state?language=hi" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/films/{film_id}/episodes/unlocked`

```bash
curl "http://localhost:8000/client/films/167/episodes/unlocked?language=hi" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `POST /client/films/{film_id}/episodes/{episode_number}/unlock`

Free episodes do not need an upstream unlock. Calling unlock for a free/playable episode returns `status: true` and `message: "Episode is already playable."`.

```bash
curl -X POST "http://localhost:8000/client/films/167/episodes/2/unlock" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

## Playback Flow

```text
1. GET /client/films/{film_id}/episodes
   Read episode ids, episode numbers, lock state, and publish state.

2. GET /client/films/{film_id}/episodes/{episode_number}/play
   Read playback.hls_url, backup_hls_url, subtitles, unlock_required, and next_episode.

3. Play playback.hls_url with ExoPlayer/HLS.

4. POST /client/films/{film_id}/episodes/{episode_number}/watch
   Save continue-watching state.

5. If unlock_required is true, call:
   POST /client/films/{film_id}/episodes/{episode_number}/unlock
```

## Engagement Endpoints

### Follow and unfollow

```bash
curl -X POST "http://localhost:8000/client/films/167/follow" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"

curl -X POST "http://localhost:8000/client/films/167/unfollow" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### Like and unlike the first/current episode

```bash
curl -X POST "http://localhost:8000/client/films/167/like" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"

curl -X POST "http://localhost:8000/client/films/167/unlike" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### Like and unlike a specific episode

```bash
curl -X POST "http://localhost:8000/client/films/167/episodes/1/like" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"

curl -X POST "http://localhost:8000/client/films/167/episodes/1/unlike" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

## Library Endpoints

### `GET /client/library/following`

```bash
curl "http://localhost:8000/client/library/following" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/history/watch`

```bash
curl "http://localhost:8000/client/history/watch" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/history/follow`

```bash
curl "http://localhost:8000/client/history/follow" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

## Reminder Endpoints

### Set and remove a film reminder

```bash
curl -X POST "http://localhost:8000/client/films/167/reminder" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"

curl -X POST "http://localhost:8000/client/films/167/unreminder" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/reminders`

```bash
curl "http://localhost:8000/client/reminders" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `POST /client/reminders/toggle`

Hidden from Swagger, but available as a raw upstream proxy.

```bash
curl -X POST "http://localhost:8000/client/reminders/toggle" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token" \
  -H "Content-Type: application/json" \
  -d "{\"film_id\":167}"
```

## Payment Endpoints

### `GET /client/payments/packages/coins`

```bash
curl "http://localhost:8000/client/payments/packages/coins" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/payments/packages/subscriptions`

```bash
curl "http://localhost:8000/client/payments/packages/subscriptions" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/payments/history`

```bash
curl "http://localhost:8000/client/payments/history" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/payments/recent`

```bash
curl "http://localhost:8000/client/payments/recent" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `POST /client/payments/subscribe`

Proxies the request body to `/api/payment_sub2`.

```bash
curl -X POST "http://localhost:8000/client/payments/subscribe" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token" \
  -H "Content-Type: application/json" \
  -d "{\"package_id\":1,\"payment_method\":\"google_play\"}"
```

## Events Endpoints

### `GET /client/events`

```bash
curl "http://localhost:8000/client/events" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `POST /client/events/action`

```bash
curl -X POST "http://localhost:8000/client/events/action" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token" \
  -H "Content-Type: application/json" \
  -d "{\"event\":\"film_click\",\"film_id\":167}"
```

### `POST /client/events/ref`

```bash
curl -X POST "http://localhost:8000/client/events/ref" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token" \
  -H "Content-Type: application/json" \
  -d "{\"ref\":\"home\",\"film_id\":167}"
```

## Config Endpoints

### `GET /client/config/menus`

```bash
curl "http://localhost:8000/client/config/menus" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/config/languages`

```bash
curl "http://localhost:8000/client/config/languages" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

### `GET /client/video-page`

Hidden from Swagger, but available.

```bash
curl "http://localhost:8000/client/video-page" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

## CDN Passthrough

The playback endpoint returns direct `ccdn.dramahub.me` HLS and subtitle URLs. The Android app can usually play those URLs directly. If proxying is needed, use `/cdn/{path}`.

```bash
curl "http://localhost:8000/cdn/films/79600814d7ade1a06fd9ed3e7cc9378a/index.m3u8" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"

curl "http://localhost:8000/cdn/subtitles/e302c8eb7003417a0028255f1a8d0bd9.vtt" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"

curl "http://localhost:8000/cdn/thumbs/1a88f900e7a2bc2af846cc33fb7f643f.webp" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

## Hidden Helper Endpoints

These routes are available but hidden from Swagger:

| Method | Endpoint | Description |
| --- | --- | --- |
| GET | `/health` | Runtime health and upstream configuration. |
| GET | `/endpoints` | Full wrapper route map and upstream mappings. |
| POST | `/device/capture` | Capture/create a device guest session. |
| POST | `/auth/device` | Alias for device capture. |
| POST | `/api/user/device_token` | Alias for device capture. |
| POST | `/auth/user/login` | Alias for device guest login/capture. |

Examples:

```bash
curl "http://localhost:8000/health"

curl "http://localhost:8000/endpoints"

curl -X POST "http://localhost:8000/device/capture" \
  -H "Content-Type: application/json" \
  -d "{\"device_id\":\"android-install-id-123\",\"language\":\"hi\"}"
```

## Raw Upstream Proxy Routes

Use these only when the clean `/client/...` API does not cover a needed upstream route.

| Route | Description |
| --- | --- |
| `/web/{path}` | Proxy any request to `DRAMAHUB_WEB_BASE`. |
| `/app/{path}` | Proxy any request to `DRAMAHUB_APP_BASE`. |
| `/cdn/{path}` | Proxy CDN assets and streams to `DRAMAHUB_CDN_BASE`. |
| `/{path}` | Compatibility proxy. Uses `DRAMAHUB_DEFAULT_UPSTREAM`, or override with `?upstream=web` / `?upstream=app`. |

Examples:

```bash
curl "http://localhost:8000/app/api/list_films?language=hi&page=1" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"

curl "http://localhost:8000/web/api/film_languages" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"

curl "http://localhost:8000/api/list_films?language=hi&upstream=app" \
  -H "Authorization: Bearer dhw_hashed-wrapper-token"
```

Known compatibility routes include:

```text
POST /api/user/update_info
POST /api/user/user_feedback
POST /api/follow_film
POST /api/like_film
POST /api/watching_film
POST /api/unlock_episode
POST /api/toggle_reminder
POST /api/payment_sub2
POST /api/action_events
POST /api/event_ref
POST /api/film_click_search
POST /api/user/get_info

GET  /api/user/get_info
GET  /api/history_follow_film
GET  /api/get_tags
GET  /api/get_film_by_tags
GET  /api/list_films
GET  /api/film_for_you
GET  /api/info_film
GET  /api/more_like_this
GET  /api/film_list_area
GET  /api/follow_list_film
GET  /api/history_watching_film
GET  /api/episode_unlocked
GET  /api/user_reminders
GET  /api/payment_history_v2
GET  /api/recent_payments
GET  /api/events
GET  /api/menus
GET  /api/home_feature
GET  /api/film_hot_search
GET  /api/film_languages
GET  /payment/subscription_packages
GET  /payment/coin_packages
GET  /video
```

When no web bearer token is configured, web routes such as `/api/home_feature`, `/api/list_films`, `/api/info_film`, `/api/user/get_info`, and `/api/film_hot_search` can fall back to the app API equivalents because app guest tokens are accepted by `api.dramatv.app`.

## Tests

```powershell
.\.venv\Scripts\python -m unittest discover -s tests
```
