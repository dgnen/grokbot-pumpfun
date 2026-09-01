# How we work here

This is trading code. A bug in it costs money, not time, so the rules
are stricter than usual.

## Before sending changes

```bash
make check      # ruff + mypy + pytest, the same thing CI runs
```

All three must be green. Tests do not go to the network — if an external
service is needed, that means `httpx.MockTransport`, not skipping the
test.

## What must be covered by a test

- any rule that decides whether to buy or not;
- any money arithmetic: position size, cost basis, PnL, fee;
- any failure that must lead to **refusing the trade**, not to a silent
  continue.

For curve math, write invariants, not examples: "the product of reserves
is preserved", "a round-trip without a fee is free", not "on these
numbers we got 0.0413".

## Pessimistic-reject rule

Any failure — a timeout, malformed JSON, an unreachable provider, an
unknown price — leads to a decision **not to buy**. Never the other way
around. If new code can finish in an undefined state, the defined
outcome must be inaction.

## Changing prompts

A prompt is part of the bot's behavior. When you change the text, bump
the agent's `version` (`src/agents/*.py`). Without that, statistics
before and after the edit collapse into one pile, and weight tuning
starts comparing two different bots.

## What is not accepted

- Implementing transaction submission in `LiveExecutor`. It is left as
  a stub on purpose: everyone writes that code themselves, for their
  own wallet and under their own responsibility.
- Defaults that make the bot more aggressive: larger size, lower
  threshold, weaker limits. The user will raise them if they decide to.
- Secrets in code, in tests, and in the example config.

## Style

Comments explain **why**, not what the line does. If the behavior is
non-obvious — describe the situation it saves you from. Those are the
comments already here.
