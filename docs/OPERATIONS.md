# Operations and recovery

These instructions apply to a separate installation of GarminCalBot. Keep its configuration, state, and credentials separate from any existing deployment.

## State and delivery transitions

The bot writes `BOT_STATE_DIR/state.json` with owner-only permissions. A Telegram delivery record moves through `sending` to `sent`. Delivery intents for prompts and verified results include non-secret recovery metadata (message purpose, date, SHA-256 calorie signature where needed, and job or Done identity). Once a send is confirmed, the bot applies its state effect and records that application; restart reconciles any confirmed effect that was interrupted. An explicit 4xx rejection becomes `pending` and retries after one minute. A network timeout or process interruption can leave `sending` or `uncertain`; the bot pauses that delivery to avoid an ordinary duplicate. A daily job is complete only after a skip due to verified freshness or a confirmed send. Garmin failures do not complete jobs. Authentication rate limits impose a six-hour cooldown.

## Resolve an uncertain delivery

1. Stop this service so only one process owns the state file.
2. Check the Telegram chat for the specific command or scheduled message and its date. The delivery key is in `state.json` (for example, `job:YYYY-MM-DD:check` or `cmd:123`). Do not share the state file publicly.
3. If the message is present, change that delivery's `status` from `sending`/`uncertain` to `sent`. If it is definitely absent, change it to `pending` and remove any `next_at` value. Use an editor that preserves valid JSON and mode `0600`.
4. Restart this service. A confirmed `sent` delivery with recovery metadata is reconciled before new work. For a daily job, the bot will mark the already-sent delivery complete without sending it twice, or retry a confirmed-absent delivery. For a command, the saved Telegram offset advances only after processing succeeds.

If you cannot determine whether the message arrived, leave the record uncertain and decide explicitly whether avoiding a duplicate or avoiding a missed message matters more. Telegram `sendMessage` has no application-supplied idempotency key.

## launchd example

Use the example in `deploy/macos/` as a **user LaunchAgent**. Replace all placeholder paths and use a private environment file outside the repository. Copy `run-bot.sh.example` to the private wrapper path named in the plist, then run `chmod 700 /ABSOLUTE/PRIVATE/PATH/run-bot.sh`; verify `test -x /ABSOLUTE/PRIVATE/PATH/run-bot.sh` before loading the LaunchAgent. Keep the environment file and state directory owner-only. The example sends launchd stdout/stderr to `/dev/null` because the application maintains a bounded private log. Install one instance only. Check `bot.log` for sanitized event categories if it is not responding.

## State corruption

Atomic writes reduce partial JSON writes. If the state file was damaged externally, the bot logs a state-load error and starts with defaults in memory. This may replay Telegram updates and lose delivery history. Stop the service, restore a private known-good state backup if available, and inspect the chat before resuming. Never copy another installation's state into this one for testing.
