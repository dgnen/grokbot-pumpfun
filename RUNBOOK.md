# Runbook

What to do with a running pipeline: how to start it, what to watch, what
each complaint means, and how to fix it. Architecture and the meaning of
the stages are in the [README](README.md); this file is operations only.

## Startup

### Bare metal

```bash
make dev                      # venv + dependencies + linter and tests
cp config.example.yaml config.yaml
$EDITOR config.yaml           # or keys via GROKBOT_* in the environment

grokbot doctor                # pre-flight check: keys, network, permissions, disk
grokbot run                   # start (mode comes from the config)
```

`grokbot doctor` is worth running before every start and after every
environment change. It does not spend model tokens: the Grok key is
checked via the model list, not a completion call.

### Docker

```bash
cp .env.example .env && $EDITOR .env
mkdir -p config logs state && cp config.example.yaml config/config.yaml
docker compose up -d
docker compose logs -f
```

The `logs/` and `state/` volumes must live outside the container: `state/`
holds open positions and daily limits, and losing that file means that
after a restart the pipeline forgets both.

### systemd

```ini
[Unit]
Description=grokbot-pumpfun
After=network-online.target

[Service]
User=grokbot
WorkingDirectory=/opt/grokbot-pumpfun
Environment=GROKBOT_GROK_API_KEY=xai-...
Environment=GROKBOT_HEALTH_PORT=8080
ExecStart=/opt/grokbot-pumpfun/.venv/bin/python -m src.cli run --config config.yaml
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=45            # larger than ops.shutdown_grace_seconds
[Install]
WantedBy=multi-user.target
```

### launchd (macOS)

`~/Library/LaunchAgents/com.grokbot.pumpfun.plist`, the important bits:
`ProgramArguments` — the same invocation, `KeepAlive` — true,
`EnvironmentVariables` — `GROKBOT_GROK_API_KEY`. Stopping via
`launchctl unload` sends SIGTERM, i.e. a clean shutdown that saves state.

## First day

The order that saves money:

1. `mode: dry-run`, a day of running, then `make replay`.
2. Watch conversion: if 0 of thousands were bought — the threshold is too
   high or the agents are rejecting; if more than a dozen an hour were
   bought — the threshold is too low.
3. In the skip-reason breakdown, check that every stage is working. If
   all skips land on one stage — the rest are not getting data.
4. In the component averages, look for an always-zero component: that is a
   silent agent, not a strict one.

Only after that is a conversation about `live` worth having.

## What to look at first

```bash
grokbot doctor                              # environment
grokbot replay logs/trades.jsonl            # funnel, exits, PnL
grokbot dashboard logs/trades.jsonl --watch 5
grokbot tune logs/trades.jsonl              # what other weights would have given
grokbot curve                               # what a trade actually costs
```

The funnel in `replay` answers the main operational question: **which
stage is actually making the decisions**. If ninety percent of the stream
dies at the monitor — the filter is too tight. If everything reaches the
checker and dies there — the scoring threshold is too low, and you are
paying for grok-4 for nothing.

## What to watch

```bash
curl -s localhost:8080/healthz | jq        # status
curl -s localhost:8080/metrics             # counters for Prometheus
python scripts/dashboard.py logs/trades.jsonl --watch 5
```

`/healthz` returns 200 on `status: ok` and 503 on `degraded` — you can
hang a restart policy on that. Fields:

| field | meaning | when it is bad |
|---|---|---|
| `status` | summary | `degraded` = circuit open or stream stalled |
| `stalled` | no socket events for over 10 minutes | `true` — the socket is dead |
| `breaker` | `closed` / `half-open` / `open` | `open` — Grok is not answering |
| `grok_budget_remaining` | remaining daily calls | 0 — until midnight UTC the agents stay silent |
| `halted` | daily loss limit is spent | `true` — no trading today |
| `blind_positions` | positions without quotes | more than 0 — exits on them do not work |
| `open_positions` | open positions | cannot exceed `max_open_positions` |
| `in_flight` | tokens in review | stuck at the ceiling — hit the Grok limit |

The `жив: ...` line in the log every `heartbeat_seconds` is the same
thing, but in the journal, so you can reconstruct history from it.

## Notifications

When `alerts.webhook_url` is set (prefer `GROKBOT_ALERT_WEBHOOK` — the
URL usually contains a token) events go to an external channel:
`started`, `stopped`, `buy`, `close`, `rug`, `breaker`, `halted`,
`stalled`. The set is configured in `alerts.events`.

States are reported **on transition**: `breaker` arrives once on open
and once on recovery, not every minute. The stream is capped by
`max_per_minute`; extras are dropped and counted in `alerts.dropped` in
`/healthz`, not queued.

Silence on the channel by itself means nothing — check liveness via
`/healthz`, not via the absence of messages. The `alerts.failed` counter
in `/healthz` is exactly how many notifications did not go out.

## Incidents

### `breaker: open`, log says «цепь Grok разомкнута»

That many consecutive calls failed. The pipeline stopped calling Grok
for `breaker_cooldown_seconds` and **does not buy for that entire window** —
every agent returns the pessimistic result, the checker answers with a
reject.

Check: the key is alive (`curl` to api.x.ai), the xAI balance has not
run out, there is no 429. The circuit will close itself after the
cooldown; a probe call will show whether it recovered.

### `grok_budget_remaining: 0`

`ops.max_grok_calls_per_day` is spent. This is the guard against a
launch spike eating a month of budget in an evening. Until midnight UTC
the agents are not called. If this is normal load — raise the ceiling;
if not — raise `filter.min_total_score` so fewer tokens reach the
agents.

### `stalled: true`

No events have come from the socket for over ten minutes. The monitor
reconnects on its own with a growing backoff; if `stalled` persists —
check `data.ws_url` and the network. Open positions are still under
stop-loss watch: it polls REST, not the socket.

### `blind_positions` greater than zero

For that many open positions, several polls in a row have not returned a
price. That means **exit rules on them are not working right now**: not
stop-loss, not take-profit, not trailing. Check the data provider
(`data.rest_url`), key limits, and the network. While there is no price,
the position lives on its own — this is the case where you should
intervene by hand.

### A position closed with reason `graduated`

The token moved to Raydium. That is good news and at the same time the
end of this project's math: the curve is gone, price no longer arrives
from there. The position is closed immediately, and dry-run proceeds
are computed from the spot without slippage — in the log this is an
estimate, not a quote. If this happens often, it is worth raising
`take_profit_pct`: the bot exits before the token gets there.

### An `intent` in the log with no following buy

The process died between sending the order and booking the position.
**The wallet may hold tokens the bot does not know about.** Check the
wallet by hand; if the position is there, you will have to close it
manually — restoring it into the bot's state by eye is not worth it,
the cost basis will be wrong anyway.

### `cooldown_left_seconds` greater than zero

A losing streak turned the pause on. This is a safeguard, not a failure: new
buys are not opened, open positions keep being driven by the exit
rules. The pause ends on its own. If it trips several times a day —
the problem is not the pause, it is selection: look at the funnel and
`tune`.

### «состояние занято другим процессом» (state held by another process)

A second bot was started on the same state file — it will refuse to
start. That is by design: two processes on one wallet will overwrite
each other's positions. Check that the old instance is actually stopped
(`grokbot doctor` will show whose PID holds the lock); a lock from a
dead process is taken over automatically.

### `halted: true`

The daily loss limit is spent. Nothing to fix; the counters reset at
midnight UTC. Open positions keep being driven by stop-loss.

### «состояние не читается — отложено в .corrupt» (state unreadable)

The state file was corrupted (usually the disk filled up mid-write).
The pipeline started from a clean slate: **it does not know about open
positions**. Open `state/pipeline.json.corrupt`, pull the position list
out of it, and deal with them by hand. Until that is done, stop-loss on
them does not work.

### Positions close by a different rule than expected

`make replay` prints close reasons. What they mean:

* almost everything in `max_hold` — the market is not giving a move, or
  `max_hold_seconds` is too small for the tokens being picked;
* almost everything in `trailing_stop` at a tiny plus — `trailing_stop_pct`
  is already ordinary memecoin noise, the pullback is caught on the
  first move;
* `take_profit` never fires — the threshold is higher than the selected
  tokens actually reach; look at the `pnl_pct` distribution on closed
  trades.

### Positions stayed open after shutdown

On a clean shutdown this is normal and a warning is written to the log:
the pipeline does not sell everything on the way out. Stop-loss on
these positions does not work until the process is up again. So a long
idle with open positions is a risk, not a pause.

### `executor_not_implemented` in the log

`mode: live` is on, but `LiveExecutor` is a stub by design. The token
passed all nine stages and was not bought. Either finish the executor
or go back to `dry-run`.

### Config rejected at startup

The message lists every problem at once. This is not nitpicking: each
of them is either a non-working setting (a zero limit) or an unsafe one
(live without a wallet key). `make check-config` shows the same thing
without starting trading.

## Updates

```bash
git pull
make check                # linter, types, tests — before the restart, not after
systemctl restart grokbot # or docker compose up -d --build
```

State survives a restart: positions are restored, daily counters and
Grok spend continue from the same place. The state format is versioned
(`version` in the file); on a version mismatch the daily counters are
reset and the positions are still read.

## Backup

Back up `state/pipeline.json` (positions are money) and `logs/*.jsonl`
(without them you cannot compute the result). Do not back up a config
with keys into shared storage — keys are easier to reissue.

## Checklist before going live

- [ ] a day in `dry-run` is done, `replay` has been reviewed
- [ ] `LiveExecutor.buy` and `.sell` are written and tested separately
- [ ] a separate wallet, with only an amount you can afford to lose
- [ ] `risk.*` rechecked against live numbers, not the defaults
- [ ] `state/` on a disk that will survive a machine restart
- [ ] `/healthz` wired into monitoring, an alert on 503 configured
- [ ] `--i-understand-the-risk` added to the unit file on purpose
