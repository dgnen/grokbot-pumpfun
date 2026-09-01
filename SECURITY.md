# Security

## What this code holds

A Solana wallet private key, a Grok API key, a data-provider key, and
a notification webhook. Any one of them is enough to spend someone
else's money — the wallet directly, the rest through a usage bill.

## How they are stored

- All four are `SecretStr`: they do not appear in `repr`, the model
  dump, the traceback, or `grokbot check`.
- The preferred way to pass them is `GROKBOT_*` environment variables,
  not a file. The Docker image contains no keys at all.
- `config.yaml` and `.env` are in `.gitignore`. Only templates with
  placeholders reach the repository, and CI separately checks that a
  template with placeholders fails config validation.

## What you should do on your side

- A separate wallet only for the bot, with an amount you can afford to
  lose.
- `state/` and `logs/` on a disk strangers cannot read: state holds
  open positions, the log holds the full decision history.
- `health_port` listens on `127.0.0.1` by default. If you expose it,
  lock it down: `/healthz` shows open positions and PnL.
- A Grok key with a spend limit on the xAI side. `ops` limits spend
  from the bot's side, but that is not a substitute for the provider
  limit.

## If you found a vulnerability

Do not open a public issue with details. Write to the repository owner
and give reasonable time to fix it.

## What this project does not promise

There is no protection against a malicious config: whoever can edit
`config.yaml` or the process environment can make the bot trade any
way they want. Access control on the machine is on you.
