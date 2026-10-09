# yt-crypto-signals

Monitors YouTube crypto channels, extracts **explicit** token price predictions from
transcripts using Claude, verifies each one against the transcript, cross-checks it
against other channels, scores it against real prices, and alerts over Telegram.

A signal tracker for personal analysis. **It does not execute trades**, holds no exchange
credentials, and exposes no network listener.

---

## How it works

Four channels are polled every 30 minutes. New videos move through a state machine, one
stage at a time, each stage a short-lived process run by a systemd timer:

```
discovered → transcribed → extracted → verified → enriched → crosschecked → alert_queued → done
     │            ↑                        │
     │     transcript_failed               └──→ skipped  (short / no captions / still
     │      (backoff retry)                                live / no verified calls)
     │            ↓
     └──→  permanent_failed + health alert
```

Four properties carry the weight:

**No hallucinated predictions.** Every extracted quote must fuzzy-match the actual
transcript within ±90 s of its claimed timestamp, or the call is rejected and the reason
recorded. The guard is deterministic — no second LLM pass. On top of that, a target
wildly out of line with the live price is dropped as a caption artefact (speech-to-text
writes "nine hundred" as "$900,000"), keeping the direction but discarding the number.

**No duplicate or lost alerts.** Alerts go through a transactional outbox. The row is
inserted in the same transaction as the state change, and `dedupe_key` is a UNIQUE index,
so duplicate suppression is a database constraint rather than application logic. Running
the sender twice sends once.

**Nothing is lost on a crash.** Each stage reads only rows in its own input state and
commits per batch, so an interrupted run leaves at most one batch to be redone.

**Raw data is append-only.** Scores, cross-checks and re-extractions live in separate
tables, and every extraction records its prompt version and model, so an improved prompt
can be re-run against full history and compared (`ycs reprocess`).

---

## VPS setup from zero

Tested on Ubuntu 24.04. Everything runs as a dedicated unprivileged user under hardened
systemd units; there is no daemon, no web server, and no Redis.

```bash
# 1. As root, fetch the code
sudo apt update && sudo apt install -y git curl
sudo git clone <your-repo-url> /opt/ycs
cd /opt/ycs

# 2. Run the installer: creates the `ycs` user, builds the venv from the
#    lockfile, applies migrations, installs and enables the timers
sudo ./deploy/install.sh

# 3. Fill in your keys
sudo -u ycs nano /opt/ycs/.env
```

Then validate, in this order — each step proves one dependency before the next relies
on it:

```bash
cd /opt/ycs
R="sudo -u ycs /opt/ycs/.venv/bin/ycs"

$R config-check                      # .env + config.yaml valid? (prints a redacted summary)
$R test-proxy                        # THE RISKY ONE: can this IP get transcripts at all?
$R test-telegram --find-chat-id      # lists chats that have messaged your bot
$R test-telegram                      # full outbox round-trip
$R backfill --days 30                # one summary message, not 30 days of alerts
```

Watch it:

```bash
systemctl list-timers 'ycs-*'
journalctl -u ycs-pipeline -f           # structured JSON, one object per line
journalctl -u ycs-pipeline -o cat | jq 'select(.level=="ERROR")'
```

### Getting each API key

| Key | Where |
|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) → API Keys |
| `YOUTUBE_API_KEY` | [Google Cloud Console](https://console.cloud.google.com) → create a project → APIs & Services → Library → enable **YouTube Data API v3** → Credentials → Create credentials → API key. Restrict it to the YouTube Data API. |
| `TELEGRAM_BOT_TOKEN` | Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow prompts |
| `TELEGRAM_CHAT_ID` | **Message your new bot first** (or add it to a group and post), then run `ycs test-telegram --find-chat-id`. Group ids are negative. This is the one value that cannot be derived from the token — someone must message the bot before it exists. |
| `COINGECKO_API_KEY` | [coingecko.com/en/api](https://www.coingecko.com/en/api) → **Demo** plan (free). Sent as the `x-cg-demo-api-key` header. |
| `WEBSHARE_PROXY_*` | [webshare.io](https://www.webshare.io) → purchase a **Residential** package → [Proxy settings](https://dashboard.webshare.io/proxy/settings) → copy *Proxy Username* and *Proxy Password*. **Not** "Proxy Server" and **not** "Static Residential" — those are datacenter ranges and YouTube blocks them. |

`HEALTHCHECK_URL` is optional: a [healthchecks.io](https://healthchecks.io) ping URL,
called after each successful run. It catches the one failure the health stage cannot —
the whole host being down, when nothing local runs to notice.

### Installing the PO-token provider

Only needed if `ycs test-proxy` still reports `blocked` with a residential proxy
configured. It has two halves:

```bash
# The server (Node, so it runs in a container bound to localhost)
cd /opt/ycs && sudo docker compose up -d

# The yt-dlp plugin that talks to it
sudo -u ycs /opt/ycs/.venv/bin/pip install bgutil-ytdlp-pot-provider

sudo -u ycs /opt/ycs/.venv/bin/ycs test-proxy   # should now print "PO-token provider: detected"
```

The provider is detected by import, not assumed: yt-dlp silently ignores
`extractor_args` for a plugin that is not installed, which would otherwise make a
misconfigured sidecar look like a YouTube-side block.

**Never add cookies alongside it.** The yt-dlp provider has no cookie path at all, which
makes that unrepresentable rather than merely discouraged.

---

## Adding a channel

One line in `config.yaml`. The handle is resolved to a channel ID and uploads playlist
once, then cached in the database.

```yaml
channels:
  - handle: "@SomeCryptoChannel"
    default_host: "Their Name"     # labels calls the extractor attributes to the host
```

Then `sudo systemctl start ycs-pipeline` (or wait for the next tick). `default_host`
exists because on a panel show the extractor mostly emits `Host`/`Guest`, which would
make the speaker leaderboard useless; named guests are still recorded under their own
names when the transcript identifies them.

---

## Commands

```
ycs run [--stages discover,transcribe]   full pipeline once (what the 30-min timer calls)
ycs backfill --days 30                   backfill history, one summary message
ycs process --video <url> --dry-run      extract + verify one video, print JSON, send nothing
ycs reprocess --prompt-version v2 [--since 2026-09-01] [--limit N]
ycs score                                score calls whose timeframe has elapsed
ycs scorecard [--send]                   weekly channel/speaker league table
ycs health [--no-notify]                 stuck stages, dead proxy, low quota
ycs stats                                row counts and pipeline state distribution
ycs config-check                         validate configuration (redacted summary)
ycs test-proxy [--video <url>]           which transcript providers work from this host
ycs test-telegram [--find-chat-id]       bot token, chat id, outbox round-trip
ycs outbox-send [--retry-failed]         flush alerts; requeue after fixing config
ycs db upgrade | current | backup
```

`ycs process --dry-run` is the inspection path and the most useful command for tuning:
it prints the verified calls *and* the rejected ones with reasons, including whether a
rejected quote appears elsewhere in the transcript — which distinguishes "the model
moved a real quote" from "the model invented one".

---

## Configuration

- **`.env`** — secrets only. Never committed; see `.env.example`.
- **`config.yaml`** — everything else: channels, thresholds, retry ladders, token prices,
  chunking. Committed, validated at startup, and **unknown keys are rejected** so a typo
  surfaces immediately instead of being silently ignored.

Secrets never reach the logs. The formatter redacts proxy userinfo and API-token shapes
at the logging boundary, falls back to replacing literal values from the environment, and
pydantic runs with `hide_input_in_errors` so a failed startup cannot print the values it
was handed.

---

## Costs

| Item | Scale |
|---|---|
| Claude (`claude-haiku-4-5`) | ~$0.015–0.03 per video; ~$2–4/month at 6 videos/day |
| YouTube Data API | ~400 of 10,000 daily quota units (`search.list` is never used — it costs 100 units/call) |
| CoinGecko | ~500–1500 calls/month against the demo tier's 10k |
| Webshare residential | the only fixed cost, if your IP is blocked |

Extraction cost is recorded per run (`extraction_run.cost_usd`) from prices in
`config.yaml`, and reported month-to-date in the weekly scorecard.

---

## Troubleshooting blocked transcripts

`ycs test-proxy` is the diagnostic. It probes every provider independently and prints the
classification for each, so one provider succeeding never masks another failing.

| Symptom | Cause | Fix |
|---|---|---|
| `blocked` on every provider | YouTube blocks the VPS IP range | Set `PROXY_PROVIDER=webshare` with **Residential** credentials |
| `blocked` *with* a residential proxy | Session needs a PO token | Install the provider (above) |
| `no_captions` | The video genuinely has none in your languages | Expected on some uploads; the video is skipped, not retried |
| `unavailable` | Private, removed, age-restricted, or still live | Expected; skipped without retry |
| `transient` on everything | Network, DNS or a dead proxy endpoint | Check egress; `journalctl -u ycs-pipeline` |
| Captions missing right after a stream | YouTube generates them late | By design: videos wait `livestream_settle_hours` (2 h) after `actualEndTime` |

Only `blocked` and `transient` are retried. `no_captions` and `unavailable` are dead
ends, and retrying them would only burn metered proxy bandwidth.

If a video exhausts the retry ladder it becomes `permanent_failed` and the health stage
reports it. To retry after fixing the cause:

```bash
sqlite3 /opt/ycs/data/ycs.db \
  "update video set state='discovered', attempts=0, next_retry_at=null
   where state='permanent_failed';"
```

### Alerts not arriving

```bash
ycs stats                          # are videos reaching alert_queued?
ycs outbox-send                    # flush manually and read the error
ycs outbox-send --retry-failed     # after fixing TELEGRAM_CHAT_ID
```

A 403 from Telegram (wrong chat id, bot removed from the group) is treated as
non-retryable so it cannot loop — but it is a *configuration* error, so
`--retry-failed` requeues those rows once you have fixed it rather than losing them.

---

## Local development

### Coin leaderboard and target tracker

```bash
uv sync --locked --extra dev
.venv/bin/ycs db upgrade
.venv/bin/ycs mentions-backfill        # scan saved transcripts; calls Claude, downloads no videos
.venv/bin/ycs dashboard               # open http://127.0.0.1:8501
```

The local dashboard has a coin leaderboard, a spreadsheet-style target timeline,
individual predictions, linked source evidence, and observed price charts. Filter
by video publication date, channel, ticker, and sponsored content. It reloads saved
data every minute and exports the selected view as an Excel workbook.

Mentions count **distinct videos per coin**, whether or not a prediction exists.
The leaderboard shows scan coverage; predictions from unscanned transcripts are
included until a full mention scan is available. Evidence quotes and coin names
are checked against the original transcript. Only completed scans are marked done,
so errors can be retried; changed transcripts are rescanned. Sponsored mentions are
excluded by default. Tickers are normalized, but ambiguous symbol mappings do not
receive a guessed current price.

Timeline date columns show **when a prediction was stated**, in `DISPLAY_TZ`.
Every target remains separate with its currency, channel and timeframe. Deadlines
are estimates from the stated timeframe. "Reached" requires recorded price-window
evaluation; current prices alone do not prove that a historical target was reached.
The graph uses cached observations, not a complete intraday price series. Blank
current prices mean a mapping or quote is unavailable; quote timestamps show age.

```bash
.venv/bin/ycs run                     # scan new transcripts, score due calls, snapshot prices
                                     # and queue one tracker summary + workbook per local day
.venv/bin/ycs report                  # export data/reports/coin-tracker.xlsx
.venv/bin/ycs report --refresh-prices --send
.venv/bin/ycs report --send --force   # explicitly send an additional report today
```

Reports use the existing Telegram outbox for retries and deduplication. The daily
systemd maintenance unit also sends a report after scoring. On a local Mac, running
the dashboard does not schedule pipeline work: run `ycs run` to fetch/process new
videos. Keep `data/reports/` files while document deliveries are pending.

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
cp .env.example .env            # fill in keys; PROXY_PROVIDER=none is fine locally
.venv/bin/ycs db upgrade
```

Checks:

```bash
.venv/bin/python -m pytest          # ~210 tests, all external APIs mocked
.venv/bin/ruff check . && .venv/bin/ruff format --check src tests
.venv/bin/mypy                      # strict
.venv/bin/alembic check             # fails if models and migrations have drifted
```

### Conventions

- **All stored datetimes are timezone-aware UTC** (SQLModel's `UTCDateTime`, which
  refuses naive values). `DISPLAY_TZ` is applied only when formatting output.
- **Enums are `StrEnum`** and persist their *values*. A plain `(str, Enum)` mixin
  interpolates as `Direction.UP` inside an f-string, which would reach the alert text.
- **Every stage is idempotent** and touches only rows in its own input state.
- **Every network call has a timeout** and a jittered `tenacity` retry policy.
- **Library APIs are checked against the installed version**, never recalled. All
  versions are pinned in `pyproject.toml` and locked in `uv.lock`.

### Layout

```
src/yt_crypto_signals/
  config.py  models.py  db.py  enums.py  logging.py  locking.py  httpc.py  utils.py  cli.py
  sources/      youtube_api.py, youtube_rss.py (fallback), transcripts/{ytapi,ytdlp,whisper}
  llm/          chunking, merge, schemas (wire + strict), client, extract, prompts/
  verify/       quotes.py  — the deterministic guard
  prices/       coingecko.py
  notify/       format.py (pure), telegram.py (transport), outbox.py (queueing)
  pipeline/     one module per stage + runner.py (framework) + orchestrate.py (sequencing)
```
