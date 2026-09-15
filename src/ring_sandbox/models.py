"""Pydantic models for the Ring Partner API (JSON:API shapes).

Shapes follow https://developer.amazon.com/docs/ring/api-documentation.html. Every model
uses ``extra="allow"`` so that new attributes Ring adds do not break parsing; the typed
fields are the ones documented today.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# --------------------------------------------------------------------------- common


class ResourceIdentifier(_Model):
    type: str
    id: str


class Relationship(_Model):
    data: ResourceIdentifier | list[ResourceIdentifier] | None = None
    links: dict[str, str] | None = None


class Meta(_Model):
    time: datetime | None = None


class APIError(_Model):
    status: str | None = None
    code: str | None = None
    title: str | None = None
    detail: str | None = None


# --------------------------------------------------------------------------- users


class UserAttributes(_Model):
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone_number: str | None = None


class User(_Model):
    type: Literal["users"] = "users"
    id: str
    attributes: UserAttributes = Field(default_factory=UserAttributes)

    @property
    def account_id(self) -> str:
        return self.id


# --------------------------------------------------------------------------- devices


class DeviceAttributes(_Model):
    name: str
    image_url: str | None = None


class Device(_Model):
    type: Literal["devices"] = "devices"
    id: str
    attributes: DeviceAttributes
    relationships: dict[str, Relationship] = Field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.attributes.name


class VideoCapabilities(_Model):
    configurations: list[str] | None = None
    codecs: list[str] | None = None
    ratio: str | None = None
    max_resolution: int | None = None
    supported_resolutions: list[int] | None = None
    component_id: str | None = None


class ConfigurableCapability(_Model):
    configurations: list[str] | None = None


class AudioCapabilities(_Model):
    # Real cameras report both as null (observed on the Playground doorbell), not absent.
    customizable_slots: int | None = None
    supported_actions: list[str] | None = None


class ComponentItem(_Model):
    component_id: str
    video: VideoCapabilities | None = None


class Components(_Model):
    items: list[ComponentItem] = Field(default_factory=list)


class BatteryCapability(_Model):
    supported: bool | None = None


class CapabilitiesAttributes(_Model):
    video: VideoCapabilities | None = None
    motion_detection: ConfigurableCapability | None = None
    image_enhancements: ConfigurableCapability | None = None
    audio: AudioCapabilities | None = None
    components: Components | None = None
    battery_status: BatteryCapability | None = None


class Capabilities(_Model):
    type: Literal["device-capabilities"] = "device-capabilities"
    id: str
    attributes: CapabilitiesAttributes

    @property
    def is_camera(self) -> bool:
        v = self.attributes.video
        return bool(v and (v.codecs or v.max_resolution))

    @property
    def is_chime(self) -> bool:
        a = self.attributes.audio
        return bool(a and a.supported_actions and "chime.play" in a.supported_actions)

    @property
    def is_multi_camera(self) -> bool:
        return bool(self.attributes.components and self.attributes.components.items)


class Detection(_Model):
    faulted: bool | None = None
    detected: bool | None = None


class Reading(_Model):
    value: float | str | None = None
    unit: str | None = None


class StatusAttributes(_Model):
    online: bool | None = None
    reported_at: datetime | None = None
    signal_strength: Reading | None = None
    battery_status: dict[str, Any] | None = None
    contact_detection: Detection | None = None
    flood_detection: Detection | None = None
    freeze_detection: Detection | None = None
    tamper_detection: Detection | None = None
    temperature: Reading | dict[str, Any] | None = None
    humidity: Reading | dict[str, Any] | None = None

    @property
    def battery_percentage(self) -> int | None:
        """Battery level, or None when the device reports a sentinel (e.g. 255) or nothing."""
        pct = (self.battery_status or {}).get("percentage")
        return pct if isinstance(pct, int) and 0 <= pct <= 100 else None


class Status(_Model):
    type: Literal["device-status"] = "device-status"
    id: str
    attributes: StatusAttributes


class Location(_Model):
    type: Literal["locations"] = "locations"
    id: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class Configurations(_Model):
    type: Literal["device-configurations"] = "device-configurations"
    id: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class DeviceBundle(_Model):
    """A device plus whatever was side-loaded via ``?include=``."""

    device: Device
    status: Status | None = None
    capabilities: Capabilities | None = None
    configurations: Configurations | None = None
    location: Location | None = None

    @property
    def id(self) -> str:
        return self.device.id

    @property
    def name(self) -> str:
        return self.device.name

    @property
    def online(self) -> bool | None:
        return self.status.attributes.online if self.status else None


# --------------------------------------------------------------------------- history


class HistoryEventType(StrEnum):
    MOTION = "motion"
    DING = "ding"
    ON_DEMAND = "on_demand"


class HistoryEventAttributes(_Model):
    event_type: str
    is_third_party_reviewed: bool | None = None
    start: int
    end: int | None = None

    @property
    def started_at(self) -> datetime:
        return datetime.fromtimestamp(self.start / 1000, tz=UTC)

    @property
    def ended_at(self) -> datetime | None:
        return datetime.fromtimestamp(self.end / 1000, tz=UTC) if self.end else None


class HistoryEvent(_Model):
    type: Literal["history-events"] = "history-events"
    id: str
    attributes: HistoryEventAttributes
    relationships: dict[str, Relationship] = Field(default_factory=dict)

    @property
    def device_id(self) -> str | None:
        rel = self.relationships.get("source")
        if rel and isinstance(rel.data, ResourceIdentifier):
            return rel.data.id
        return None


class HistoryPage(_Model):
    data: list[HistoryEvent] = Field(default_factory=list)
    links: dict[str, str] = Field(default_factory=dict)

    @property
    def next_key(self) -> str | None:
        nxt = self.links.get("next")
        if not nxt or "page[key]=" not in nxt:
            return None
        return nxt.split("page[key]=", 1)[1].split("&", 1)[0]


# --------------------------------------------------------------------------- webhooks


class WebhookEventType(StrEnum):
    MOTION_DETECTED = "motion_detected"
    BUTTON_PRESS = "button_press"
    DEVICE_ADDED = "device_added"
    DEVICE_REMOVED = "device_removed"
    DEVICE_ONLINE = "device_online"
    DEVICE_OFFLINE = "device_offline"
    APP_INTEGRATION_ADDED = "app_integration_added"
    APP_INTEGRATION_REMOVED = "app_integration_removed"
    SUBSCRIPTION_ACTIVATED = "subscription_activated"
    SUBSCRIPTION_DEACTIVATED = "subscription_deactivated"
    # Sensors (Early Access)
    CONTACT_SENSOR_FAULTED = "contact_sensor_faulted"
    CONTACT_SENSOR_CLEARED = "contact_sensor_cleared"
    TAMPER_DETECTED = "tamper_detected"
    TAMPER_CLEARED = "tamper_cleared"
    FLOOD_DETECTED = "flood_detected"
    FLOOD_CLEARED = "flood_cleared"
    FREEZE_DETECTED = "freeze_detected"
    FREEZE_CLEARED = "freeze_cleared"
    TEMPERATURE_EXCEEDED = "temperature_exceeded"
    TEMPERATURE_CLEARED = "temperature_cleared"
    HUMIDITY_EXCEEDED = "humidity_exceeded"
    HUMIDITY_CLEARED = "humidity_cleared"
    PM25_EXCEEDED = "pm25_exceeded"
    PM25_CLEARED = "pm25_cleared"
    CO_EXCEEDED = "co_exceeded"
    CO_CLEARED = "co_cleared"


class MotionSubType(StrEnum):
    HUMAN = "human"
    VEHICLE = "vehicle"
    ANIMAL = "animal"
    PACKAGE = "package"
    MOTION = "motion"
    OTHER_MOTION = "other_motion"


class WebhookMeta(_Model):
    version: str = "1.1"
    time: datetime
    request_id: str
    account_id: str | None = None


class WebhookAttributes(_Model):
    source: str
    source_type: str | None = None
    source_id: str | None = None
    timestamp: int | None = None
    sub_type: str | None = None
    component_ids: list[str] | None = None
    plan_id: str | None = None
    expires_at: datetime | None = None

    @property
    def occurred_at(self) -> datetime | None:
        if self.timestamp is None:
            return None
        return datetime.fromtimestamp(self.timestamp / 1000, tz=UTC)


class WebhookData(_Model):
    id: str
    type: str
    attributes: WebhookAttributes
    relationships: dict[str, Relationship] = Field(default_factory=dict)


class WebhookEvent(_Model):
    meta: WebhookMeta
    data: WebhookData

    @property
    def event_type(self) -> str:
        return self.data.type

    @property
    def device_id(self) -> str:
        return self.data.attributes.source

    @property
    def request_id(self) -> str:
        return self.meta.request_id

    @property
    def sub_type(self) -> str | None:
        return self.data.attributes.sub_type

    @property
    def occurred_at(self) -> datetime:
        return self.data.attributes.occurred_at or self.meta.time


# --------------------------------------------------------------------------- media


class MediaClip(_Model):
    content: bytes
    content_type: str = "video/mp4"
    partial: bool = False
    actual_timestamp: int | None = None
    actual_length_ms: int | None = None


class Snapshot(_Model):
    content: bytes
    content_type: str = "image/jpeg"
    timestamp: int | None = None
