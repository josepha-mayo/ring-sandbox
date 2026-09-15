from datetime import UTC, datetime, timedelta

import pytest

from ring_sandbox import RingAPIError, RingClient, WebhookEvent, sign, verify, webhooks
from ring_sandbox.world import DeviceKind, World


def test_me(ring_client: RingClient, ring_world: World):
    me = ring_client.me()
    assert me.account_id == ring_world.account_id
    assert me.attributes.email == "sandbox@example.com"


def test_devices_with_includes(ring_client: RingClient):
    bundles = ring_client.devices(include=["status", "capabilities"])
    assert len(bundles) == 4
    by_name = {b.name: b for b in bundles}
    front = by_name["Front Door"]
    assert front.online is True
    assert front.capabilities and front.capabilities.is_camera
    chime = by_name["Kitchen Chime"]
    assert chime.capabilities and chime.capabilities.is_chime and not chime.capabilities.is_camera
    sensor = by_name["Front Door Sensor"]
    assert sensor.capabilities and not sensor.capabilities.is_camera


def test_bad_include_rejected_client_side(ring_client: RingClient):
    with pytest.raises(ValueError):
        ring_client.devices(include=["bogus"])


def test_unauthorized(ring_transport):
    with RingClient("", base_url="http://sandbox", transport=ring_transport) as c:
        with pytest.raises(RingAPIError) as ei:
            c.devices()
    assert ei.value.status_code == 401


def test_expired_token_refreshes_and_retries(ring_transport, ring_world: World):
    ring_world.required_token = "current-token"
    rotated = []
    with RingClient(
        "stale-token",
        base_url="http://sandbox",
        transport=ring_transport,
        refresh_token="refresh-1",
        token_url="http://sandbox/oauth/token",
        on_token_refresh=rotated.append,
    ) as client:
        assert client.me().account_id == ring_world.account_id
        assert client.access_token == ring_world.required_token != "stale-token"
    assert rotated[0]["access_token"] == ring_world.required_token
    assert rotated[0]["refresh_token"] and rotated[0]["expires_in"] == 14400


def test_refresh_failure_surfaces_token_error(ring_transport, ring_world: World):
    ring_world.required_token = "current-token"
    with RingClient(
        "stale-token",
        base_url="http://sandbox",
        transport=ring_transport,
        refresh_token="invalid",
        token_url="http://sandbox/oauth/token",
    ) as client:
        with pytest.raises(RingAPIError) as ei:
            client.me()
    assert ei.value.status_code == 400 and ei.value.code == "invalid_grant"


def test_token_url_requires_https_or_explicit_transport():
    with pytest.raises(ValueError):
        RingClient("t", token_url="http://auth.example.test/oauth/token")


def test_status_sensor_semantics(ring_client: RingClient, ring_control, ring_world: World):
    sensor = next(d for d in ring_world.devices.values() if d.kind == DeviceKind.CONTACT_SENSOR)
    st = ring_client.status(sensor.id)
    assert st.attributes.contact_detection and st.attributes.contact_detection.faulted is False
    assert st.attributes.battery_percentage == 91
    ring_control.post(
        "/_sandbox/events", json={"device_id": sensor.id, "type": "contact_sensor_faulted"}
    ).raise_for_status()
    st = ring_client.status(sensor.id)
    assert st.attributes.contact_detection.faulted is True


def test_history_filter_and_pagination(ring_client: RingClient, ring_control, ring_world: World):
    cam = ring_world.cameras()[0]
    base = datetime.now(tz=UTC) - timedelta(hours=2)
    for i in range(60):
        sub = "human" if i % 3 == 0 else "vehicle"
        ring_control.post(
            "/_sandbox/events",
            json={
                "device_id": cam.id,
                "type": "motion_detected",
                "sub_type": sub,
                "at": (base + timedelta(seconds=30 * i)).isoformat(),
                "deliver": False,
            },
        ).raise_for_status()
    ring_control.post(
        "/_sandbox/events", json={"device_id": cam.id, "type": "button_press", "deliver": False}
    )

    all_events = list(ring_client.events(cam.id))
    assert len(all_events) == 61
    assert all_events[0].attributes.start >= all_events[-1].attributes.start  # newest first

    humans = list(ring_client.events(cam.id, event_types=["motion.human"]))
    assert len(humans) == 20

    dings = list(ring_client.events(cam.id, event_types=["ding"]))
    assert len(dings) == 1 and dings[0].attributes.event_type == "ding"

    recent = list(ring_client.events(cam.id, since=base + timedelta(seconds=30 * 50)))
    assert len(recent) == 11


def test_snapshot_and_clip(ring_client: RingClient, ring_control, ring_world: World):
    cam = ring_world.cameras()[0]
    at = datetime.now(tz=UTC) - timedelta(minutes=5)
    ring_control.post(
        "/_sandbox/events",
        json={
            "device_id": cam.id,
            "type": "motion_detected",
            "sub_type": "human",
            "at": at.isoformat(),
            "deliver": False,
        },
    ).raise_for_status()

    snap = ring_client.snapshot_at(cam.id, at + timedelta(seconds=2))
    assert snap.content.startswith(b"\x89PNG")
    assert snap.timestamp is not None

    latest = ring_client.snapshot_latest(cam.id, at - timedelta(minutes=1))
    assert latest.content.startswith(b"\x89PNG")

    clip = ring_client.clip(cam.id, at + timedelta(seconds=1), 5_000)
    assert clip.content[4:8] == b"ftyp" and not clip.partial

    partial = ring_client.clip(cam.id, at + timedelta(seconds=15), 60_000)
    assert partial.partial and partial.actual_length_ms == 5_000

    with pytest.raises(RingAPIError) as ei:
        ring_client.clip(cam.id, at - timedelta(hours=1), 5_000)
    assert ei.value.status_code == 416 and ei.value.code == "TIMESTAMP_NOT_FOUND"

    # media access flips is_third_party_reviewed
    ev = next(ring_client.events(cam.id))
    assert ev.attributes.is_third_party_reviewed is True


def test_chime_playback(ring_client: RingClient, ring_world: World):
    chime = next(d for d in ring_world.devices.values() if d.kind == DeviceKind.CHIME)
    ring_client.chime_play(chime.id, "ring-appstore-event-1")
    assert ring_world.delivered[-1]["kind"] == "chime.play"
    with pytest.raises(RingAPIError):
        ring_client.chime_play(chime.id, "nope")
    cam = ring_world.cameras()[0]
    with pytest.raises(RingAPIError):
        ring_client.chime_play(cam.id, "ring-appstore-event-1")


def test_webhook_signing_roundtrip():
    payload = webhooks.build_event(
        event_type="motion_detected", device_id="ava1.ring.device.X", sub_type="human"
    )
    body = webhooks.encode(payload)
    sig = sign("k3y", body)
    assert sig.startswith("sha256=")
    assert verify("k3y", body, sig)
    assert not verify("other", body, sig)
    assert not verify("k3y", body + b" ", sig)
    ev = webhooks.parse(body, signing_key="k3y", signature=sig)
    assert isinstance(ev, WebhookEvent)
    assert ev.event_type == "motion_detected" and ev.sub_type == "human"
    assert ev.device_id == "ava1.ring.device.X"
    with pytest.raises(webhooks.SignatureError):
        webhooks.parse(body, signing_key="wrong", signature=sig)


def test_inject_returns_v11_payload(ring_control):
    resp = ring_control.post(
        "/_sandbox/events", json={"type": "motion_detected", "sub_type": "package"}
    )
    resp.raise_for_status()
    wh = resp.json()["webhook"]
    assert wh["meta"]["version"] == "1.1"
    assert wh["data"]["type"] == "motion_detected"
    assert wh["data"]["attributes"]["sub_type"] == "package"
    assert resp.json()["history"]["attributes"]["event_type"] == "motion"
