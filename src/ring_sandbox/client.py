"""Typed synchronous client for the Ring Partner API.

Point ``base_url`` at ``https://api.amazonvision.com`` (default) or at a running
:mod:`ring_sandbox.emulator` instance for offline development.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any

import httpx

from .models import (
    APIError,
    Capabilities,
    Configurations,
    Device,
    DeviceBundle,
    HistoryEvent,
    HistoryPage,
    Location,
    MediaClip,
    Snapshot,
    Status,
    User,
)

PRODUCTION_BASE_URL = "https://api.amazonvision.com"
_INCLUDABLE = ("status", "capabilities", "configurations", "location")


class RingAPIError(Exception):
    def __init__(self, status_code: int, errors: list[APIError], body: str = ""):
        self.status_code = status_code
        self.errors = errors
        self.body = body
        detail = "; ".join(e.detail or e.title or e.code or "" for e in errors) or body[:200]
        super().__init__(f"Ring API {status_code}: {detail}")

    @property
    def code(self) -> str | None:
        return self.errors[0].code if self.errors else None


class RingClient:
    """Bearer-token client. Tokens from the Playground last ~30 minutes; production
    access tokens ~4 hours (refresh via :meth:`RingClient.refresh`)."""

    def __init__(
        self,
        access_token: str,
        *,
        base_url: str = PRODUCTION_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ):
        self._token = access_token
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    # ----------------------------------------------------------------- plumbing

    @property
    def access_token(self) -> str:
        return self._token

    @access_token.setter
    def access_token(self, value: str) -> None:
        self._token = value

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> RingClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", **extra}

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            resp = self._http.request(
                method, path, headers=self._headers(**kwargs.pop("headers", {})), **kwargs
            )
            if resp.status_code == 429 and attempt < self.max_retries:
                delay = float(resp.headers.get("Retry-After", 2**attempt))
                time.sleep(delay)
                attempt += 1
                continue
            if resp.status_code >= 400:
                raise RingAPIError(resp.status_code, _parse_errors(resp), resp.text)
            return resp

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params).json()

    # ----------------------------------------------------------------- account

    def me(self) -> User:
        return User.model_validate(self._get_json("/v1/users/me")["data"])

    # ----------------------------------------------------------------- devices

    def devices(self, include: Iterable[str] = ()) -> list[DeviceBundle]:
        include = tuple(include)
        unknown = set(include) - set(_INCLUDABLE)
        if unknown:
            raise ValueError(f"unknown include(s) {sorted(unknown)}; allowed: {_INCLUDABLE}")
        params = {"include": ",".join(include)} if include else None
        doc = self._get_json("/v1/devices", params=params)
        return _bundle(doc)

    def device(self, device_id: str) -> Device:
        return Device.model_validate(self._get_json(f"/v1/devices/{device_id}")["data"])

    def status(self, device_id: str) -> Status:
        return Status.model_validate(self._get_json(f"/v1/devices/{device_id}/status")["data"])

    def capabilities(self, device_id: str, component_id: str | None = None) -> Capabilities:
        params = {"component_id": component_id} if component_id else None
        doc = self._get_json(f"/v1/devices/{device_id}/capabilities", params=params)
        return Capabilities.model_validate(doc["data"])

    def configurations(self, device_id: str, component_id: str | None = None) -> Configurations:
        params = {"component_id": component_id} if component_id else None
        doc = self._get_json(f"/v1/devices/{device_id}/configurations", params=params)
        return Configurations.model_validate(doc["data"])

    def location(self, device_id: str) -> Location:
        return Location.model_validate(self._get_json(f"/v1/devices/{device_id}/location")["data"])

    # ----------------------------------------------------------------- history

    def events_page(
        self,
        device_id: str,
        *,
        event_types: Iterable[str] | None = None,
        page_key: str | None = None,
    ) -> HistoryPage:
        params: dict[str, Any] = {}
        if event_types:
            params["event_types"] = ",".join(event_types)
        if page_key:
            params["page[key]"] = page_key
        doc = self._get_json(f"/v1/history/devices/{device_id}/events", params=params or None)
        return HistoryPage.model_validate(doc)

    def events(
        self,
        device_id: str,
        *,
        event_types: Iterable[str] | None = None,
        since: datetime | None = None,
        max_pages: int = 20,
    ) -> Iterator[HistoryEvent]:
        """Iterate newest-first through history, following ``links.next``.

        Stops early once events are older than ``since`` (history is newest-first).
        """
        event_types = tuple(event_types) if event_types else None
        page_key: str | None = None
        since_ms = int(since.timestamp() * 1000) if since else None
        for _ in range(max_pages):
            page = self.events_page(device_id, event_types=event_types, page_key=page_key)
            if not page.data:
                return
            for ev in page.data:
                if since_ms is not None and ev.attributes.start < since_ms:
                    return
                yield ev
            page_key = page.next_key
            if not page_key:
                return

    # ----------------------------------------------------------------- media

    def snapshot_at(
        self,
        device_id: str,
        timestamp: datetime | int,
        *,
        fmt: str = "jpeg",
        width: int | None = None,
        height: int | None = None,
        component_id: str | None = None,
    ) -> Snapshot:
        body: dict[str, Any] = {"type": "at_timestamp", "timestamp": _ms(timestamp)}
        return self._snapshot(device_id, body, fmt, width, height, component_id)

    def snapshot_latest(
        self,
        device_id: str,
        start: datetime | int,
        end: datetime | int | None = None,
        *,
        fmt: str = "jpeg",
        width: int | None = None,
        height: int | None = None,
        component_id: str | None = None,
    ) -> Snapshot:
        body: dict[str, Any] = {"type": "latest_in_range", "start_timestamp": _ms(start)}
        if end is not None:
            body["end_timestamp"] = _ms(end)
        return self._snapshot(device_id, body, fmt, width, height, component_id)

    def _snapshot(
        self,
        device_id: str,
        body: dict[str, Any],
        fmt: str,
        width: int | None,
        height: int | None,
        component_id: str | None,
    ) -> Snapshot:
        opts: dict[str, Any] = {"format": fmt}
        if width or height:
            opts["resolution"] = {"width": width, "height": height}
        body["image_options"] = opts
        if component_id:
            body["components"] = [{"component_id": component_id}]
        resp = self._request(
            "POST",
            f"/v1/devices/{device_id}/media/image/download",
            json=body,
            headers={"Content-Type": "application/json", "Accept": "*/*"},
        )
        ts = resp.headers.get("X-Media-Timestamp")
        return Snapshot(
            content=resp.content,
            content_type=resp.headers.get("Content-Type", f"image/{fmt}"),
            timestamp=int(ts) if ts else None,
        )

    def clip(
        self,
        device_id: str,
        timestamp: datetime | int,
        duration_ms: int,
        *,
        codec: str | None = None,
        frame_rate: int | None = None,
        width: int | None = None,
        height: int | None = None,
        audio: bool = False,
        component_id: str | None = None,
    ) -> MediaClip:
        """Download an existing recording. Does NOT trigger recording; expect 416 if idle."""
        if duration_ms > 900_000:
            raise ValueError("duration_ms must be <= 900000 (15 minutes)")
        body: dict[str, Any] = {"timestamp": _ms(timestamp), "duration": duration_ms}
        video: dict[str, Any] = {}
        if codec:
            video["codec"] = codec
        if frame_rate:
            video["frame_rate"] = frame_rate
        if width or height:
            video["resolution"] = {"width": width, "height": height}
        if video:
            body["video_options"] = video
        if audio:
            body["audio_options"] = {"audio_enabled": True}
        if component_id:
            body["components"] = [{"component_id": component_id}]
        resp = self._request(
            "POST",
            f"/v1/devices/{device_id}/media/video/download",
            json=body,
            headers={"Content-Type": "application/json", "Accept": "*/*"},
        )
        ts = resp.headers.get("X-Media-Timestamp")
        ln = resp.headers.get("X-Media-Length")
        return MediaClip(
            content=resp.content,
            content_type=resp.headers.get("Content-Type", "video/mp4"),
            partial=resp.status_code == 206,
            actual_timestamp=int(ts) if ts else None,
            actual_length_ms=int(ln) if ln else None,
        )

    # ----------------------------------------------------------------- chimes

    def chime_play(self, device_id: str, slot_event: str) -> dict[str, Any]:
        """Play one of the app's audio slots (e.g. ``ring-appstore-event-1``) on a chime."""
        resp = self._request(
            "POST",
            f"/v1/devices/{device_id}/media/audio/playback",
            json={"event": slot_event},
            headers={"Content-Type": "application/json"},
        )
        return resp.json() if resp.content else {}


# --------------------------------------------------------------------------- helpers


def _ms(value: datetime | int) -> int:
    return value if isinstance(value, int) else int(value.timestamp() * 1000)


def _parse_errors(resp: httpx.Response) -> list[APIError]:
    try:
        errs = resp.json().get("errors", [])
        return [APIError.model_validate(e) for e in errs]
    except Exception:
        return []


def _bundle(doc: dict[str, Any]) -> list[DeviceBundle]:
    included: dict[tuple[str, str], dict[str, Any]] = {
        (r["type"], r["id"]): r for r in doc.get("included", [])
    }
    parsers = {
        "status": ("device-status", Status),
        "capabilities": ("device-capabilities", Capabilities),
        "configurations": ("device-configurations", Configurations),
        "location": ("locations", Location),
    }
    out: list[DeviceBundle] = []
    for raw in doc.get("data", []):
        device = Device.model_validate(raw)
        extras: dict[str, Any] = {}
        for rel_name, (rtype, model) in parsers.items():
            rel = device.relationships.get(rel_name)
            if rel and rel.data is not None and not isinstance(rel.data, list):
                hit = included.get((rtype, rel.data.id))
                if hit:
                    extras[rel_name] = model.model_validate(hit)
        out.append(DeviceBundle(device=device, **extras))
    return out
