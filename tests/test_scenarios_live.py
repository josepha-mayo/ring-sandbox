"""Socket-level test: emulator + scenario replay + signed webhook delivery to a local receiver."""

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
import uvicorn

from ring_sandbox import RingClient, webhooks
from ring_sandbox.emulator import create_app
from ring_sandbox.scenarios import BUILTIN, run
from ring_sandbox.world import default_world

KEY = "test-hmac-key"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Receiver(BaseHTTPRequestHandler):
    received: list[tuple[bytes, str | None]] = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        _Receiver.received.append((body, self.headers.get(webhooks.SIGNATURE_HEADER)))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_):  # silence
        pass


@pytest.fixture(scope="module")
def stack():
    api_port, hook_port = _free_port(), _free_port()
    world = default_world()
    server = uvicorn.Server(
        uvicorn.Config(create_app(world), host="127.0.0.1", port=api_port, log_level="warning")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    hooks = HTTPServer(("127.0.0.1", hook_port), _Receiver)
    threading.Thread(target=hooks.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{api_port}"
    for _ in range(50):
        try:
            httpx.get(base + "/_sandbox/health", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    httpx.post(
        base + "/_sandbox/webhooks",
        json={"url": f"http://127.0.0.1:{hook_port}/hook", "signing_key": KEY},
    )
    yield base, world
    server.should_exit = True
    hooks.shutdown()


def test_delivery_scenario_end_to_end(stack):
    base, world = stack
    payloads = run(BUILTIN["delivery"], base, speed=1000)
    assert len(payloads) == 5

    deadline = time.time() + 5
    while len(_Receiver.received) < 5 and time.time() < deadline:
        time.sleep(0.05)
    assert len(_Receiver.received) == 5

    types = []
    for raw, sig in _Receiver.received:
        ev = webhooks.parse(raw, signing_key=KEY, signature=sig)  # raises on bad signature
        types.append((ev.event_type, ev.sub_type))
    assert ("motion_detected", "package") in types
    assert ("button_press", None) in types

    with RingClient("any", base_url=base) as ring:
        cam = next(b for b in ring.devices(include=["capabilities"]) if b.capabilities.is_camera)
        hist = list(ring.events(cam.id))
        assert {h.attributes.event_type for h in hist} == {"motion", "ding"}
        pkg = next(ring.events(cam.id, event_types=["motion.package"]))
        snap = ring.snapshot_at(cam.id, pkg.attributes.start + 1000)
        assert snap.content.startswith(b"\x89PNG")


def test_backdated_visit_lands_in_history(stack):
    base, world = stack
    httpx.post(base + "/_sandbox/reset")
    run(BUILTIN["home_aide_visit"], base, backdate=True, deliver=False)
    with RingClient("any", base_url=base) as ring:
        bundles = ring.devices(include=["capabilities", "status"])
        cam = next(b for b in bundles if b.capabilities.is_camera)
        sensor = next(b for b in bundles if b.name == "Front Door Sensor")
        hist = list(ring.events(cam.id))
        assert len(hist) == 3  # human, ding, human
        span_s = (hist[0].attributes.start - hist[-1].attributes.start) / 1000
        assert 90 * 60 <= span_s <= 90 * 60 + 30
        assert sensor.status.attributes.contact_detection.faulted is False  # door closed at the end


def test_shipped_examples_parse_and_sort():
    """Every example YAML in the repo is a valid scenario — they ship as docs."""
    from pathlib import Path

    from ring_sandbox.scenarios import load_yaml

    examples = sorted(Path(__file__).resolve().parent.parent.glob("examples/*.yml"))
    assert len(examples) >= 3
    for path in examples:
        scenario = load_yaml(str(path))
        assert scenario.name == path.stem
        assert scenario.steps, path
        assert all(s.offset_s >= 0 for s in scenario.steps)
