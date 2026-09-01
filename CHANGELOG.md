# Changelog

Format: what changed and why it mattered. Versions follow meaning, not
a schedule.

## 0.3.0 — 2026-08-27

The version about making the numbers in the reports match what would
actually have happened.

### Bugs found

- **Curve progress was counted together with the virtual reserve.** The
  monitor took `vSolInBondingCurve` whole, and from birth the token
  already holds 30 virtual SOL. The "curve filled under 40%" filter
  actually cut everything that had raised more than ~4 real SOL instead
  of 34 — eight times stricter than intended. The candidate stream was
  frozen for no good reason.
- **A buy at an unknown price** created a position with
  `entry_price = 0`: no exit rule fires on such a position, and it
  would have hung open forever. Now this is a refused trade.
- **The order-ceiling formula for price impact** was derived wrong
  (it used `1/(1−x)` instead of `1+x`) and understated the result.
  Derived again; a test checks it matches the target to 1e-9.
- **The Windows signal handler** captured the loop variable: both
  signals reported the same one.

### Execution became honest

- `src/curve.py`: constant product of virtual reserves, fee, slippage,
  own-order impact, round-trip cost, restoring reserves from the spot
  price.
- Entry price in the log is the average fill price, not the quote.
- Tradability cutoffs: a thin curve, an expensive round trip, a
  position-size ceiling from liquidity.

### Position management

- Four exit rules with priority: stop-loss, take-profit (can be
  partial), trailing from the peak, hold-time limit. The peak survives
  a restart.
- A token moving to Raydium is a separate exit reason: the curve is
  gone, and rules that compute from it would go blind at the
  position's best moment.
- A total-exposure ceiling: three positions at the cap are one large
  bet.
- A position without quotes for several polls in a row is marked
  blind: an error in the log, `degraded` on `/healthz`, a notification.

### Decisions

- Creator memory (`src/reputation.py`): an address whose token already
  collapsed is cut before a single Grok call. Built from our own
  closed trades, not from outside lists.
- Market pulse (`src/market.py`): the timing agent receives measured
  observations instead of the pipeline's internal counters.
- Prompt versions are written into the buy log: without them, weight
  tuning compares decisions of different bots as if they were one.

### Operations

- `grokbot doctor` — pre-flight environment check. The model is not
  called, tokens are not spent.
- `grokbot` — one command: `run`, `check`, `doctor`, `replay`,
  `dashboard`, `tune`, `curve`.
- Notifications to an external webhook (`src/alerts.py`), off by
  default.
- `intent` in the log before the order is sent: a process death
  between execution and bookkeeping no longer leaves an invisible
  position.
- A log write does not raise: a full disk must not leave open
  positions unwatched.
- `scripts/tune.py` — pick weights and a threshold from our own log,
  with the limitation printed in the open: what would have become of
  filtered tokens, the log does not know.

### Tests

- Curve invariants instead of examples.
- A trading-day simulation that checks money conservation after every
  step.

## 0.2.0 — 2026-08-26

- State survives a restart: positions, daily limits, Grok spend.
- Clean shutdown on SIGTERM, `/healthz` and `/metrics`, heartbeat.
- Three Grok-spend limiters: rate, daily budget, circuit breaker.
- Secrets as `SecretStr`, environment variables beat the file, config
  validation before start.
- JSONL rotation, bounded monitor memory.
- CI on 3.11–3.13, Dockerfile, Makefile, RUNBOOK.

## 0.1.0 — 2026-08-26

First build from the spec: monitor, analyzer, four Grok agents,
scoring matrix, risk manager, dry-run, JSONL log, replay, dashboard.
Transaction execution left as a stub on purpose.
