# yt-crypto-signals

Monitors YouTube crypto channels, extracts **explicit** token price predictions from
transcripts using Claude, verifies them against the transcript, cross-checks them across
channels, scores them against real prices, and alerts over Telegram.

A signal tracker for personal analysis. **It does not execute trades** and holds no
exchange credentials.

> Build status: checkpoint 1 of 7 (skeleton, config, models, migrations, CLI stubs).
> The full VPS deployment guide, systemd units and troubleshooting arrive at checkpoint 7.

## How it works

Four channels are polled every 30 minutes. New videos move through a state machine, one
stage at a time, each stage a short-lived process driven by a systemd timer:

```
discovered → transcribed → extracted → verified → enriched → crosschecked → alert_queued → done
```

Three properties are load-bearing:

- **No hallucinated predictions.** Every extracted quote must fuzzy-match the actual
  transcript within ±90 s of its claimed timestamp, or the call is rejected and logged.
  The guard is deterministic — no second LLM pass.
- **No duplicate or lost alerts.** Alerts go through a transactional outbox. The row is
  inserted in the same transaction as the state change, and `dedupe_key` is a UNIQUE
  index, so duplicate suppression is a database constraint rather than application logic.
- **Raw data is append-only.** Scores, cross-checks and re-extractions are written to
  separate tables, so an improved prompt can be re-run against full history.

## Local development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"

cp .env.example .env     # then fill in your keys
.venv/bin/ycs config-check   # validates .env + config.yaml, prints a redacted summary
.venv/bin/ycs db upgrade     # apply migrations
.venv/bin/ycs stats
```

Checks:

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check src tests
.venv/bin/mypy
.venv/bin/alembic check      # fails if models and migrations have drifted
```

## Adding a channel

One line in `config.yaml`; the handle is resolved to a channel ID once and cached:

```yaml
channels:
  - handle: "@SomeCryptoChannel"
    default_host: "Their Name"     # labels calls attributed to the unnamed host
```

## Configuration

- `.env` — secrets only (API keys, proxy credentials, Telegram token). Never committed.
  See `.env.example`.
- `config.yaml` — everything else: channels, thresholds, retry ladders, token prices,
  chunking. Committed, and validated at startup with unknown keys rejected so a typo
  surfaces immediately.

Secrets never reach the logs: the log formatter redacts proxy userinfo and API-token
shapes, and pydantic is configured with `hide_input_in_errors` so a failed startup
cannot print the values it was given.

## Conventions

- **All stored datetimes are timezone-aware UTC** (SQLModel's `UTCDateTime`).
  `DISPLAY_TZ` is applied only when formatting output.
- **Every stage is idempotent** and selects only rows in its own input state, so a crash
  mid-run loses at most the in-flight batch.
- **Every network call has a timeout** and a jittered tenacity retry policy.
