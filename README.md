# grokbot-pumpfun

[![CI](https://github.com/zostaff/grokbot-pumpfun/actions/workflows/ci.yml/badge.svg)](https://github.com/zostaff/grokbot-pumpfun/actions/workflows/ci.yml)

A memecoin trading pipeline on pump.fun: the stream of new launches goes
through nine stages, four of which are Grok API agents and the rest are
plain code. The point of the design is the order of the stages: cheap
filters come before expensive ones, and only a fraction of a percent of
the stream reaches the strong model.

**Trade execution is intentionally left as a stub.** Code that would send
transactions with your key is not generated here — see [Executor](#executor).
By default the project runs in `dry-run`.

## Architecture

```
                    stream of new pump.fun tokens
                                │
┌───────────────────────────────▼───────────────────────────────┐
│ 1. MONITOR            WebSocket, code filter                  │
│    ≥5 buyers · curve <40% · has metadata · >2 min             │
└───────────────────────────────┬───────────────────────────────┘
                    ~94% filtered │
┌───────────────────────────────▼───────────────────────────────┐
│ 1.5 CREATOR MEMORY    code, own log of closed trades          │
│    an address whose token already rugged goes no further      │
└───────────────────────────────┬───────────────────────────────┘
                                │
┌───────────────────────────────▼───────────────────────────────┐
│ 2. ANALYZER           REST ×3 in parallel (asyncio.gather)    │
│    top-5 · snipers · diversity · social signals · curve       │
│    cut off if risk_score > 7/10; unconditional veto if        │
│    creator holds ≥25% or top-5 hold ≥80%                      │
│    tradability: thin curve and expensive round-trip — skip    │
└───────────────────────────────┬───────────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
┌───────────────┐      ┌────────────────┐      ┌────────────────┐
│ 3. AUDITOR    │      │ 4. NARRATIVE   │      │ 5. TIMING      │
│ grok-4-fast   │      │ grok-4-fast    │      │ grok-4-fast    │
│ coordination, │      │ trend,         │      │ market as a    │
│ wash, dump,   │      │ virality,      │      │ whole, 15 min  │
│ organic       │      │ community      │      │ cache          │
└───────┬───────┘      └────────┬───────┘      └────────┬───────┘
        └───────────────────────┼───────────────────────┘
                                ▼
┌───────────────────────────────────────────────────────────────┐
│ 6. SCORING MATRIX     code, weights from config               │
│    audit·0.30 + narrative·0.25 + timing·0.15 + metrics·0.30   │
│    below min_total_score → skip with a reason                 │
└───────────────────────────────┬───────────────────────────────┘
                                │
┌───────────────────────────────▼───────────────────────────────┐
│ 7. CHECKER            grok-4, adversarial                     │
│    looks for reasons NOT to buy: contradictions, missed flags │
│    approve: false — a normal outcome; an error is also false  │
└───────────────────────────────┬───────────────────────────────┘
                                │
┌───────────────────────────────▼───────────────────────────────┐
│ 8. RISK GATE          code, six limits                        │
│    per-trade cap · daily loss · trades/day ·                  │
│    open positions · total exposure · curve liquidity          │
└───────────────────────────────┬───────────────────────────────┘
                                │
┌───────────────────────────────▼───────────────────────────────┐
│ 9. EXECUTION          from curve math: fee,                   │
│    slippage, own order's impact on price                      │
│    dry-run: tx_hash "dry_run" · live: a stub by design        │
└───────────────────────────────┬───────────────────────────────┘
                                │
┌───────────────────────────────▼───────────────────────────────┐
│ 10. EXITS             background task, priority top to bottom │
│    stop-loss · migrate to Raydium · take-profit (can be a     │
│    fraction) · trailing from peak · hold-time limit           │
└───────────────────────────────┬───────────────────────────────┘
                                ▼
              JSONL log: intent / buy / skip / close
```

## Structure

```
grokbot-pumpfun/
├── README.md
├── RUNBOOK.md                # operations: startup, incidents, checklists
├── pyproject.toml            # package, ruff, mypy, pytest
├── Makefile                  # make dev / check / run / replay
├── Dockerfile                # image with no keys inside
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
├── config.example.yaml       # template; config.yaml is in .gitignore
├── .env.example              # secrets for compose; .env is in .gitignore
├── .github/workflows/ci.yml  # ruff + mypy + pytest on 3.11-3.13 + image
├── src/
│   ├── cli.py                # grokbot run|check|doctor|replay|tune|curve
│   ├── pipeline.py           # orchestrator, lifecycle, entry point
│   ├── curve.py              # curve math: fee, slippage
│   ├── doctor.py             # pre-flight environment check
│   ├── market.py             # market pulse for the timing agent
│   ├── reputation.py         # memory of token creators
│   ├── alerts.py             # notifications to an external webhook
│   ├── models.py             # pydantic models, config, validation, secrets
│   ├── monitor.py            # WebSocket launch monitor (code)
│   ├── analyzer.py           # REST metrics analyzer (code)
│   ├── agents/
│   │   ├── base.py           # shared Grok call mechanics
│   │   ├── auditor.py        # agent 1: wallet audit
│   │   ├── narrative.py      # agent 2: meme potential
│   │   ├── timing.py         # agent 3: market moment (with cache)
│   │   └── checker.py        # agent 4: adversarial review
│   ├── scoring.py            # scoring matrix (code)
│   ├── risk.py               # risk manager and exit rules
│   ├── state.py              # state that survives a restart
│   ├── ops.py                # Grok limiters, metrics, health, heartbeat
│   ├── executor.py           # execution: dry-run works, live is a stub
│   └── log.py                # JSONL logging with rotation
├── tests/                    # pytest, does not go to the network
└── scripts/
    ├── replay.py             # log replay and statistics
    ├── dashboard.py          # CLI dashboard
    └── tune.py               # pick weights and threshold from the log
```

## Quick start

```bash
git clone https://github.com/zostaff/grokbot-pumpfun.git
cd grokbot-pumpfun

make dev                     # venv, dependencies, linter and tests
cp config.example.yaml config.yaml
$EDITOR config.yaml          # Grok key; the rest can stay as-is

grokbot doctor               # pre-flight check: keys, network, permissions
grokbot run                  # dry-run, the default mode
```

You do not have to put keys in the file at all — environment variables
take precedence:

```bash
export GROKBOT_GROK_API_KEY=xai-...
make run
```

In a container:

```bash
cp .env.example .env && $EDITOR .env
mkdir -p config logs state && cp config.example.yaml config/config.yaml
docker compose up -d && docker compose logs -f
```

What happens next: the pipeline subscribes to new launches and writes
`logs/trades.jsonl`. You can watch it live:

```bash
python scripts/dashboard.py logs/trades.jsonl --watch 5   # what is happening now
python scripts/replay.py   logs/trades.jsonl              # period summary
```

Tests:

```bash
pytest -v
```

## Agents

**Auditor** (`grok-4-fast`) gets the raw trade stream and the holder list —
not aggregates, but the transactions themselves. It looks for what gets
lost in averages: coordinated buys with identical amounts and intervals
under five seconds, wash trading, the creator splitting a position before
dumping, batch buys in the first second of the token's life. It returns
four boolean flags and the share of organic buyers. When data is
insufficient it must raise a flag, not give the token the benefit of the
doubt.

**Narrative** (`grok-4-fast`) does not look at on-chain data at all. Its
input is the name, ticker, description, image, and links; its question is
whether this will spread. Four independent scores from 0 to 1: fit with
the trend, virality, signs of a living community, timing of the launch.
Clones of yesterday's hype are scored strictly; missing data is a low
score, not a medium one.

**Timing** (`grok-4-fast`, 15-minute cache) scores the backdrop, not the
token: sentiment on Solana, whether a meme season is on, volumes on
pump.fun, anomalies such as a network outage or a cascade dump. The answer
is the same for every token inside the window, so it is cached — otherwise
every launch would pay for the same conclusion. The cache is lock-protected:
a burst of simultaneous tokens does not fire three identical parallel
requests. A failure is not cached.

**Checker** (`grok-4`, the stronger model) is the last line before money
and the only agent forbidden from looking for reasons to buy. It receives
every prior conclusion and looks for contradictions among them: high meme
potential with low organic share, a healthy curve with concentration in
the top-5, a strong score assembled from one component while the others
fail. `approve: false` is the expected outcome, not a failure.

### Shared rule for all four

All call mechanics live in `agents/base.py`: request assembly,
`temperature=0`, strict JSON parsing, retries with exponential backoff,
30-second timeout. Prompts live as constants in the agent modules and end
with a requirement to answer in bare JSON with no markdown wrapper.

**On any error the agent returns the most pessimistic result, not an
empty one.** A timeout, a 500, malformed JSON, a response that does not
match the schema — for the auditor that means every risk flag is `true`
and organic share is zero; for the checker that means `approve: false`.
A broken check equals a reject, never a silent pass. This is covered by
tests (`tests/test_agents.py`).

## Config

Everything lives in `config.yaml`; the template is `config.example.yaml`.
**`config.yaml` is in `.gitignore`**; only the template with placeholders
reaches the repository.

| section | what it sets |
|---|---|
| `mode` | `dry-run` or `live` |
| `grok` | key, `fast_model` for the three fast agents, `checker_model` for the checker, timeout, retries |
| `solana` | RPC, wallet private key, Jito: block-engine address and tip size |
| `data` | provider key, REST and WS addresses |
| `risk` | five limits: per-trade cap, daily loss, trades per day, open positions, stop-loss |
| `filter` | base-filter thresholds and `min_total_score` |
| `scoring` | weights of the four components and the timing cache TTL |
| `logging` | JSONL path and level |

Scoring weights are normalized: if you write 0.5/0.5/0.5/0.5, the
proportions are kept and the result stays in the 0..1 range.

### Risk management

Position size is proportional to the score, but capped twice: by the
`max_sol_per_trade` ceiling and by 30% of the remaining daily loss budget.
So as the day goes into the red, stakes shrink automatically, and once
`daily_loss_limit_sol` is hit the pipeline stops until the next UTC day.

### Creator memory

The pipeline reviews every launch from a clean slate, so the same deployer
can dump us three times in a row — and each time they would be "new".
The auditor would not recognize them either: it sees one token, not the
address's history.

`src/reputation.py` keeps a book of addresses from **our own closed
trades** — this is not a list from the internet and not a heuristic, but
a fact from our own log. A close worse than
`rug_loss_pct` counts as a rug for that address; after
`block_creator_after_rugs` rugs, their tokens are cut at the door, before
a single Grok call. Separately, `one_position_per_creator` applies: two
tokens from the same deployer are one bet, not two — they usually dump
together.

The book lives in `state/creators.json` and survives a restart. Clean
addresses are forgotten after `forget_creators_after_days`; addresses with
rugs never are: that is the book's value.

### Position exits

Open positions are driven by a separate background task that polls prices
every `stop_loss_poll_seconds`. There are four rules, and the order among
them is the priority order:

| rule | when it fires | why |
|---|---|---|
| `stop_loss` | price is below entry by `stop_loss_pct` | cap the loss |
| `take_profit` | price is above entry by `take_profit_pct` | take the profit |
| `trailing_stop` | pullback from the peak by `trailing_stop_pct` | do not give back what already grew |
| `max_hold` | in the position longer than `max_hold_seconds` | a memecoin that did not move in an hour will not move |

Trailing is computed only above the entry price: below it, the stop-loss
owns the position, otherwise the two rules would fight over the same
drawdown. The price peak is stored in state and survives a restart —
otherwise after a restart the trail would start over from the current
price. Zero in any of the three newer parameters turns that rule off;
with `stop_loss_pct` alone the behavior is exactly what it used to be.

The exit reason lands in `close.reason` in the log and in the
`exit_<reason>` counter in metrics — from those you can see how positions
actually end: taken profit, pullback, or timer.

### Dry-run mode

Default is `mode: dry-run`. The pipeline walks every stage, actually
calls the agents, and computes the score, but instead of a transaction
it writes a record with `tx_hash: "dry_run"`. Prices are real, so
stop-loss and PnL in dry-run are computed from the market.

Switching to live is only by an explicit config edit, and at startup the
pipeline prints a warning and requires a flag:

```bash
python -m src.pipeline --config config.yaml --i-understand-the-risk
```

Without the flag a `live` start is rejected.

## Honest trade economics

The pipeline used to assume it buys at the quoted price. That is a lie
three times over: the venue takes a fee, the buy moves the price against
the buyer, and on the way out the same thing happens in reverse. On a
curve with a few dozen SOL of reserve, a half-SOL order is a noticeable
share of liquidity, not a speck of dust.

`src/curve.py` computes from the constant product of virtual reserves:

| what | how it is computed |
|---|---|
| entry price | average fill price of the order, not the spot |
| fee | `market.trade_fee_pct` on each side |
| price impact | exact formula, not a fit: `(1+s/S)/(1−f)` |
| order ceiling | max SOL that fits inside `max_price_impact_pct` |
| round-trip cost | entry plus an immediate exit — the threshold of whether a trade makes sense |

The practical point in one line: **the dry-run is what decides whether to
turn live on**, and if dry-run buys at the quote, that decision is made
on a profit that does not exist.

The same source gives three cutoffs that did not exist before: a curve
that is too thin (`min_curve_liquidity_sol` — you cannot get out, your
own sell will crash the price), a round-trip that is too expensive
(`max_round_trip_cost_pct` — the move the trade was for is eaten by
costs), and a position-size ceiling from liquidity (an order that moves
the price by percents is buying from itself).

To see the numbers on a fresh curve: `grokbot curve`.

## Unattended operation

The pipeline is built to run for days. Here is what that required.

**State survives a restart.** Open positions, daily counters, and Grok
spend live in `state/pipeline.json` and are loaded at startup. Without
this a restart would reset the daily loss limit and the guard against
buying the same token again — both limiters would start from scratch.
The file is written atomically (a temp file plus `os.replace`), so a
half-written JSON never sits on disk. Positions are always restored;
daily counters only if the file is from today.

**Shutdown is clean.** SIGTERM and SIGINT do not tear work apart: the
pipeline stops accepting new tokens, lets whatever is already in review
finish (up to `shutdown_grace_seconds`), saves state, and closes
connections. Positions are not sold off — and a warning is written to
the log that stop-loss on them does not work until the process is up
again.

**Grok spend is limited in three different ways**, because their failure
modes differ: a token bucket stops hammering the API faster than agreed,
a daily call budget stops a launch spike from eating a month of money in
an evening, and a circuit breaker opens after `breaker_failures`
consecutive failures and stops calling a place that is not answering
anyway. While the circuit is open, agents return the pessimistic result
— so the pipeline does not buy.

**Important events are reported outward.** When `alerts.webhook_url` is
set, the pipeline sends events to the webhook: start and stop, a buy, a
position close, a creator rug, an open circuit, the daily limit hit, a
stalled launch stream. States are reported on transition, not on every
tick; the stream is rate-limited; and any send error stays in the log
and does not touch trading. Off by default.

**A pause after a losing streak.** The daily limit catches a slow bleed,
but not a fast series: three stops in a row usually mean a hostile
regime, not bad luck. `cooldown_after_losses` stops new buys for
`cooldown_minutes`; a profitable trade resets the counter. The pause
survives a restart — otherwise you could "cure" it by restarting.

**One bot per one state.** Two processes on the same state file are two
bots on the same wallet: they will overwrite each other's positions,
hit the daily limit twice, and buy the same token. A PID lock keeps the
second from starting; a lock from a dead process is taken over, because
a crash must not leave the system unstartable.

**Exposure is limited in total, not per trade.** Three positions at the
cap are one large bet, not three small ones: memecoins fall together.
`risk.max_total_exposure_sol` caps the sum in the market; the size of a
new trade is cut by the free remainder, and a partial take-profit
returns it.

**A move to Raydium does not leave the position blind.** When the curve
ends, all of this project's math no longer applies to the token: price
stops arriving, and the exit rules would go blind at the exact moment
the position is at its best profit. That is a separate exit reason,
immediately after stop-loss.

**A buy-intent trail.** An `intent` record is written before the order is
sent. If the process dies between execution and bookkeeping, the disk
keeps an intent without a buy — on the next start this is printed with
a demand to check the wallet, not discovered a week later via leftover
tokens.

**Liveness is visible from outside.** When `ops.health_port` is greater
than zero, `GET /healthz` comes up (JSON, 200 on `ok` and 503 on
`degraded` — you can hang a restart policy on it) and `GET /metrics` in
Prometheus format. Every `heartbeat_seconds` the same thing goes to the
log as a line. No web framework was introduced for this: two handlers
on `asyncio.start_server`.

**Nothing grows without bound.** JSONL rotates by size
(`logging.max_bytes`, `backups`); the monitor buffer and the list of
already-seen mints are length-capped.

**Secrets do not leak into logs.** Keys are `SecretStr`: they are in
neither `repr`, nor the model dump, nor the traceback. `--check` prints
the config with keys masked. The image contains no secrets at all: they
arrive via `GROKBOT_*`; the config is mounted as a volume.

**A bad config does not start.** A zero trade limit, a threshold out of
range, `live` without a wallet key, a leftover placeholder instead of a
Grok key — all of these are startup errors with a list of problems, not
a surprise an hour into trading. Warnings that do not block startup are
printed separately.

What to watch in production, what each complaint means, and how to fix
it — [RUNBOOK.md](RUNBOOK.md).

### Environment variables

| variable | what it sets |
|---|---|
| `GROKBOT_GROK_API_KEY` | xAI key |
| `GROKBOT_DATA_API_KEY` | data-provider key |
| `GROKBOT_WALLET_PRIVATE_KEY` | wallet private key (needed only in live) |
| `GROKBOT_MODE` | `dry-run` or `live` |
| `GROKBOT_RPC_URL` | Solana RPC |
| `GROKBOT_LOG_PATH`, `GROKBOT_LOG_LEVEL` | log path and level |
| `GROKBOT_STATE_PATH` | state file |
| `GROKBOT_HEALTH_PORT` | health-endpoint port, 0 to disable |
| `GROKBOT_ALERT_WEBHOOK` | webhook for notifications (usually contains a token) |

An empty variable value does not wipe what is in the file: that is a
common compose mistake.

## Executor

`src/executor.py` is the only place left unfinished on purpose.
`DryRunExecutor` works fully; `LiveExecutor.buy` and `.sell` raise
`NotImplementedError`, and next to them sits a step-by-step list of what
still needs to be written: Keypair load, bonding-curve accounts, buyer
ATA, `max_sol_cost` with slippage, the pump.fun program instruction,
ComputeBudget, sending a bundle to Jito with a tip, waiting for
confirmation.

Reading the price from the curve is shared by both modes and works.

## Logging

JSONL, one record per line, three types:

- `buy` — the full decision context: decomposed score, replies from all
  four agents, metrics, entry price, size, `tx_hash`;
- `skip` — stage, reason, and detail (for example, which scoring
  component was the weakest);
- `close` — exit price, PnL in SOL and percent, hold time, reason.

`scripts/replay.py` builds a summary from the log: how many were
reviewed and bought, skip-reason breakdown by stage, score histogram
and component averages, PnL, win rate, average hold time.
`scripts/dashboard.py` shows the current state: open positions, today's
funnel, latest events; with `--watch N` it refreshes itself.

`scripts/tune.py` recomputes scoring from the log with different weights
and a different threshold — the components are stored in every record,
so the agents do not need to be called again. It shows how the threshold
changes the number of candidates, and which weight sets would have kept
more profit on trades that already happened.

A limitation the script prints itself: what would have become of a token
filtered by the threshold, the log does not know and cannot know. The
table describes what already happened, not future income, and on a couple
of dozen trades this is fitting to noise, not tuning. Hence the warning
when the sample is under 30 closed trades.

## Pre-flight check

```bash
grokbot doctor              # or: make doctor
grokbot doctor --offline    # without network checks
grokbot doctor --json       # for monitoring
```

Half of failed starts are not about logic, but about the environment: a
key is expired, the provider returns 403, the socket does not open from
this network, the state directory is not writable. That is discovered
after an hour of silent idle work, and could have been found in ten
seconds before start.

Checked: config, permissions on the log and state directories, free
space, curve constants, the Grok key (via the model list — **the model
is not called, tokens are not spent**), the data provider, the launch
stream on the socket, Solana RPC, and live-mode readiness. A quiet night
on the socket is different from a broken socket: the first is a note,
the second is a reject.

## Tests and checks

```bash
make check      # ruff + mypy + pytest, the same thing CI runs
make test
make cov
```

Covered: the base filter and the monitor buffer, the scoring matrix at
boundary values and weight normalization, all five risk limits, position
shrink near the daily-budget edge, day rollover, stop-loss, agents on
mocked HTTP — including malformed JSON, a timeout, a 500, and a
schema-invalid reply — config validation and secret masking, state after
a restart, Grok-spend limiters, the health endpoint, log rotation, and
an end-to-end pipeline run in dry-run. No test goes to the network,
except the health endpoint on 127.0.0.1.

Two kinds of tests that usually do not exist stand on their own:

**Curve invariants** (`tests/test_curve.py`) — not "on these numbers we
got this much", but constant-product identities: the product of reserves
is preserved, a round-trip without a fee is free, a round-trip with a
fee costs exactly `1−(1−f)²`, splitting an order gives exactly the same
as one large one, the impact ceiling matches the target to the ninth
digit.

**Trading-day simulation** (`tests/test_simulation.py`) — dozens of
tokens with moving prices through the real pipeline, with a check after
every step: no ceiling is exceeded, the sum of PnL from the log equals
what the risk manager booked, partial exits neither create nor lose SOL.
Such discrepancies are invisible on a single trade — they accumulate
over a day.

CI runs the linter, types, and tests on Python 3.11, 3.12, and 3.13,
builds the image, and separately checks that `config.example.yaml` with
placeholders does not start.

## Disclaimer

This is research code, not a trading product and not financial advice.

Memecoins on a bonding curve go to zero completely, and that is the
usual outcome, not a rare one. A large share of pump.fun launches are
organized rugs; some of the rest become them. Neither a wallet audit nor
an adversarial check reliably tells a prepared dump from organic growth
— they only lower the share of obviously bad entries.

The five limits in the config cap the speed of losing money, not the
probability of losing it. Automated trading on your own key means that a
bug in the code, a data-provider outage, or a bad prompt costs exactly
as much as sits in the wallet.

Work in `dry-run` until you have read every stage yourself. The live
part is unfinished on purpose: by finishing it, you accept
responsibility for what it does with your funds.
