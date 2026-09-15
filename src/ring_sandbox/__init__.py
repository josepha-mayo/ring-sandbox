"""ring-sandbox: typed client + offline emulator for the Ring Partner API."""

from .client import PRODUCTION_BASE_URL, RingAPIError, RingClient
from .models import (
    Capabilities,
    Device,
    DeviceBundle,
    HistoryEvent,
    MediaClip,
    MotionSubType,
    Snapshot,
    Status,
    User,
    WebhookEvent,
    WebhookEventType,
)
from .webhooks import SignatureError, sign, verify

__all__ = [
    "PRODUCTION_BASE_URL",
    "Capabilities",
    "Device",
    "DeviceBundle",
    "HistoryEvent",
    "MediaClip",
    "MotionSubType",
    "RingAPIError",
    "RingClient",
    "SignatureError",
    "Snapshot",
    "Status",
    "User",
    "WebhookEvent",
    "WebhookEventType",
    "sign",
    "verify",
]
__version__ = "0.1.0"
