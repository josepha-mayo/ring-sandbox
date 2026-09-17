# ring-sandbox

Typed Python client **and** offline emulator for the [Ring Partner API](https://developer.amazon.com/docs/ring/api-documentation.html) (`api.amazonvision.com`).

Ring ships no SDK and no local simulator. Testing a partner integration today means a real device, a 30-minute Playground token, or hand-rolled mocks. `ring-sandbox` gives you:

- **`RingClient`** – a small, typed, synchronous client over `httpx` covering users, device discovery (with `?include=` side-loading), status, capabilities, configurations, location, event history (auto-pagination), image snapshots (303 redirect flow), media clips (200/206/416 semantics), and chime playback.
- **Webhook helpers** – HMAC-SHA256 `X-Signature` signing/verification over raw bytes, v1.1 payload construction, and parsing into a `WebhookEvent`.
- **Emulator** – a FastAPI app that speaks the same JSON:API shapes at `/v1/...`, plus a `/_sandbox` control plane to inject events, register webhook targets (the emulator signs and delivers them), add devices (doorbells, cameras, chimes, Early Access sensors), and reset.
- **Scenarios** – scripted event sequences (`delivery`, `home_aide_visit`, `short_visit`, `no_show`, `device_flap`, or your own YAML), replayable in real time, time-compressed, or back-dated into history.
- **pytest plugin** – `ring_client`, `ring_control`, `ring_world` fixtures that run the emulator in-process with no sockets.
- **Recorder** – `ring-sandbox record --token ...` snapshots real Playground/production responses into JSON fixtures.

## Install

```bash
pip install "ring-sandbox[server]"      # client + emulator + CLI
pip install ring-sandbox                # client only

# development: pinned, CI-tested dependency set
pip install -r requirements-dev.txt -e ".[dev]"
```

## Client

```python
from ring_sandbox import RingClient

with RingClient(token) as ring:                       # production by default
    for d in ring.devices(include=["status", "capabilities"]):
        print(d.name, d.online, d.capabilities.is_camera)

    cam = next(d for d in ring.devices(include=["capabilities"]) if d.capabilities.is_camera)
    for ev in ring.events(cam.id, event_types=["motion.human", "ding"]):
        print(ev.attributes.started_at, ev.attributes.event_type)

    snap = ring.snapshot_latest(cam.id, start=some_datetime)   # follows the 303 to the pre-signed URL
    clip = ring.clip(cam.id, timestamp=some_datetime, duration_ms=10_000)
    if clip.partial: print("only", clip.actual_length_ms, "ms available")
```

Media endpoints 303-redirect to a pre-signed URL on another host. Redirects are followed
manually: bearer credentials and cookies are never forwarded, the response body is capped by
`max_media_bytes`, and off-origin targets must be allowlisted — pass
`media_origins=["https://media-host.example", "*.amazonaws.com"]` when talking to real Ring.
JSON endpoints never follow redirects; device ids are treated as opaque single path segments.

Point it at the emulator with `RingClient(token, base_url="http://127.0.0.1:8787")`.

## Emulator

```bash
ring-sandbox serve --port 8787
ring-sandbox webhook http://localhost:8000/webhooks/ring --key my-hmac-key
ring-sandbox play delivery --speed 5           # courier: vehicle -> human -> ding -> package -> vehicle
ring-sandbox play home_aide_visit --backdate   # 90-minute visit written straight into history
ring-sandbox play examples/late_arrival.yml    # your own scenario: name, description, steps
ring-sandbox inject --type motion_detected --sub-type human
```

Custom scenarios are plain YAML — `steps` entries take `offset_s`, `type`, optional `sub_type`, `device` (id or name), and `duration_ms`. See `examples/late_arrival.yml` for a documented file.

Interactive docs at `http://127.0.0.1:8787/_sandbox/docs`. Drop real `default.jpg` / `default.mp4` (or `<device_id>.jpg`) in a folder and pass `--media-dir` to serve real media instead of placeholders.

### Fidelity notes

The emulator reproduces the parts of the API that bite integrators:

| Behaviour | Emulated |
|---|---|
| JSON:API compound documents via `?include=` | yes |
| History newest-first, `page[key]` cursor, dotted `event_types` filters | yes |
| `is_third_party_reviewed` flips after media access | yes |
| Snapshot `303 See Other` to a pre-signed URL | yes |
| Clip `206 Partial` + `X-Media-Length`, `416 TIMESTAMP_NOT_FOUND` when idle | yes |
| Chime playback restricted to the app's two audio slots | yes |
| Sensor `faulted` semantics, `255` battery sentinel on mains devices | yes |
| Webhook v1.1 payloads with `sub_type`, `component_ids`, HMAC `X-Signature` | yes |
| OAuth / account linking / nonce flow | no (use any bearer token, or `--token` to pin one) |
| WHEP / RTSP live video | no |

## Webhooks

```python
from ring_sandbox import webhooks

@app.post("/webhooks/ring")
async def ring_hook(request: Request):
    raw = await request.body()                                    # raw bytes, never re-serialized JSON
    ev = webhooks.parse(raw, signing_key=KEY, signature=request.headers.get("X-Signature"))
    if ev.event_type == "motion_detected" and ev.sub_type == "human": ...
```

## Chaos fault injection

`ring-sandbox serve --chaos storm` runs the emulator with a fault-injection profile:
webhook deliveries can be duplicated, dropped, or delayed with jitter, and history/media
endpoints can return transient 500s. Presets: `delivery` (dup/drop/delay only), `flaky`
(endpoint failures only), `storm` (both). A custom profile is a key=value list:
`--chaos drop=0.2,duplicate=0.4,jitter_ms=1500`. `--chaos-seed N` makes the fault stream
deterministic for reproducible runs.

Every injected fault is recorded — `GET /_sandbox/chaos` returns the active profile plus
the actions taken so far (`webhook.dropped`, `webhook.duplicated`, `webhook.delayed` with
the applied ms). `POST /_sandbox/chaos` adjusts rates live (`{"drop": 0.5}`) without
restarting. The point is proving the *receiver*: a correct consumer dedupes re-delivered
`request_id`s, tolerates out-of-order arrival, and keeps working through flaky polls.

## pytest

```python
def test_visit_detection(ring_client, ring_control):
    ring_control.post("/_sandbox/events", json={"type": "motion_detected", "sub_type": "human"})
    cam = ring_client.devices()[0]
    assert next(ring_client.events(cam.id)).attributes.event_type == "motion"
```

## Status

Built during the Amazon Developer Hackathon 2026. Response shapes follow the public documentation; where the docs are ambiguous the emulator follows what the Playground returns (see `fixtures/`). Sensors and chimes are Early Access upstream and may change.

MIT licensed.
