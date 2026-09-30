from datetime import UTC, datetime

import httpx
import pytest

from ring_sandbox import RingAPIError, RingClient, webhooks

API = "https://api.amazonvision.com"
MEDIA = "https://media.example.test"
PNG = b"\x89PNG\r\n\x1a\nplaceholder"


def test_media_redirect_never_forwards_credentials_or_cookies():
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                303,
                headers={
                    "Location": MEDIA + "/image?signature=private",
                    "Set-Cookie": "secret=value",
                },
            )
        return httpx.Response(
            200, content=PNG, headers={"Content-Type": "image/png", "X-Media-Timestamp": "1000"}
        )

    with RingClient(
        "private-token", media_origins=[MEDIA], transport=httpx.MockTransport(handler)
    ) as ring:
        assert ring.snapshot_at("camera", 1000).content == PNG
    assert seen[0].headers["Authorization"] == "Bearer private-token"
    assert seen[1].method == "GET"
    assert "Authorization" not in seen[1].headers and "Cookie" not in seen[1].headers


@pytest.mark.parametrize(
    "target",
    [
        "http://169.254.169.254/latest/meta-data",
        "https://untrusted.example.test/image",
        "https://user:password@media.example.test/image",
        "http://media.example.test/image",
    ],
)
def test_unapproved_redirect_is_blocked_before_second_request(target):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(303, headers={"Location": target})

    with RingClient("secret", transport=httpx.MockTransport(handler)) as ring:
        with pytest.raises(ValueError):
            ring.snapshot_at("camera", 1000)
    assert len(seen) == 1


def test_wildcard_media_origin_matches_host_suffix_only():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.host == "api.amazonvision.com":
            return httpx.Response(
                303, headers={"Location": "https://bucket.s3.amazonaws.com/signed?x=1"}
            )
        return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})

    with RingClient(
        "secret", media_origins=["*.amazonaws.com"], transport=httpx.MockTransport(handler)
    ) as ring:
        assert ring.snapshot_at("camera", 1000).content == PNG
    assert seen[1].url.host == "bucket.s3.amazonaws.com"
    assert "Authorization" not in seen[1].headers


@pytest.mark.parametrize(
    "target",
    ["https://amazonaws.com.evil.test/image", "https://s3.amazonaws.com:8443/image"],
)
def test_wildcard_media_origin_rejects_lookalikes(target):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(303, headers={"Location": target})

    with RingClient(
        "secret", media_origins=["*.amazonaws.com"], transport=httpx.MockTransport(handler)
    ) as ring:
        with pytest.raises(ValueError):
            ring.snapshot_at("camera", 1000)
    assert len(seen) == 1


def test_json_endpoints_do_not_follow_redirects():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": MEDIA + "/devices"})

    with RingClient("secret", transport=httpx.MockTransport(handler)) as ring:
        with pytest.raises(ValueError):
            ring.devices()
    assert len(seen) == 1


def test_opaque_device_id_cannot_change_path_or_query():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(404, json={"errors": []})

    with RingClient("secret", transport=httpx.MockTransport(handler)) as ring:
        with pytest.raises(RingAPIError):
            ring.status("cam/../../users/me?override=yes#fragment")
    assert seen[0].url.query == b""
    assert b"%2F" in seen[0].url.raw_path and b"%3F" in seen[0].url.raw_path


def test_retry_preserves_media_headers_and_bounds_wait(monkeypatch):
    seen = []
    delays = []
    monkeypatch.setattr("ring_sandbox.client.time.sleep", delays.append)

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "NaN"})
        return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})

    with RingClient("secret", transport=httpx.MockTransport(handler)) as ring:
        ring.snapshot_at("camera", 1000)
    assert delays == [1]
    assert all(request.headers["Accept"] == "*/*" for request in seen)


def test_oversized_media_stops_streaming():
    consumed = []

    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            for i in range(20):
                consumed.append(i)
                yield b"x" * 1024

    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, stream=Chunks(), headers={"Content-Type": "image/png"})
    )
    with RingClient("secret", max_media_bytes=2048, transport=transport) as ring:
        with pytest.raises(ValueError):
            ring.snapshot_at("camera", 1000)
    assert len(consumed) == 3


def test_error_messages_do_not_echo_response_secrets():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(403, json={"errors": [{"detail": "private-query-token"}]})
    )
    with RingClient("secret", transport=transport) as ring:
        with pytest.raises(RingAPIError) as caught:
            ring.devices()
    assert "private-query-token" not in str(caught.value)


def test_invalid_media_inputs_fail_before_network():
    with RingClient(
        "secret", transport=httpx.MockTransport(lambda _: pytest.fail("network called"))
    ) as ring:
        for duration in (0, -1, True):
            with pytest.raises(ValueError):
                ring.clip("camera", 1000, duration)
        with pytest.raises(ValueError):
            ring.snapshot_at("camera", datetime(2026, 1, 1))
        with pytest.raises(ValueError):
            ring.snapshot_at("camera", 1000, width=10)


def test_freshness_window_bounds_replays():
    """meta.time lives inside the signed body, so a captured verbatim delivery
    has an authentic-but-stale timestamp — the window catches what a purged
    dedupe tombstone no longer can."""
    import json as _json
    from datetime import timedelta

    key = "k3y"
    payload = webhooks.build_event(event_type="button_press", device_id="cam")
    body = webhooks.encode(payload)
    sig = webhooks.sign(key, body)
    ev = webhooks.parse(body, signing_key=key, signature=sig, max_age_s=60)
    assert ev.request_id == payload["meta"]["request_id"]

    # Age the signed body: meta.time moves back — authenticity unchanged,
    # freshness is what separates "authentic" from "acceptable".
    old = _json.loads(body)
    old["meta"]["time"] = "2001-01-01T00:00:00Z"
    stale = webhooks.encode(old)
    stale_sig = webhooks.sign(key, stale)
    with pytest.raises(webhooks.SignatureError, match="freshness"):
        webhooks.parse(stale, signing_key=key, signature=stale_sig, max_age_s=60)
    # The same stale body still verifies without a window — the inbox worker
    # re-verifies stored deliveries for authenticity, not freshness.
    assert (
        webhooks.parse(stale, signing_key=key, signature=stale_sig).request_id
        == (payload["meta"]["request_id"])
    )

    # max_age without verification is a config error, not a silent skip
    with pytest.raises(ValueError):
        webhooks.parse(body, max_age_s=60)

    # future-dated meta.time beyond skew is rejected the same way
    future = _json.loads(body)
    future["meta"]["time"] = (
        (datetime.now(UTC) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    )
    fbody = webhooks.encode(future)
    with pytest.raises(webhooks.SignatureError, match="freshness"):
        webhooks.parse(fbody, signing_key=key, signature=webhooks.sign(key, fbody), max_age_s=60)


def test_non_ascii_signature_is_rejected_without_crashing():
    assert not webhooks.verify("key", b"body", "sha256=" + "\u00e9" * 64)
