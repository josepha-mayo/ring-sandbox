"""FastAPI application that emulates ``https://api.amazonvision.com``.

Data-plane routes mirror the documented Ring Partner API. Control-plane routes live under
``/_sandbox`` and let tests/scenarios inject events, register webhook targets, and reset.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__, webhooks
from .models import WebhookEventType
from .world import (
    Chaos,
    DeviceKind,
    SandboxDevice,
    WebhookTarget,
    World,
    default_world,
    now_ms,
)

log = logging.getLogger("ring_sandbox")


def _error(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "errors": [
                {
                    "status": str(status),
                    "code": code,
                    "title": code.replace("_", " ").title(),
                    "detail": detail,
                }
            ]
        },
    )


class NotFound(Exception):
    pass


class _Unauthorized(Exception):
    def __init__(self, detail: str):
        self.detail = detail


# Request bodies live at module scope so FastAPI can resolve the (postponed) annotations.


class ImageRequest(BaseModel):
    type: str
    timestamp: int | None = None
    start_timestamp: int | None = None
    end_timestamp: int | None = None
    image_options: dict[str, Any] = Field(default_factory=dict)
    components: list[dict[str, str]] | None = None


class VideoRequest(BaseModel):
    timestamp: int
    duration: int
    video_options: dict[str, Any] | None = None
    audio_options: dict[str, Any] | None = None
    components: list[dict[str, str]] | None = None


class PlaybackRequest(BaseModel):
    event: str


class InjectEvent(BaseModel):
    device_id: str | None = None  # defaults to first camera
    type: str = "motion_detected"
    sub_type: str | None = None
    at: datetime | None = None
    duration_ms: int = 20_000
    deliver: bool = True


class WebhookTargetIn(BaseModel):
    url: str
    signing_key: str


class DeviceIn(BaseModel):
    kind: DeviceKind
    name: str
    id: str | None = None
    battery: int | None = None
    components: list[str] = Field(default_factory=list)


class AppIntegrationPatch(BaseModel):
    status: str


class SubscriptionIn(BaseModel):
    device_id: str
    plan_id: str = "sandbox.protect.basic"
    state: str = "active"  # active | trialing
    days: int = 30


def create_app(world: World | None = None, chaos: Chaos | None = None) -> FastAPI:
    world = world or default_world()
    chaos = dataclasses.replace(chaos) if chaos else None
    app = FastAPI(title="ring-sandbox", version=__version__, docs_url="/_sandbox/docs")
    app.state.world = world
    app.state.chaos = chaos
    # The deliveries still in flight. Both call sites fired them with
    # `asyncio.create_task` and kept no handle, so a consumer running a batch
    # had no way to know when the queue had drained: with the `delivery` preset
    # each delivery is held up to `delay_ms + jitter_ms` (1.8 s), and the only
    # recourse was to sleep for a guessed number of seconds. On a loaded
    # machine that guess is short and the run reports fewer deliveries than
    # were made — a measurement that moves with the load of the machine rather
    # than with the code under test.
    app.state.in_flight = set()
    rng = chaos.rng() if chaos else None

    def _flaky(rate: float) -> JSONResponse | None:
        if rng and rate and rng.random() < rate:
            return _error(500, "chaos", "chaos-injected transient failure — retry")
        return None

    @app.middleware("http")
    async def _chaos_throttle(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Chaos rate limiting on the data plane only (``/v1/*``): short-circuits
        with 429/503 + ``Retry-After`` so clients exercise backoff paths. The
        control plane stays deterministic."""
        if rng and chaos and request.url.path.startswith("/v1/"):
            if chaos.rate_limit and rng.random() < chaos.rate_limit:
                world.delivered.append(
                    {"kind": "chaos.rate_limited", "path": request.url.path, "status": 429}
                )
                resp = _error(429, "rate_limited", "rate limit exceeded — retry after delay")
                resp.headers["Retry-After"] = "1"
                return resp
            if chaos.unavailable and rng.random() < chaos.unavailable:
                world.delivered.append(
                    {"kind": "chaos.unavailable", "path": request.url.path, "status": 503}
                )
                resp = _error(503, "service_unavailable", "service unavailable — retry after delay")
                resp.headers["Retry-After"] = "2"
                return resp
        return await call_next(request)

    # ------------------------------------------------------------------ auth

    async def auth(authorization: str | None = Header(default=None)) -> None:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise _Unauthorized("missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        if not token or (world.required_token and token != world.required_token):
            raise _Unauthorized("invalid or expired token")

    @app.exception_handler(_Unauthorized)
    async def _unauth(_: Request, exc: _Unauthorized) -> JSONResponse:
        return _error(401, "unauthorized", exc.detail)

    @app.exception_handler(NotFound)
    async def _nf(_: Request, exc: NotFound) -> JSONResponse:
        return _error(404, "not_found", str(exc) or "device not found")

    def device_or_404(device_id: str) -> SandboxDevice:
        dev = world.get(device_id)
        if dev is None:
            raise NotFound(f"device {device_id} not found or not accessible")
        return dev

    def meta() -> dict[str, str]:
        return {"time": datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")}

    # ------------------------------------------------------------------ account

    @app.post("/oauth/token")
    async def token(
        grant_type: str = Form(...),
        refresh_token: str | None = Form(default=None),
        client_id: str | None = Form(default=None),
    ) -> dict[str, Any]:
        """RFC 6749 refresh grant. Sandbox simplification: any well-formed refresh
        token is accepted and the world rotates to the newly issued access token."""
        del client_id  # accepted but unused in the sandbox
        if grant_type != "refresh_token":
            return _error(400, "unsupported_grant_type", "only refresh_token is supported")
        if not refresh_token:
            return _error(400, "invalid_request", "refresh_token is required")
        if refresh_token == "invalid":
            return _error(400, "invalid_grant", "refresh token rejected")
        world.required_token = f"sandbox-{secrets.token_hex(8)}"
        return {
            "access_token": world.required_token,
            "refresh_token": f"sandbox-refresh-{secrets.token_hex(8)}",
            "token_type": "Bearer",
            "expires_in": 14400,
        }

    @app.get("/v1/users/me", dependencies=[Depends(auth)])
    async def me() -> dict[str, Any]:
        return {
            "meta": meta(),
            "data": {"type": "users", "id": world.account_id, "attributes": world.user},
        }

    def _fire(payload: dict[str, Any]) -> None:
        if world.webhooks:
            _track(asyncio.create_task(deliver(payload)))

    def _track(task: asyncio.Task[None]) -> None:
        """Keep a handle so `pending` can be answered, and drop it on the way
        out. Without the reference the task can also be garbage-collected
        mid-flight, which is the other reason to hold it."""
        app.state.in_flight.add(task)
        task.add_done_callback(app.state.in_flight.discard)

    # --------------------------------------------------------- app integrations

    def _app_integration_resource() -> dict[str, Any] | None:
        if world.app_integration_status is None:
            return None
        return {
            "type": "app-integrations",
            "id": world.account_id,
            "attributes": {"status": world.app_integration_status},
        }

    @app.post("/v1/accounts/me/app-integrations", dependencies=[Depends(auth)])
    async def app_integration_link() -> dict[str, Any]:
        """Partner confirms the account link (nonce verification) -> status
        ``awaiting``. Fires ``app_integration_added`` plus ``device_added`` for
        every consented device; those are account-linking events that fire
        regardless of subscription state. Re-confirming is idempotent."""
        if world.app_integration_status is None:
            world.app_integration_status = "awaiting"
            _fire(
                webhooks.build_event(
                    event_type=WebhookEventType.APP_INTEGRATION_ADDED,
                    source_type="accounts",
                    account_id=world.account_id,
                )
            )
            for d in world.devices.values():
                _fire(
                    webhooks.build_event(
                        event_type=WebhookEventType.DEVICE_ADDED,
                        device_id=d.id,
                        account_id=world.account_id,
                    )
                )
        return {"meta": meta(), "data": _app_integration_resource()}

    @app.get("/v1/accounts/me/app-integrations", dependencies=[Depends(auth)])
    async def app_integration_get() -> dict[str, Any]:
        return {"meta": meta(), "data": _app_integration_resource()}

    @app.patch("/v1/accounts/me/app-integrations", dependencies=[Depends(auth)])
    async def app_integration_update(body: AppIntegrationPatch) -> dict[str, Any]:
        """Advance the integration status. The real flow only moves forward:
        ``awaiting`` -> ``completed`` (finalizes the link in the Ring app UI)."""
        if world.app_integration_status is None:
            return _error(400, "bad_request", "no app integration to update")
        if body.status == world.app_integration_status:
            return {"meta": meta(), "data": _app_integration_resource()}
        if body.status != "completed":
            return _error(
                400,
                "bad_request",
                "status can only move forward to 'completed' "
                f"(have {world.app_integration_status!r})",
            )
        world.app_integration_status = "completed"
        return {"meta": meta(), "data": _app_integration_resource()}

    # ------------------------------------------------------------- subscriptions

    def _subscription_resource(sub: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "subscriptions",
            "id": sub["id"],
            "attributes": {
                "plan_id": sub["plan_id"],
                "state": sub["state"],
                "expires_at": sub["expires_at"],
                "created_at": sub["created_at"],
            },
            "relationships": {"devices": {"data": {"type": "devices", "id": sub["device_id"]}}},
        }

    @app.get("/v1/accounts/me/subscriptions", dependencies=[Depends(auth)])
    async def subscriptions() -> dict[str, Any]:
        """Subscriptions/trials the partner app holds for this user — per-device;
        empty when no trial or plan is active (which gates event delivery)."""
        return {
            "meta": meta(),
            "data": [_subscription_resource(s) for s in world.subscriptions.values()],
        }

    # ------------------------------------------------------------------ devices

    @app.get("/v1/devices", dependencies=[Depends(auth)])
    async def devices(include: str | None = Query(default=None)) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "meta": meta(),
            "data": [d.to_resource() for d in world.devices.values()],
        }
        if include:
            wanted = {s.strip() for s in include.split(",") if s.strip()}
            bad = wanted - {"status", "capabilities", "configurations", "location"}
            if bad:
                return _error(400, "bad_request", f"unknown include: {sorted(bad)}")  # type: ignore[return-value]
            inc: list[dict[str, Any]] = []
            for d in world.devices.values():
                if "status" in wanted:
                    inc.append(d.status_resource())
                if "capabilities" in wanted:
                    inc.append(d.capabilities_resource())
                if "configurations" in wanted:
                    inc.append(d.configurations_resource())
                if "location" in wanted:
                    inc.append(d.location_resource())
            doc["included"] = inc
        return doc

    @app.get("/v1/devices/{device_id}", dependencies=[Depends(auth)])
    async def device(device_id: str) -> dict[str, Any]:
        return {"meta": meta(), "data": device_or_404(device_id).to_resource()}

    @app.get("/v1/devices/{device_id}/status", dependencies=[Depends(auth)])
    async def status(device_id: str) -> dict[str, Any]:
        return {"meta": meta(), "data": device_or_404(device_id).status_resource()}

    @app.get("/v1/devices/{device_id}/capabilities", dependencies=[Depends(auth)])
    async def capabilities(device_id: str, component_id: str | None = None) -> dict[str, Any]:
        return {
            "meta": meta(),
            "data": device_or_404(device_id).capabilities_resource(component_id),
        }

    @app.get("/v1/devices/{device_id}/configurations", dependencies=[Depends(auth)])
    async def configurations(device_id: str) -> dict[str, Any]:
        return {"meta": meta(), "data": device_or_404(device_id).configurations_resource()}

    @app.get("/v1/devices/{device_id}/location", dependencies=[Depends(auth)])
    async def location(device_id: str) -> dict[str, Any]:
        return {"meta": meta(), "data": device_or_404(device_id).location_resource()}

    # ------------------------------------------------------------------ history

    PAGE_SIZE = 50

    @app.get("/v1/history/devices/{device_id}/events", dependencies=[Depends(auth)])
    async def history(
        request: Request, device_id: str, event_types: str | None = None
    ) -> dict[str, Any]:
        if chaos and (fail := _flaky(chaos.flaky_history)):
            return fail  # type: ignore[return-value]
        dev = device_or_404(device_id)
        page_key = request.query_params.get("page[key]")
        filters = [f.strip() for f in event_types.split(",")] if event_types else []
        recs = [r for r in dev.history if r.matches(filters)]
        if page_key:
            cutoff = int(datetime.fromisoformat(page_key.replace("Z", "+00:00")).timestamp() * 1000)
            recs = [r for r in recs if r.start < cutoff]
        page, rest = recs[:PAGE_SIZE], recs[PAGE_SIZE:]
        doc: dict[str, Any] = {"data": [r.to_jsonapi(device_id) for r in page], "links": {}}
        if rest:
            nxt_ts = (
                datetime.fromtimestamp(page[-1].start / 1000, tz=UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
            q = f"page[key]={nxt_ts}" + (f"&event_types={event_types}" if event_types else "")
            doc["links"]["next"] = f"/v1/history/devices/{device_id}/events?{q}"
        return doc

    # ------------------------------------------------------------------ media

    @app.post("/v1/devices/{device_id}/media/image/download", dependencies=[Depends(auth)])
    async def image_download(device_id: str, body: ImageRequest) -> Response:
        if chaos and (fail := _flaky(chaos.flaky_media)):
            return fail
        dev = device_or_404(device_id)
        if not dev.is_camera:
            return _error(400, "bad_request", "device has no camera")
        if body.components and len(body.components) != 1:
            return _error(400, "bad_request", "components takes exactly one entry")
        if body.type == "at_timestamp":
            if body.timestamp is None:
                return _error(400, "bad_request", "timestamp required for at_timestamp")
            ts = body.timestamp
        elif body.type == "latest_in_range":
            if body.start_timestamp is None:
                return _error(400, "bad_request", "start_timestamp required for latest_in_range")
            end = body.end_timestamp or now_ms()
            if end - body.start_timestamp > 24 * 3600 * 1000:
                return _error(400, "bad_request", "window must be <= 24 hours")
            covering = [r for r in dev.history if body.start_timestamp <= r.start <= end]
            ts = covering[0].start if covering else end
        else:
            return _error(400, "bad_request", "type must be at_timestamp or latest_in_range")
        if ts > now_ms() + 1000:
            return _error(400, "bad_request", "timestamp must be <= now")
        world.record_on_demand(device_id, ts)
        fmt = body.image_options.get("format", "jpeg")
        world.record_on_demand(device_id, ts)  # real API logs an on_demand history entry
        # Real API 303-redirects to a pre-signed URL; emulate that so clients exercise redirects.
        return Response(
            status_code=303, headers={"Location": f"/_sandbox/media/image/{device_id}/{ts}/{fmt}"}
        )

    @app.get("/_sandbox/media/image/{device_id}/{ts}/{fmt}")
    async def image_blob(device_id: str, ts: int, fmt: str) -> Response:
        dev = device_or_404(device_id)
        for r in dev.history:
            if r.start <= ts <= r.end:
                r.reviewed = True
        content, mime = world.snapshot_bytes(dev, ts, fmt)
        return Response(content=content, media_type=mime, headers={"X-Media-Timestamp": str(ts)})

    @app.post("/v1/devices/{device_id}/media/video/download", dependencies=[Depends(auth)])
    async def video_download(device_id: str, body: VideoRequest) -> Response:
        if chaos and (fail := _flaky(chaos.flaky_media)):
            return fail
        dev = device_or_404(device_id)
        if not dev.is_camera:
            return _error(400, "bad_request", "device has no camera")
        if body.duration > 900_000:
            return _error(400, "bad_request", "duration must be <= 900000")
        if body.components and len(body.components) != 1:
            return _error(400, "bad_request", "components takes exactly one entry")
        rec = world.recording_covering(device_id, body.timestamp)
        if rec is None:
            return _error(416, "TIMESTAMP_NOT_FOUND", "no recording at requested timestamp")
        rec.reviewed = True
        world.record_on_demand(device_id, body.timestamp)
        available = rec.end - body.timestamp
        partial = available < body.duration
        headers = {"X-Media-Timestamp": str(body.timestamp)}
        if partial:
            headers["X-Media-Length"] = str(available)
        return Response(
            content=world.clip_bytes(dev),
            media_type="video/mp4",
            status_code=206 if partial else 200,
            headers=headers,
        )

    @app.post("/v1/devices/{device_id}/media/audio/playback", dependencies=[Depends(auth)])
    async def chime_playback(device_id: str, body: PlaybackRequest) -> Response:
        dev = device_or_404(device_id)
        if dev.kind != DeviceKind.CHIME:
            return _error(400, "bad_request", "device is not a chime")
        slots = {
            s["event"]
            for s in dev.configurations_resource()["attributes"]["audio"]["customizable_slots"]
        }
        if body.event not in slots:
            return _error(400, "bad_request", f"unknown slot {body.event}; have {sorted(slots)}")
        world.delivered.append(
            {"kind": "chime.play", "device_id": device_id, "event": body.event, "at": now_ms()}
        )
        return Response(status_code=204)

    # ------------------------------------------------------------------ live video (WHEP)

    _SDP_ANSWER = (
        "v=0\r\n"
        "o=- 0 0 IN IP4 127.0.0.1\r\n"
        "s=ring-sandbox\r\n"
        "t=0 0\r\n"
        "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
        "c=IN IP4 127.0.0.1\r\n"
        "a=recvonly\r\n"
        "a=rtpmap:96 H264/90000\r\n"
    )

    @app.post(
        "/v1/devices/{device_id}/media/streaming/whep/sessions",
        dependencies=[Depends(auth)],
    )
    async def whep_open(
        request: Request, device_id: str, component_id: str | None = Query(default=None)
    ) -> Response:
        """WebRTC-HTTP Egress: client POSTs an SDP offer (video only, recvonly),
        gets 201 + an SDP answer and a ``Location`` session URL to DELETE.
        A live view surfaces in Event History as an ``on_demand`` entry."""
        dev = device_or_404(device_id)
        if not dev.is_camera:
            return _error(400, "bad_request", "device has no camera")
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/sdp":
            return _error(
                415, "unsupported_media_type", "WHEP offers use Content-Type: application/sdp"
            )
        offer = await request.body()
        if not offer.lstrip().startswith(b"v="):
            return _error(400, "bad_request", "body must be an SDP offer (starts with v=)")
        if component_id and dev.components and component_id not in dev.components:
            return _error(400, "bad_request", f"unknown component_id {component_id!r}")
        session_id = secrets.token_urlsafe(16)
        world.whep_sessions[session_id] = {
            "device_id": device_id,
            "component_id": component_id,
            "created_ms": now_ms(),
        }
        world.record_on_demand(device_id)
        world.delivered.append(
            {"kind": "whep.session.open", "session_id": session_id, "device_id": device_id}
        )
        return Response(
            status_code=201,
            content=_SDP_ANSWER,
            media_type="application/sdp",
            headers={
                "Location": f"/v1/devices/{device_id}/media/streaming/whep/sessions/{session_id}"
            },
        )

    @app.delete(
        "/v1/devices/{device_id}/media/streaming/whep/sessions/{session_id}",
        dependencies=[Depends(auth)],
    )
    async def whep_close(device_id: str, session_id: str) -> Response:
        sess = world.whep_sessions.pop(session_id, None)
        if sess is None or sess["device_id"] != device_id:
            raise NotFound(f"whep session {session_id} not found")
        world.delivered.append(
            {"kind": "whep.session.close", "session_id": session_id, "device_id": device_id}
        )
        return Response(status_code=204)

    # ------------------------------------------------------------------ control plane

    async def deliver(payload: dict[str, Any]) -> None:
        body = webhooks.encode(payload)
        rid = payload["meta"]["request_id"]
        async with httpx.AsyncClient(timeout=5.0) as client:
            for target in list(world.webhooks):
                # Every draw for this delivery is taken here, before the first
                # await. `Chaos` promises that "seed makes the fault stream
                # deterministic so chaos runs are reproducible in tests", and
                # that held only while deliveries were serial: `inject` ends in
                # `asyncio.create_task(deliver(...))`, so several deliveries
                # run at once, and the `await asyncio.sleep(delay)` below used
                # to sit BETWEEN two draws from this single shared rng. Which
                # coroutine drew next then depended on the event loop, and the
                # same seed gave a different number of drops from one run to
                # the next.
                #
                # Which delivery is dropped still depends on the order they
                # start in, and cannot not: `meta.request_id` is a fresh uuid4,
                # so a delivery has no identity that survives a restart. What
                # is restored is the part a measurement rests on — the same
                # seed and the same number of deliveries give the same counts,
                # because the stream is now consumed in whole per-delivery
                # chunks instead of interleaved ones.
                if rng:
                    drop = chaos.drop and rng.random() < chaos.drop
                    delay = (chaos.delay_ms + rng.randint(0, chaos.jitter_ms)) / 1000
                    copies = 1 + sum(rng.random() < chaos.duplicate for _ in range(2))
                else:
                    drop, delay, copies = False, 0, 1

                if drop:
                    world.delivered.append(
                        {"kind": "webhook.dropped", "url": target.url, "request_id": rid}
                    )
                    continue
                if delay:
                    world.delivered.append(
                        {
                            "kind": "webhook.delayed",
                            "url": target.url,
                            "request_id": rid,
                            "ms": int(delay * 1000),
                        }
                    )
                    await asyncio.sleep(delay)
                for copy in range(copies):
                    if copy:
                        world.delivered.append(
                            {
                                "kind": "webhook.duplicated",
                                "url": target.url,
                                "request_id": rid,
                            }
                        )
                    headers = {
                        "Content-Type": "application/json",
                        webhooks.SIGNATURE_HEADER: webhooks.sign(target.signing_key, body),
                    }
                    try:
                        resp = await client.post(target.url, content=body, headers=headers)
                        world.delivered.append(
                            {
                                "kind": "webhook",
                                "url": target.url,
                                "status": resp.status_code,
                                "request_id": rid,
                            }
                        )
                    except httpx.HTTPError as exc:
                        log.warning("webhook delivery to %s failed: %s", target.url, exc)
                        world.delivered.append(
                            {
                                "kind": "webhook",
                                "url": target.url,
                                "status": None,
                                "error": str(exc),
                                "request_id": rid,
                            }
                        )

    @app.post("/_sandbox/events")
    async def inject(body: InjectEvent) -> dict[str, Any]:
        device_id = body.device_id or (world.cameras()[0].id if world.cameras() else None)
        if not device_id:
            return _error(400, "bad_request", "no device_id and no cameras in world")  # type: ignore[return-value]
        dev = device_or_404(device_id)
        at = body.at or datetime.now(tz=UTC)
        at_ms = int(at.timestamp() * 1000)
        rec = world.record_event(
            dev.id, body.type, sub_type=body.sub_type, at_ms=at_ms, duration_ms=body.duration_ms
        )
        payload = webhooks.build_event(
            event_type=body.type,
            device_id=dev.id,
            account_id=world.account_id,
            occurred_at=at,
            sub_type=body.sub_type,
            component_ids=dev.components or None,
        )
        if body.deliver and world.webhooks:
            _track(asyncio.create_task(deliver(payload)))
        return {"webhook": payload, "history": rec.to_jsonapi(dev.id) if rec else None}

    @app.get("/_sandbox/chaos")
    async def get_chaos() -> dict[str, Any]:
        """Current fault-injection profile, what chaos has done so far (from
        world.delivered kinds webhook.dropped/.duplicated/.delayed), and how
        many deliveries are still in flight.

        ``actions`` is the whole history, while ``GET /_sandbox/state``
        truncates ``delivered`` to its last 50 entries — counting faults from
        ``/state`` undercounts silently on any run of size. Count them here."""
        actions = [d for d in world.delivered if str(d.get("kind", "")).startswith("webhook.")]
        return {
            "profile": None
            if chaos is None
            else {k: getattr(chaos, k) for k in Chaos.__dataclass_fields__},
            "actions": actions,
            # Poll until this reaches zero instead of sleeping: a batch run
            # then waits for the queue rather than for a number somebody
            # guessed about another machine.
            "pending": len(app.state.in_flight),
        }

    @app.post("/_sandbox/chaos")
    async def set_chaos(body: dict[str, Any]) -> dict[str, Any]:
        """Adjust rates live — e.g. {"drop": 0.5, "jitter_ms": 2000}."""
        if chaos is None:
            return _error(400, "bad_request", "server was not started with chaos enabled")
        for k, v in body.items():
            if k in Chaos.__dataclass_fields__ and k != "seed":
                setattr(chaos, k, v)
        return {"profile": {k: getattr(chaos, k) for k in Chaos.__dataclass_fields__}}

    @app.post("/_sandbox/webhooks")
    async def add_webhook(body: WebhookTargetIn) -> dict[str, Any]:
        world.webhooks.append(WebhookTarget(body.url, body.signing_key))
        return {"targets": [t.url for t in world.webhooks]}

    @app.delete("/_sandbox/webhooks")
    async def clear_webhooks() -> dict[str, Any]:
        world.webhooks.clear()
        return {"targets": []}

    @app.post("/_sandbox/subscriptions")
    async def add_subscription(body: SubscriptionIn) -> dict[str, Any]:
        """Activate a partner-app subscription/trial for a device — the sandbox
        stand-in for the Ring appstore enrollment flow. Fires
        ``subscription_activated`` (which is what gates event delivery)."""
        dev = device_or_404(body.device_id)
        if body.state not in ("active", "trialing"):
            return _error(400, "bad_request", "state must be active or trialing")
        now = datetime.now(tz=UTC)
        expires = now + timedelta(days=body.days)
        sub = {
            "id": f"sub_{secrets.token_hex(6)}",
            "device_id": dev.id,
            "plan_id": body.plan_id,
            "state": body.state,
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
            "created_at": now.isoformat().replace("+00:00", "Z"),
        }
        world.subscriptions[sub["id"]] = sub
        _fire(
            webhooks.build_event(
                event_type=WebhookEventType.SUBSCRIPTION_ACTIVATED,
                device_id=dev.id,
                account_id=world.account_id,
                extra_attributes={"plan_id": sub["plan_id"], "expires_at": sub["expires_at"]},
            )
        )
        return {"meta": meta(), "data": _subscription_resource(sub)}

    @app.delete("/_sandbox/subscriptions/{subscription_id}")
    async def del_subscription(subscription_id: str) -> dict[str, Any]:
        sub = world.subscriptions.pop(subscription_id, None)
        if sub is None:
            raise NotFound(f"subscription {subscription_id} not found")
        _fire(
            webhooks.build_event(
                event_type=WebhookEventType.SUBSCRIPTION_DEACTIVATED,
                device_id=sub["device_id"],
                account_id=world.account_id,
                extra_attributes={"plan_id": sub["plan_id"], "expires_at": sub["expires_at"]},
            )
        )
        return {"meta": meta(), "data": _subscription_resource(sub)}

    @app.delete("/_sandbox/app-integration")
    async def unlink_app_integration() -> dict[str, Any]:
        """Simulate the user unlinking the partner app in the Ring app: fires
        ``app_integration_removed`` plus ``device_removed`` per device and
        ``subscription_deactivated`` per subscription, then clears the link."""
        if world.app_integration_status is None:
            return _error(400, "bad_request", "no app integration to unlink")
        world.app_integration_status = None
        _fire(
            webhooks.build_event(
                event_type=WebhookEventType.APP_INTEGRATION_REMOVED,
                source_type="accounts",
                account_id=world.account_id,
            )
        )
        for d in world.devices.values():
            _fire(
                webhooks.build_event(
                    event_type=WebhookEventType.DEVICE_REMOVED,
                    device_id=d.id,
                    account_id=world.account_id,
                )
            )
        for sub in list(world.subscriptions.values()):
            _fire(
                webhooks.build_event(
                    event_type=WebhookEventType.SUBSCRIPTION_DEACTIVATED,
                    device_id=sub["device_id"],
                    account_id=world.account_id,
                    extra_attributes={
                        "plan_id": sub["plan_id"],
                        "expires_at": sub["expires_at"],
                    },
                )
            )
        world.subscriptions.clear()
        world.whep_sessions.clear()
        return {"ok": True}

    @app.post("/_sandbox/devices")
    async def add_device(body: DeviceIn) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"battery": body.battery, "components": body.components}
        if body.id:
            kwargs["id"] = body.id
        dev = world.add(SandboxDevice(body.kind, body.name, **kwargs))
        _fire(
            webhooks.build_event(
                event_type=WebhookEventType.DEVICE_ADDED,
                device_id=dev.id,
                account_id=world.account_id,
            )
        )
        return {"data": dev.to_resource()}

    @app.delete("/_sandbox/devices/{device_id}")
    async def remove_device(device_id: str) -> dict[str, Any]:
        dev = world.remove(device_id)
        if dev is None:
            raise NotFound(f"device {device_id} not found")
        _fire(
            webhooks.build_event(
                event_type=WebhookEventType.DEVICE_REMOVED,
                device_id=dev.id,
                account_id=world.account_id,
            )
        )
        return {"data": dev.to_resource()}

    @app.get("/_sandbox/state")
    async def state() -> dict[str, Any]:
        return {
            "account_id": world.account_id,
            "devices": [
                {
                    "id": d.id,
                    "kind": d.kind,
                    "name": d.name,
                    "online": d.online,
                    "events": len(d.history),
                }
                for d in world.devices.values()
            ],
            "webhooks": [t.url for t in world.webhooks],
            "app_integration_status": world.app_integration_status,
            "subscriptions": [_subscription_resource(s) for s in world.subscriptions.values()],
            "whep_sessions": [
                {"session_id": sid, **sess} for sid, sess in world.whep_sessions.items()
            ],
            "delivered": world.delivered[-50:],
        }

    @app.post("/_sandbox/load")
    async def load(request: Request) -> dict[str, Any]:
        """Load `record` fixtures into the running world — body is either
        ``{"path": "fixtures/"}`` or a ``{filename: parsed_json}`` map. The
        record→replay recipe: capture a real API surface once, replay it in
        CI forever."""
        from .world import load_fixture_docs

        body = await request.json()
        if isinstance(body, dict) and "path" in body:
            counts = load_fixture_docs(world, body["path"])
        else:
            counts = load_fixture_docs(world, body)
        return {"ok": True, "loaded": counts}

    @app.post("/_sandbox/reset")
    async def reset() -> dict[str, Any]:
        fresh = default_world()
        world.devices = fresh.devices
        world.delivered.clear()
        world.app_integration_status = None
        world.subscriptions.clear()
        world.whep_sessions.clear()
        return {"ok": True}

    @app.get("/_sandbox/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
