"""Webhook signing and verification for Ring Partner API webhooks.

Ring signs every webhook with HMAC-SHA256 over the *raw request body* and puts the hex
digest in ``X-Signature: sha256=<hex>``. Verify against the raw bytes, never against a
re-serialized JSON object.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .models import WebhookEvent

SIGNATURE_HEADER = "X-Signature"
_PREFIX = "sha256="


def sign(signing_key: str, raw_body: bytes) -> str:
    """Return the ``X-Signature`` header value for ``raw_body``."""
    digest = hmac.new(signing_key.encode(), raw_body, hashlib.sha256).hexdigest()
    return _PREFIX + digest


def verify(signing_key: str, raw_body: bytes, received_signature: str | None) -> bool:
    """Constant-time verification of a Ring webhook signature."""
    if not received_signature:
        return False
    expected = sign(signing_key, raw_body)
    return hmac.compare_digest(expected, received_signature.strip())


class SignatureError(ValueError):
    pass


def parse(
    raw_body: bytes, *, signing_key: str | None = None, signature: str | None = None
) -> WebhookEvent:
    """Parse (and optionally verify) a webhook body into a :class:`WebhookEvent`.

    Pass ``signing_key`` and ``signature`` to enforce verification; omit both to parse only
    (useful in tests or when a reverse proxy already verified the request).
    """
    if signing_key is not None and not verify(signing_key, raw_body, signature):
        raise SignatureError("Ring webhook signature mismatch")
    return WebhookEvent.model_validate_json(raw_body)


def build_event(
    *,
    event_type: str,
    device_id: str,
    account_id: str = "ava1.ring.account.SANDBOX",
    occurred_at: datetime | None = None,
    sub_type: str | None = None,
    component_ids: Iterable[str] | None = None,
    request_id: str | None = None,
    extra_attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct a v1.1 webhook payload identical in shape to what Ring sends."""
    occurred_at = occurred_at or datetime.now(tz=UTC)
    ts_ms = int(occurred_at.timestamp() * 1000)
    attributes: dict[str, Any] = {
        "source": device_id,
        "source_type": "devices",
        "timestamp": ts_ms,
    }
    if sub_type is not None:
        attributes["sub_type"] = sub_type
    if component_ids is not None:
        attributes["component_ids"] = list(component_ids)
    if extra_attributes:
        attributes.update(extra_attributes)
    return {
        "meta": {
            "version": "1.1",
            "time": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
            "request_id": request_id or str(uuid.uuid4()),
            "account_id": account_id,
        },
        "data": {
            "id": f"{device_id}_{event_type}_{ts_ms}",
            "type": event_type,
            "attributes": attributes,
            "relationships": {"devices": {"links": {"self": f"/v1/devices/{device_id}"}}},
        },
    }


def encode(payload: dict[str, Any]) -> bytes:
    """Serialize a payload the way the emulator sends it (compact, sorted keys)."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
