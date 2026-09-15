"""``ring-sandbox`` command line.

ring-sandbox serve [--port 8787] [--media-dir ./media] [--token secret]
ring-sandbox play delivery [--speed 10] [--backdate]
ring-sandbox inject --type motion_detected --sub-type human
ring-sandbox webhook http://localhost:8000/webhooks/ring --key <hmac>
ring-sandbox record --token <playground token> --out fixtures/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

DEFAULT_URL = "http://127.0.0.1:8787"


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .emulator import create_app
    from .world import default_world

    world = default_world()
    world.required_token = args.token
    if args.media_dir:
        world.media_dir = Path(args.media_dir)
    uvicorn.run(create_app(world), host=args.host, port=args.port, log_level="info")


def _play(args: argparse.Namespace) -> None:
    from .scenarios import BUILTIN, load_yaml, run

    scenario = (
        load_yaml(args.scenario)
        if args.scenario.endswith((".yml", ".yaml"))
        else BUILTIN[args.scenario]
    )
    events = run(
        scenario, args.url, speed=args.speed, backdate=args.backdate, deliver=not args.no_deliver
    )
    for ev in events:
        d = ev["data"]
        print(
            f"{ev['meta']['time']}  {d['type']:<24} "
            f"{d['attributes'].get('sub_type') or '':<10} {d['attributes']['source']}"
        )


def _inject(args: argparse.Namespace) -> None:
    body = {"device_id": args.device, "type": args.type, "sub_type": args.sub_type}
    resp = httpx.post(f"{args.url}/_sandbox/events", json=body, timeout=10)
    resp.raise_for_status()
    print(json.dumps(resp.json()["webhook"], indent=2))


def _webhook(args: argparse.Namespace) -> None:
    resp = httpx.post(
        f"{args.url}/_sandbox/webhooks",
        json={"url": args.target, "signing_key": args.key},
        timeout=10,
    )
    resp.raise_for_status()
    print(json.dumps(resp.json(), indent=2))


def _record(args: argparse.Namespace) -> None:
    """Snapshot real API responses (Playground or production) into JSON fixtures."""
    from .client import PRODUCTION_BASE_URL, RingAPIError, RingClient

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with RingClient(args.token, base_url=args.base_url or PRODUCTION_BASE_URL) as ring:
        raw_devices = ring._get_json(
            "/v1/devices", {"include": "status,capabilities,configurations,location"}
        )
        (out / "devices.json").write_text(json.dumps(raw_devices, indent=2))
        print(f"wrote devices.json ({len(raw_devices.get('data', []))} devices)")
        try:
            (out / "me.json").write_text(json.dumps(ring._get_json("/v1/users/me"), indent=2))
            print("wrote me.json")
        except RingAPIError as exc:
            print(f"users/me: {exc}")
        for dev in raw_devices.get("data", []):
            did = dev["id"]
            try:
                hist = ring._get_json(f"/v1/history/devices/{did}/events")
                (out / f"history.{did}.json").write_text(json.dumps(hist, indent=2))
                print(
                    f"wrote history for {dev['attributes'].get('name')} "
                    f"({len(hist.get('data', []))} events)"
                )
            except RingAPIError as exc:
                print(f"history {did}: {exc}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="ring-sandbox",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the emulator")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument(
        "--token", help="require this bearer token (default: accept any non-empty token)"
    )
    s.add_argument(
        "--media-dir",
        help="directory of <device_id>.jpg/.mp4 or default.jpg/.mp4 to serve as media",
    )
    s.set_defaults(fn=_serve)

    s = sub.add_parser("play", help="replay a built-in or YAML scenario")
    s.add_argument("scenario")
    s.add_argument("--url", default=DEFAULT_URL)
    s.add_argument("--speed", type=float, default=1.0)
    s.add_argument(
        "--backdate", action="store_true", help="write the scenario into the past instantly"
    )
    s.add_argument("--no-deliver", action="store_true")
    s.set_defaults(fn=_play)

    s = sub.add_parser("inject", help="inject one event")
    s.add_argument("--url", default=DEFAULT_URL)
    s.add_argument("--device")
    s.add_argument("--type", default="motion_detected")
    s.add_argument("--sub-type")
    s.set_defaults(fn=_inject)

    s = sub.add_parser("webhook", help="register a webhook target")
    s.add_argument("target")
    s.add_argument("--url", default=DEFAULT_URL)
    s.add_argument("--key", required=True, help="HMAC signing key the emulator should sign with")
    s.set_defaults(fn=_webhook)

    s = sub.add_parser("record", help="record real API responses into fixtures")
    s.add_argument("--token", required=True)
    s.add_argument("--base-url")
    s.add_argument("--out", default="fixtures")
    s.set_defaults(fn=_record)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except httpx.HTTPStatusError as exc:
        print(f"error: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
