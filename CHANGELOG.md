# Changelog

## 0.2.0 — unreleased

### Added

- **Chaos fault injection** — `ring-sandbox serve --chaos` injects duplicated,
  dropped, and delayed webhook deliveries plus flaky Event History / media
  responses. `GET|POST /_sandbox/chaos` inspects and adjusts the fault stream
  live. Every injected fault is recorded for assertions.
- **Scenario DSL** — load YAML visit scenarios (`ring-sandbox scenario FILE`,
  used by Attest's `attest replay path/to/scenario.yml`) with validated
  devices, event sequences, and clock control. Ships documented examples:
  `late_arrival.yml`, `partial_blackout.yml`, `visitor_not_worker.yml`.
- **OAuth refresh grant** — RFC 6749 `refresh_token` flow: on 401 the client
  refreshes once and retries, persisting rotated tokens. Exercised against the
  emulator; production endpoints unverified.
- **CI** — GitHub Actions matrix: Windows + Linux, Python 3.11 + 3.14.

### Fixed

- Media redirect handling hardened: JSON endpoints never follow redirects;
  media redirects only reach same-origin or allowlisted HTTPS hosts, never
  carry credentials, and response bodies are byte-capped.
- Event History mirrors live-observed behavior: Playground only surfaces
  `on_demand` events, and the emulator now matches.

## 0.1.0 — 2026-09-15

First public release on PyPI.

- Typed Python client for the Ring Partner API: users/me, devices, Event
  History, media download, webhook registration.
- Offline emulator (`ring-sandbox serve`) implementing the same surfaces with
  signed webhook delivery to the consumer's endpoint.
- pytest plugin with in-process `ring_client` / `ring_server` / `ring_control`
  fixtures.
- `ring-sandbox record` captures real Playground responses into sanitized JSON
  fixtures — several real recordings included under `fixtures/`.
