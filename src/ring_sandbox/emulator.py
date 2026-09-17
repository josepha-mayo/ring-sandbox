"""FastAPI application that emulates ``https://api.amazonvision.com``.

Data-plane routes mirror the documented Ring Partner API. Control-plane routes live under
``/_sandbox`` and let tests/scenarios inject events, register webhook targets, and reset.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import secrets
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import webhooks
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


def create_app(world: World | None = None, chaos: Chaos | None = None) -> FastAPI:
    world = world or default_world()
    chaos = dataclasses.replace(chaos) if chaos else None
    app = FastAPI(title="ring-sandbox", version="0.1.0", docs_url="/_sandbox/docs")
    app.state.world = world
    app.state.chaos = chaos
    rng = chaos.rng() if chaos else None

    def _flaky(rate: float) -> JSONResponse | None:
        if rng and rate and rng.random() < rate:
            return _error(500, "chaos", "chaos-injected transient failure — retry")
        return None

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

    # ------------------------------------------------------------------ control plane

    async def deliver(payload: dict[str, Any]) -> None:
        body = webhooks.encode(payload)
        rid = payload["meta"]["request_id"]
        async with httpx.AsyncClient(timeout=5.0) as client:
            for target in list(world.webhooks):
                if rng and chaos.drop and rng.random() < chaos.drop:
                    world.delivered.append(
                        {"kind": "webhook.dropped", "url": target.url, "request_id": rid}
                    )
                    continue
                delay = (chaos.delay_ms + rng.randint(0, chaos.jitter_ms)) / 1000 if rng else 0
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
                copies = 1 + sum(rng.random() < chaos.duplicate for _ in range(2)) if rng else 1
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
            asyncio.create_task(deliver(payload))
        return {"webhook": payload, "history": rec.to_jsonapi(dev.id) if rec else None}

    @app.get("/_sandbox/chaos")
    async def get_chaos() -> dict[str, Any]:
        """Current fault-injection profile, plus what chaos has done so far
        (from world.delivered kinds webhook.dropped/.duplicated/.delayed)."""
        actions = [d for d in world.delivered if str(d.get("kind", "")).startswith("webhook.")]
        return {
            "profile": None
            if chaos is None
            else {k: getattr(chaos, k) for k in Chaos.__dataclass_fields__},
            "actions": actions,
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

    @app.post("/_sandbox/devices")
    async def add_device(body: DeviceIn) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"battery": body.battery, "components": body.components}
        if body.id:
            kwargs["id"] = body.id
        dev = world.add(SandboxDevice(body.kind, body.name, **kwargs))
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
            "delivered": world.delivered[-50:],
        }

    @app.post("/_sandbox/reset")
    async def reset() -> dict[str, Any]:
        fresh = default_world()
        world.devices = fresh.devices
        world.delivered.clear()
        return {"ok": True}

    @app.get("/_sandbox/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
