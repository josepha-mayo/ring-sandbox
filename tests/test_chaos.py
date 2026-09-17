"""Chaos fault injection: deterministic (seeded) drops/duplicates/delays and flaky endpoints."""

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import uvicorn

from ring_sandbox.emulator import create_app
from ring_sandbox.world import CHAOS_PRESETS, Chaos, default_world

KEY = "chaos-hmac-key"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Receiver(BaseHTTPRequestHandler):
    received: list[bytes] = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        _Receiver.received.append(self.rfile.read(n))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_):
        pass


def _serve(app, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            httpx.get(base + "/_sandbox/health", timeout=0.5)
            return server
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


def test_chaos_endpoint_reports_profile_and_actions():
    app = create_app(default_world(), Chaos(duplicate=0.5, drop=0.25, seed=3))
    port = _free_port()
    _serve(app, port)
    base = f"http://127.0.0.1:{port}"
    profile = httpx.get(base + "/_sandbox/chaos").json()["profile"]
    assert profile["duplicate"] == 0.5 and profile["drop"] == 0.25 and profile["seed"] == 3
    updated = httpx.post(base + "/_sandbox/chaos", json={"drop": 0.9}).json()["profile"]
    assert updated["drop"] == 0.9 and updated["duplicate"] == 0.5


def test_chaos_endpoint_requires_chaos_enabled():
    app = create_app(default_world())
    port = _free_port()
    _serve(app, port)
    resp = httpx.post(f"http://127.0.0.1:{port}/_sandbox/chaos", json={"drop": 0.5})
    assert resp.status_code == 400


def test_flaky_history_deterministic_at_rate_one():
    app = create_app(default_world(), Chaos(flaky_history=1.0))
    port = _free_port()
    _serve(app, port)
    base = f"http://127.0.0.1:{port}"
    world = app.state.world
    cam = world.cameras()[0]
    resp = httpx.get(
        f"{base}/v1/history/devices/{cam.id}/events",
        headers={"Authorization": "Bearer sandbox-token"},
    )
    assert resp.status_code == 500
    assert resp.json()["errors"][0]["code"] == "chaos"


def test_drop_all_webhooks_receives_nothing_but_records_actions():
    app = create_app(default_world(), Chaos(drop=1.0, seed=1))
    api_port, hook_port = _free_port(), _free_port()
    _serve(app, api_port)
    base = f"http://127.0.0.1:{api_port}"
    _Receiver.received = []
    hooks = HTTPServer(("127.0.0.1", hook_port), _Receiver)
    threading.Thread(target=hooks.serve_forever, daemon=True).start()
    httpx.post(
        base + "/_sandbox/webhooks",
        json={"url": f"http://127.0.0.1:{hook_port}/hook", "signing_key": KEY},
    )
    httpx.post(base + "/_sandbox/events", json={"type": "motion_detected"})
    httpx.post(base + "/_sandbox/events", json={"type": "motion_detected"})
    time.sleep(0.8)
    assert _Receiver.received == []
    actions = httpx.get(base + "/_sandbox/chaos").json()["actions"]
    dropped = [a for a in actions if a["kind"] == "webhook.dropped"]
    assert len(dropped) == 2
    hooks.shutdown()


def test_duplicate_all_webhooks_delivers_extra_copies():
    app = create_app(default_world(), Chaos(duplicate=1.0, seed=1))
    api_port, hook_port = _free_port(), _free_port()
    _serve(app, api_port)
    base = f"http://127.0.0.1:{api_port}"
    _Receiver.received = []
    hooks = HTTPServer(("127.0.0.1", hook_port), _Receiver)
    threading.Thread(target=hooks.serve_forever, daemon=True).start()
    httpx.post(
        base + "/_sandbox/webhooks",
        json={"url": f"http://127.0.0.1:{hook_port}/hook", "signing_key": KEY},
    )
    httpx.post(base + "/_sandbox/events", json={"type": "motion_detected"})
    for _ in range(50):
        if len(_Receiver.received) >= 3:
            break
        time.sleep(0.1)
    # duplicate=1.0 means both extra-copy draws succeed: 1 original + 2 copies
    assert len(_Receiver.received) == 3
    assert len({b for b in _Receiver.received}) == 1  # identical bodies — same request_id
    actions = httpx.get(base + "/_sandbox/chaos").json()["actions"]
    assert sum(a["kind"] == "webhook.duplicated" for a in actions) == 2
    hooks.shutdown()


def test_seeded_fault_streams_are_reproducible():
    a, b = Chaos(seed=42, drop=0.5).rng(), Chaos(seed=42, drop=0.5).rng()
    assert [a.random() for _ in range(10)] == [b.random() for _ in range(10)]


def test_presets_not_mutated_by_serve_overrides():
    before = CHAOS_PRESETS["storm"].drop
    app = create_app(default_world(), CHAOS_PRESETS["storm"])
    app.state.chaos.drop = 0.99  # simulate live POST /_sandbox/chaos
    assert CHAOS_PRESETS["storm"].drop == before
