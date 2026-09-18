# Contributing

Contributions are welcome — this project exists because the Ring Partner API
has no official SDK or local simulator, and the more accurate the emulation,
the more useful it is to everyone building on the API.

## What helps most

- **Response-shape corrections.** If the Playground returns something different
  from what the emulator produces, open an issue with the real response
  (redact tokens, account IDs, and personal data) — or better, record it with
  `ring-sandbox record` and send the fixture.
- **Missing endpoints.** New API surfaces follow the pattern in
  `src/ring_sandbox/server/` — a route handler plus a typed client method.
- **Scenario files.** Interesting visit patterns as YAML DSL entries under
  `examples/` — see `late_arrival.yml` and `partial_blackout.yml`.
- **Chaos modes.** New fault classes for the chaos injector (reordering,
  clock skew, malformed payloads) that a correct receiver should survive.

## Ground rules

- **Never commit real credentials, tokens, or personal data.** Recorded
  fixtures must be sanitized — `ring-sandbox record` already strips auth
  headers; check account IDs and media URLs by eye before committing.
- Response shapes follow the public documentation; where docs are ambiguous,
  the emulator follows what the Playground actually returns (fixtures/ is the
  evidence trail). If you haven't verified a shape against the real API, say
  so in the PR — don't guess.
- Keep the package dependency-light: `httpx` for the client, `fastapi` only
  for the server extra. No new runtime dependency without a clear need.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements-dev.txt -e ".[dev,server]"
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\ruff check src tests
.\.venv\Scripts\ruff format --check src tests
```

CI runs the same on Windows + Linux, Python 3.11 + 3.14.

## Bug reports

Include the endpoint, the expected shape (docs link or Playground response),
and what the emulator returned. For feature requests, describe the API surface
or integration friction you're trying to simulate.
