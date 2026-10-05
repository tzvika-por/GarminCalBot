# GarminCalBot

GarminCalBot is a small, single-user Python Telegram bot that reads daily calorie data from Garmin Connect. It can report total, active, and resting calories on demand, ask for a device sync when the data cannot be verified as fresh, and run an evening reminder and check. This repository contains source and examples, with no production credentials, logs, state, or health data.

This is an unofficial independent project. It is not affiliated with, endorsed by, or supported by Garmin or Telegram. Garmin Connect is an unofficial integration surface and may change. Confirm the calorie fields against your own account before relying on the values.

## Commands

Send one of these messages to the configured Telegram chat:

| Command | Behavior |
|---|---|
| `calories` | Fetches today's total, active, and resting calories. The reply explicitly says that sync freshness was not verified for this on-demand request. |
| `status` | Shows sync-wait status, last verified time, timezone, and schedule. |
| `Done` | Checks for a newer device sync timestamp, or changed same-day calorie data relative to a stored baseline. It reports updated calories only when one of those checks establishes freshness. Otherwise it acknowledges the request and retries briefly in the background. |

The Hebrew equivalents `קלוריות`, `סטטוס`, and `בוצע` are also accepted. Commands are case-insensitive and accepted only from `TELEGRAM_CHAT_ID`.

Example with **synthetic** values:

```text
Garmin Calories
Date: 2026-01-02
Total: 2100 kcal
Active: 500 kcal
Resting: 1600 kcal
```

Resting calories use Garmin's BMR/resting field when available. If exactly one field is missing and the other two are present, the bot derives it arithmetically. An unavailable field is shown as `N/A`.

## Sync and scheduled behavior

A sync is considered fresh if Garmin exposes a plausible device sync timestamp within the past 60 minutes. After a `Done` response to a prompt, that timestamp must also be newer than the prompt. If usable metadata is unavailable, the bot compares today's calorie signature with an earlier same-day baseline. The first observation without metadata creates a baseline but **does not prove a sync**. Unchanged data remains unverified; changed data is treated as evidence of a new cloud update. This fallback cannot prove the physical device synchronized if the values changed for another reason.

In `BOT_TIMEZONE`, the bot checks at **22:30** whether a sync reminder is needed and at **23:05** whether it can send today's calorie summary. If the process is busy or restarts after either target time, it catches up later on the **same calendar day**. Failed actions remain pending and retry after a delay. Successful actions run at most once per day. It does not backfill prior days. A `Done` retry does not block Telegram polling for four minutes; it makes up to eight spaced checks.

Telegram does not offer an idempotency key for `sendMessage`. The bot writes a delivery intent before sending and records success afterward. Confirmed deliveries carry their non-secret state effect (such as a SHA-256 calorie signature, date, or prompt timestamp) so restart can complete that effect without resending. If a send times out or a process dies in the narrow interval where the outcome cannot be known, the delivery is marked uncertain and **not automatically repeated**. This avoids an ordinary duplicate but requires an operator to inspect the chat and resolve that delivery. See [Operations](docs/OPERATIONS.md).

## Requirements and installation

- Python **3.13** (tested locally on 3.13; prepared CI checks 3.13).
- A Garmin Connect account and your own Telegram bot/chat.
- Internet access during actual operation. Tests use fake services and need no credentials.

```sh
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip check
```

`requirements.txt` names the exact direct dependencies. `requirements.lock` pins the full dependency set resolved for this project. Recreate the lock deliberately if changing Python/platform or dependency versions; the lock does not include wheel hashes and has been verified locally only on macOS/Python 3.13.

## Private configuration

Copy [.env.example](.env.example) to a **private location outside this repository**, replace every placeholder with your own values, and restrict access to that file. The program reads environment variables; it does not parse `.env` files by itself. Never commit, paste, or log real values.

| Variable | Purpose |
|---|---|
| `GARMIN_EMAIL` | Garmin Connect account email. |
| `GARMIN_PASSWORD` | Garmin Connect account password. |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token. |
| `TELEGRAM_CHAT_ID` | One allowed destination chat and command source. |
| `BOT_TIMEZONE` | IANA timezone for the evening schedule; default `UTC`. Set it explicitly for your locale. |
| `BOT_STATE_DIR` | Private state/log directory; default `~/.local/state/garmin-cal-bot`. |

For a local run, export the variables through your shell or a private environment file:

```sh
set -a
. /ABSOLUTE/PRIVATE/PATH/bot.env
set +a
python garmin_cal_bot.py
```

The bot makes no Telegram startup announcement. It begins polling and performs due same-day scheduled work. Stop it with Ctrl-C or SIGTERM. Do not run two pollers against the same Telegram bot token; Telegram may return polling conflicts.

## macOS launchd

The [example plist](deploy/macos/com.example.garmin-cal-bot.plist.example) and [example wrapper](deploy/macos/run-bot.sh.example) use placeholders. They are examples for a **user LaunchAgent**, not an installed service. Replace all paths, copy the wrapper to a private location, then run `chmod 700 /ABSOLUTE/PRIVATE/PATH/run-bot.sh` so launchd can execute it directly. Keep the private environment file outside the repository and owner-readable only. Point the plist to the private wrapper. The example directs launchd stdout/stderr to `/dev/null`; the application writes its own private bounded log. Do not install the example unchanged, and do not run it alongside another instance using the same bot token. See [Operations](docs/OPERATIONS.md) for setup and recovery notes.

## State, logs, security, and privacy

State is human-readable JSON at `BOT_STATE_DIR/state.json`. Writes use a same-directory temporary file, flush, atomic replace, and owner-only (`0600`) permissions. The state directory is `0700`. State contains dates, update offsets, hashed calorie signatures, retry/delivery status and recovery metadata, and sync timestamps; it contains no configured credentials or raw calorie totals. Confirmed delivery effects are reconciled on startup. A corrupt state file is logged and defaults are loaded in memory. Back up state privately if you need recovery of offsets after external file damage.

Application logging is redacted, stored at `BOT_STATE_DIR/bot.log`, and capped at roughly 1 MB plus three rotated backups. It records event categories and exception types, not raw exception strings, Telegram API URLs, credentials, or calorie values. Error messages sent to the chat are short and do not include exception details. Keep the state directory and any backups private. The bot reuses a Garmin session in memory and rejects `GARMINTOKENS`, which the pinned upstream library uses for disk token storage. MFA-required logins fail clearly because this unattended bot cannot request an MFA code.

The bot requires Garmin credentials to log in, and the Telegram token to poll/send. Anyone with those credentials can access the associated services. The repository intentionally has no live values. A `.gitignore` helps prevent accidents but is not a substitute for reviewing the exact files before committing or sharing them.

## Tests and troubleshooting

```sh
python -m unittest discover -s tests -v
python -m compileall -q garmin_cal_bot.py tests
python -m tabnanny garmin_cal_bot.py tests
python -m pip check
```

Tests use deterministic clocks and fake Garmin/Telegram adapters. They must not contact either service. If the bot does not answer, verify the four environment variables are present, the configured chat ID is correct, only one poller uses the token, and the private log has recent events. If reminders do not fire, check `BOT_TIMEZONE` and the state job record. If a delivery is uncertain, follow the reconciliation steps in [Operations](docs/OPERATIONS.md); do not blindly delete state. Garmin authentication, account MFA, API schema changes, network outages, and rate limits may prevent delivery. A detected Garmin rate limit causes a six-hour cooldown. Repeated Telegram polling failures retry after 10 seconds, doubling to a five-minute cap; a successful poll resets that delay.

## Limitations

- There is no guaranteed exactly-once Telegram delivery across a crash or ambiguous network response. Uncertain outcomes require manual reconciliation.
- Signature change is a fallback freshness signal, not proof of a physical device sync. Identical calorie values after a real sync remain unverified without metadata.
- The program is single-user and uses one JSON state file. Running multiple instances against the same token/state is unsupported.
- Local tests validate logic and current dependency API signatures; no live Garmin or Telegram action was used for this validation.
- A missing or corrupt state file can lose prior offsets and delivery records; keep private backups if continuity is important.

## Related Projects

[Garmin Battery Alert](https://github.com/tzvika-por/garmin-battery-alert) is another Garmin/Telegram automation project by the same author. It monitors a watch battery using a Connect IQ app and Cloudflare relay. GarminCalBot reads Garmin Connect calorie data from a local Python service. They are separate implementations with no documented shared code, backend, or direct integration. Whether a private deployment uses the same Telegram bot or chat is not established by these repositories.

## License

Apache License 2.0. See [LICENSE](LICENSE). Third-party packages retain their own licenses; see [Dependency notes](docs/DEPENDENCIES.md).
