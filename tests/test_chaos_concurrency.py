"""Seeded chaos under concurrent delivery, and knowing when the queue is empty.

`Chaos` promises that "``seed`` makes the fault stream deterministic so chaos
runs are reproducible in tests". `test_seeded_fault_streams_are_reproducible`
checks that two fresh `Chaos.rng()` objects yield the same sequence — which is
a property of `random.Random`, not of the emulator. Nothing covered a run.

It does not hold for a run, because deliveries are concurrent.
`POST /_sandbox/events` ends in `asyncio.create_task(deliver(payload))`, and
`deliver()` awaits `asyncio.sleep(delay)` in the middle, between two draws from
a single `rng` shared by every delivery. Which coroutine draws next therefore
depends on the event loop's scheduling, so the same seed produces a different
fault stream from one run to the next as soon as more than one event is in
flight.

The second test is the practical consequence for a consumer. Nothing reports
in-flight deliveries: `create_task` results are not tracked, and the
`delivery` preset holds each delivery for up to `delay_ms + jitter_ms` (1.8 s).
A batch run cannot tell when the queue has drained, so it waits a fixed number
of seconds and hopes. On a loaded machine that guess is short, and the run
reports fewer deliveries than were made — a measurement that moves with the
load of the machine rather than with the code under test.

Found while running 20 doors at once against the `delivery` preset: the same
seed gave 8 drops on one run and 9 on the next, and a batch reported 58 of 60
receivers notified where a longer wait gave 60.
"""

import socket
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import uvicorn

from ring_sandbox.emulator import create_app
from ring_sandbox.world import Chaos, default_world

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
    raise RuntimeError("emulator did not start")


def _run(seed: int, events: int = 12) -> list[str]:
    """One chaos run: N events injected at once, and the fault stream it gave."""
    app = create_app(
        default_world(), Chaos(seed=seed, drop=0.3, duplicate=0.3, delay_ms=10, jitter_ms=60)
    )
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

    # At once, which is the point: one at a time would never interleave.
    with ThreadPoolExecutor(max_workers=events) as pool:
        list(
            pool.map(
                lambda _: httpx.post(base + "/_sandbox/events", json={"type": "motion_detected"}),
                range(events),
            )
        )
    time.sleep(2)

    actions = httpx.get(base + "/_sandbox/chaos").json()["actions"]
    hooks.shutdown()
    # Counts, not identities. `meta.request_id` is a fresh uuid4, so a delivery
    # has no name that survives a restart and "which one was dropped" cannot be
    # compared across runs — an earlier version of this test tried, and could
    # never have passed. How MANY were dropped is the part a measurement rests
    # on, and it is the part the seed can honestly promise.
    return Counter(a["kind"] for a in actions)


def test_a_seeded_run_is_reproducible_under_concurrent_delivery():
    """The promise in `Chaos`'s own docstring, applied to a run rather than to
    `random.Random`."""
    assert _run(seed=7) == _run(seed=7)


def test_the_seed_still_decides_something():
    """The guard on the guard: a fix that made every seed behave alike would
    pass the test above and quietly destroy the point of seeding. Several
    seeds, because two of them landing on the same counts is a coincidence and
    not a failure."""
    comptes = [_run(seed=s) for s in (7, 23, 99)]

    assert len({tuple(sorted(c.items())) for c in comptes}) > 1


def test_the_queue_says_when_it_is_empty():
    """So a batch run can wait for the thing itself instead of sleeping for a
    guessed number of seconds."""
    app = create_app(default_world(), Chaos(seed=3, delay_ms=200, jitter_ms=400))
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

    for _ in range(6):
        httpx.post(base + "/_sandbox/events", json={"type": "motion_detected"})

    assert httpx.get(base + "/_sandbox/chaos").json()["pending"] > 0

    for _ in range(60):
        if httpx.get(base + "/_sandbox/chaos").json()["pending"] == 0:
            break
        time.sleep(0.1)

    assert httpx.get(base + "/_sandbox/chaos").json()["pending"] == 0
    assert len(_Receiver.received) == 6, "nothing was still in flight when it said zero"
    hooks.shutdown()
