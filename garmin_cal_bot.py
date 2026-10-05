"""Single-user Garmin calorie bot. No network work happens on import."""

from __future__ import annotations

import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import signal
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as wall_time, timedelta, timezone
from typing import Callable, Mapping, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


REMINDER_TIME = wall_time(22, 30)
CHECK_TIME = wall_time(23, 5)
FRESH_WINDOW = timedelta(minutes=60)
DONE_RETRY_INTERVAL = timedelta(seconds=30)
DONE_MAX_ATTEMPTS = 8
GARMIN_COOLDOWN = timedelta(hours=6)
POLL_BACKOFF_INITIAL = 10
POLL_BACKOFF_CAP = 300
CALORIES_WORDS = {"calories", "קלוריות"}
DONE_WORDS = {"done", "בוצע"}
STATUS_WORDS = {"status", "סטטוס"}
TOKEN_PATTERN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{25,}\b")
BOT_URL_PATTERN = re.compile(r"https://api\.telegram\.org/bot[^/\s]+", re.I)


class StatePersistenceError(Exception):
    pass


class GarminAuthError(Exception):
    pass


class GarminFetchError(Exception):
    pass


class GarminRateLimitError(GarminFetchError):
    pass


class TelegramPollError(Exception):
    pass


class TelegramSendError(Exception):
    def __init__(self, *, definite: bool):
        super().__init__("Telegram delivery failed")
        self.definite = definite


class DeliveryUncertainError(Exception):
    pass


class DeliveryDeferredError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    garmin_email: str = field(repr=False)
    garmin_password: str = field(repr=False)
    telegram_token: str = field(repr=False)
    telegram_chat_id: str = field(repr=False)
    zone: ZoneInfo = field(repr=False)
    state_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        values = os.environ if env is None else env
        names = ("GARMIN_EMAIL", "GARMIN_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
        missing = [name for name in names if not values.get(name, "").strip()]
        if missing:
            raise ValueError("Missing required environment variables: " + ", ".join(missing))
        zone_name = values.get("BOT_TIMEZONE", "UTC")
        try:
            zone = ZoneInfo(zone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("BOT_TIMEZONE is not a valid IANA timezone") from exc
        directory = Path(values.get("BOT_STATE_DIR", str(Path.home() / ".local/state/garmin-cal-bot")))
        return cls(*(values[name] for name in names), zone, directory.expanduser())


def redact(message: str, secrets: tuple[str, ...] = ()) -> str:
    result = BOT_URL_PATTERN.sub("[TELEGRAM_API_URL]", message)
    result = TOKEN_PATTERN.sub("[TELEGRAM_TOKEN]", result)
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    return result


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...]):
        super().__init__("%(asctime)s %(levelname)s %(message)s")
        self.secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self.secrets)


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.baseFilename, flags, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding)

    def doRollover(self):
        super().doRollover()
        for index in range(1, self.backupCount + 1):
            path = Path(f"{self.baseFilename}.{index}")
            if path.exists():
                path.chmod(0o600)


def ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise PermissionError("state directory must not be a symbolic link")
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
        path.chmod(0o700)
    elif not path.is_dir() or (path.stat().st_mode & 0o077):
        raise PermissionError("state directory must be private (0700)")


def make_logger(config: Config) -> logging.Logger:
    ensure_private_directory(config.state_dir)
    logger = logging.getLogger("garmin_cal_bot")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    handler = PrivateRotatingHandler(config.state_dir / "bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(RedactingFormatter((config.garmin_email, config.garmin_password, config.telegram_token, config.telegram_chat_id)))
    logger.addHandler(handler)
    return logger


def default_state() -> dict:
    return {
        "schema": 1,
        "offset": None,
        "baseline_date": None,
        "baseline_signature": None,
        "last_verified_sync_at": None,
        "last_seen_sync_at": None,
        "last_prompt_at": None,
        "garmin_cooldown_until": None,
        "awaiting_sync": False,
        "jobs": {},
        "job_retry_at": {},
        "deliveries": {},
        "done_retry": None,
    }


class StateStore:
    def __init__(self, path: Path, logger: logging.Logger):
        self.path = path
        self.logger = logger

    def load(self) -> dict:
        if not self.path.exists():
            return default_state()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or loaded.get("schema") != 1:
                raise ValueError("invalid state schema")
            state = default_state()
            state.update(loaded)
            if any(not isinstance(state[key], dict) for key in ("jobs", "job_retry_at", "deliveries")):
                raise ValueError("invalid state collections")
            if state["offset"] is not None and (not isinstance(state["offset"], int) or isinstance(state["offset"], bool)):
                raise ValueError("invalid Telegram offset")
            if state["done_retry"] is not None and not isinstance(state["done_retry"], dict):
                raise ValueError("invalid retry state")
            return state
        except (OSError, ValueError, TypeError) as exc:
            self.logger.error("state load failed: %s; defaults in memory", type(exc).__name__)
            return default_state()

    def save(self, state: dict) -> None:
        path = self.path
        temp_name = None
        try:
            ensure_private_directory(path.parent)
            fd, temp_name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=path.parent)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            path.chmod(0o600)
            temp_name = None
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except (OSError, TypeError, ValueError) as exc:
            raise StatePersistenceError(type(exc).__name__) from None
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass


def local_now(zone: ZoneInfo, clock: Callable[[], datetime]) -> datetime:
    instant = clock()
    if instant.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return instant.astimezone(zone)


def parse_instant(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def parse_sync_value(value: object, zone: ZoneInfo) -> datetime | None:
    try:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            if value <= 0:
                return None
            seconds = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(seconds, timezone.utc)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or stripped.lower() in {"none", "null", "0"}:
                return None
            if stripped.replace(".", "", 1).isdigit():
                return parse_sync_value(float(stripped), zone)
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed
    except (OverflowError, OSError, ValueError):
        return None
    return None


def latest_device_sync(devices: object, zone: ZoneInfo, now: datetime) -> datetime | None:
    best = None
    if not isinstance(devices, list):
        return None
    for device in devices:
        if not isinstance(device, dict):
            continue
        for key, value in device.items():
            name = str(key).lower()
            if not (("sync" in name or "upload" in name) and any(part in name for part in ("time", "date", "ts"))):
                continue
            candidate = parse_sync_value(value, zone)
            if candidate and datetime(2020, 1, 1, tzinfo=timezone.utc) <= candidate <= now + timedelta(minutes=5):
                if best is None or candidate > best:
                    best = candidate
    return best


def number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError):
        return None


def first(summary: Mapping[str, object], names: tuple[str, ...]) -> object:
    return next((summary[name] for name in names if summary.get(name) is not None), None)


@dataclass(frozen=True)
class Calories:
    total: float | None
    active: float | None
    resting: float | None
    signature: str


def parse_calories(summary: Mapping[str, object], day: date) -> Calories:
    total = number(first(summary, ("totalKilocalories", "totalCalories")))
    active = number(first(summary, ("activeKilocalories", "activeCalories")))
    resting = number(first(summary, ("bmrKilocalories", "restingKilocalories", "restingCalories")))
    if total is None and active is not None and resting is not None:
        total = active + resting
    if resting is None and total is not None and active is not None:
        resting = max(total - active, 0)
    if active is None and total is not None and resting is not None:
        active = max(total - resting, 0)
    raw = f"{day.isoformat()}|{total}|{active}|{resting}"
    return Calories(total, active, resting, hashlib.sha256(raw.encode()).hexdigest())


def fmt(value: float | None) -> str:
    if value is None:
        return "N/A"
    return str(int(value)) if value.is_integer() else f"{value:.1f}"


def calories_message(calories: Calories, day: date, title: str = "Garmin Calories") -> str:
    return "\n".join((title, f"Date: {day.isoformat()}", f"Total: {fmt(calories.total)} kcal", f"Active: {fmt(calories.active)} kcal", f"Resting: {fmt(calories.resting)} kcal"))


@dataclass(frozen=True)
class Snapshot:
    calories: Calories
    sync_at: datetime | None
    metadata_error: bool = False


@dataclass(frozen=True)
class Freshness:
    status: str  # fresh, stale, unknown
    reason: str


def assess_freshness(snapshot: Snapshot, state: dict, now: datetime, *, after: datetime | None = None) -> Freshness:
    sync_at = snapshot.sync_at
    if sync_at is not None:
        age = now - sync_at
        if timedelta(0) <= age <= FRESH_WINDOW and (after is None or sync_at > after):
            return Freshness("fresh", "recent device sync timestamp")
        return Freshness("stale", "device sync timestamp is not newer or recent")
    day = now.date().isoformat()
    baseline = state.get("baseline_signature") if state.get("baseline_date") == day else None
    if baseline is None:
        return Freshness("unknown", "no same-day baseline")
    if snapshot.calories.signature != baseline:
        return Freshness("fresh", "daily data changed since baseline")
    return Freshness("stale", "no newer sync or data change")


class GarminPort(Protocol):
    def fetch(self, day: date, now: datetime) -> Snapshot: ...


class TelegramPort(Protocol):
    def send(self, text: str) -> None: ...
    def poll(self, offset: int | None) -> list[dict]: ...


class GarminClient:
    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self._client = None

    def _authenticated_client(self):
        if self._client is not None:
            return self._client
        # The upstream library uses GARMINTOKENS as a disk token-store path.
        if os.environ.get("GARMINTOKENS"):
            raise GarminAuthError("GARMINTOKENS must be unset; token files are unsupported")
        try:
            from garminconnect import Garmin
            client = Garmin(self.config.garmin_email, self.config.garmin_password)
            result = client.login()
            if result not in (None, (None, None)):
                raise GarminAuthError("Garmin login requires MFA or returned an unexpected result")
        except GarminAuthError:
            raise
        except Exception as exc:
            if rate_limited(exc):
                raise GarminRateLimitError() from None
            if "mfa" in str(exc).lower():
                raise GarminAuthError("Garmin login requires MFA") from None
            raise GarminAuthError(type(exc).__name__) from None
        self._client = client
        return client

    def fetch(self, day: date, now: datetime) -> Snapshot:
        client = self._authenticated_client()
        try:
            summary = client.get_user_summary(day.isoformat())
            if not isinstance(summary, dict):
                raise TypeError("unexpected summary type")
            calories = parse_calories(summary, day)
        except Exception as exc:
            if rate_limited(exc):
                raise GarminRateLimitError() from None
            if authentication_failed(exc):
                self._client = None
                raise GarminAuthError(type(exc).__name__) from None
            raise GarminFetchError(type(exc).__name__) from None
        try:
            devices = client.get_devices()
            sync_at = latest_device_sync(devices, self.config.zone, now)
            return Snapshot(calories, sync_at)
        except Exception as exc:
            if rate_limited(exc):
                raise GarminRateLimitError() from None
            if authentication_failed(exc):
                self._client = None
                raise GarminAuthError(type(exc).__name__) from None
            self.logger.warning("Garmin device metadata unavailable: %s", type(exc).__name__)
            return Snapshot(calories, None, True)


def rate_limited(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    response = getattr(exc, "response", None)
    return "toomanyrequests" in name or "ratelimit" in name or getattr(response, "status_code", None) == 429


def authentication_failed(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    response = getattr(exc, "response", None)
    return "authentication" in name or "autherror" in name or getattr(response, "status_code", None) == 401


class TelegramClient:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = f"https://api.telegram.org/bot{config.telegram_token}"

    def send(self, text: str) -> None:
        import requests
        try:
            response = requests.post(f"{self.base_url}/sendMessage", json={"chat_id": self.config.telegram_chat_id, "text": text}, timeout=20)
            if not response.ok:
                raise TelegramSendError(definite=400 <= response.status_code < 500)
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise TelegramSendError(definite=True)
        except TelegramSendError:
            raise
        except (requests.RequestException, ValueError):
            raise TelegramSendError(definite=False) from None

    def poll(self, offset: int | None) -> list[dict]:
        import requests
        params: dict[str, int] = {"timeout": 30}
        if offset is not None:
            params["offset"] = offset
        try:
            response = requests.get(f"{self.base_url}/getUpdates", params=params, timeout=40)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("result"), list):
                raise ValueError("invalid Telegram response")
            return payload["result"]
        except (requests.RequestException, ValueError) as exc:
            raise TelegramPollError(type(exc).__name__) from None


class Bot:
    def __init__(self, config: Config, store: StateStore, garmin: GarminPort, telegram: TelegramPort,
                 logger: logging.Logger, clock: Callable[[], datetime], sleeper: Callable[[float], None] = time.sleep):
        self.config = config
        self.store = store
        self.garmin = garmin
        self.telegram = telegram
        self.logger = logger
        self.clock = clock
        self.sleeper = sleeper
        self.state = store.load()
        self.stopping = False
        self.poll_failures = 0
        self.recover_sent_deliveries()

    def now(self) -> datetime:
        return local_now(self.config.zone, self.clock)

    def persist(self) -> None:
        self.store.save(self.state)

    def observe(self, snapshot: Snapshot, now: datetime) -> None:
        if snapshot.sync_at and (not parse_instant(self.state.get("last_seen_sync_at")) or snapshot.sync_at > parse_instant(self.state["last_seen_sync_at"])):
            self.state["last_seen_sync_at"] = snapshot.sync_at.isoformat()
        if self.state.get("baseline_date") != now.date().isoformat() or not self.state.get("baseline_signature"):
            self.state["baseline_date"] = now.date().isoformat()
            self.state["baseline_signature"] = snapshot.calories.signature
        self.persist()

    def fetch_snapshot(self, now: datetime) -> Snapshot:
        cooldown = parse_instant(self.state.get("garmin_cooldown_until"))
        if cooldown and now < cooldown:
            raise GarminRateLimitError()
        try:
            snapshot = self.garmin.fetch(now.date(), now)
        except GarminRateLimitError:
            self.state["garmin_cooldown_until"] = (now + GARMIN_COOLDOWN).isoformat()
            self.persist()
            raise
        if cooldown:
            self.state["garmin_cooldown_until"] = None
            self.persist()
        return snapshot

    def apply_sent_delivery(self, key: str) -> None:
        entry = self.state["deliveries"].get(key, {})
        if entry.get("status") != "sent" or entry.get("effect_applied"):
            return
        effect = entry.get("effect")
        if not isinstance(effect, dict):
            return  # Older records without recovery metadata have no exact effect to apply.
        kind = effect.get("kind")
        if kind == "verified":
            self.state["last_verified_sync_at"] = entry["at"]
            self.state["baseline_date"] = effect["day"]
            self.state["baseline_signature"] = effect["signature"]
            self.state["awaiting_sync"] = False
            update_id = effect.get("done_update_id")
            if update_id is not None and (self.state.get("done_retry") or {}).get("update_id") == update_id:
                self.state["done_retry"] = None
        elif kind == "prompt":
            self.state["last_prompt_at"] = entry["at"]
            self.state["awaiting_sync"] = True
        elif kind == "done_ack":
            update_id = effect["update_id"]
            started = parse_instant(entry["at"])
            self.state["done_retry"] = {
                "update_id": update_id, "attempts": 1, "day": effect["day"],
                "started_at": entry["at"], "next_at": (started + DONE_RETRY_INTERVAL).isoformat(),
            }
        elif kind == "done_final":
            if (self.state.get("done_retry") or {}).get("update_id") == effect["update_id"]:
                self.state["done_retry"] = None
            if effect.get("sync_prompt"):
                self.state["last_prompt_at"] = entry["at"]
                self.state["awaiting_sync"] = True
        else:
            raise ValueError("invalid delivery effect")
        if "job_day" in effect:
            day, job = effect["job_day"], effect["job_kind"]
            self.state["jobs"].setdefault(day, {})[job] = "complete"
            self.state["job_retry_at"].pop(f"{day}:{job}", None)
        entry["effect_applied"] = True
        self.persist()

    def recover_sent_deliveries(self) -> None:
        for key, entry in self.state["deliveries"].items():
            if (entry.get("status") == "sent" and not entry.get("effect_applied")
                    and isinstance(entry.get("effect"), dict)):
                self.apply_sent_delivery(key)

    def send_once(self, key: str, message: str, now: datetime, *, purpose: str = "command",
                  effect: dict | None = None) -> bool:
        entry = self.state["deliveries"].get(key, {})
        status = entry.get("status")
        if status == "sent":
            self.apply_sent_delivery(key)
            return True
        self.ensure_delivery_ready(key, now)
        delivery = {"status": "sending", "at": now.isoformat(), "purpose": purpose}
        if effect is not None:
            delivery["effect"] = effect
        self.state["deliveries"][key] = delivery
        self.persist()  # Never send unless intent is durable.
        try:
            self.telegram.send(message)
        except TelegramSendError as exc:
            self.state["deliveries"][key]["status"] = "pending" if exc.definite else "uncertain"
            if exc.definite:
                self.state["deliveries"][key]["next_at"] = (now + timedelta(minutes=1)).isoformat()
            self.persist()
            raise
        self.state["deliveries"][key]["status"] = "sent"
        self.persist()
        self.apply_sent_delivery(key)
        return True

    def ensure_delivery_ready(self, key: str, now: datetime) -> None:
        entry = self.state["deliveries"].get(key, {})
        if entry.get("status") in {"sending", "uncertain"}:
            raise DeliveryUncertainError(key)
        if entry.get("status") == "pending" and (parse_instant(entry.get("next_at")) or now) > now:
            raise DeliveryDeferredError(key)

    def prompt(self, key: str, reason: str, snapshot: Snapshot | None, now: datetime) -> None:
        if snapshot:
            self.observe(snapshot, now)
        effect = {"kind": "prompt"}
        if key.startswith("job:"):
            _, day, job = key.split(":", 2)
            effect.update(job_day=day, job_kind=job)
        self.send_once(key, "Data may be stale. Please sync Garmin Connect, then reply Done.\n" + reason, now,
                       purpose="prompt", effect=effect)

    def daily_action(self, kind: str, now: datetime) -> None:
        day_key = now.date().isoformat()
        jobs = self.state["jobs"].setdefault(day_key, {})
        if jobs.get(kind) == "complete":
            return
        retry_key = f"{day_key}:{kind}"
        if (parse_instant(self.state["job_retry_at"].get(retry_key)) or now) > now:
            return
        due = REMINDER_TIME if kind == "reminder" else CHECK_TIME
        if now.timetz().replace(tzinfo=None) < due:
            return
        key = f"job:{day_key}:{kind}"
        previous = self.state["deliveries"].get(key, {})
        if previous.get("status") == "sent":
            self.apply_sent_delivery(key)
            jobs[kind] = "complete"
            self.persist()
            return
        try:
            self.ensure_delivery_ready(key, now)
        except (DeliveryUncertainError, DeliveryDeferredError):
            return
        try:
            snapshot = self.fetch_snapshot(now)
            freshness = assess_freshness(snapshot, self.state, now)
            self.observe(snapshot, now)
            if kind == "reminder":
                if freshness.status != "fresh":
                    self.prompt(key, "Recent sync not verified.", snapshot, now)
            elif freshness.status == "fresh" and snapshot.calories.total is not None:
                self.send_once(key, calories_message(snapshot.calories, now.date()), now,
                               purpose="daily_calories",
                               effect={"kind": "verified", "day": day_key,
                                       "signature": snapshot.calories.signature,
                                       "job_day": day_key, "job_kind": kind})
            else:
                self.prompt(key, "Current calories or recent sync not verified.", snapshot, now)
            jobs[kind] = "complete"
            self.state["job_retry_at"].pop(retry_key, None)
            self.persist()
        except GarminRateLimitError:
            self.logger.warning("scheduled %s Garmin rate limit", kind)
            self.state["job_retry_at"][retry_key] = self.state["garmin_cooldown_until"] or (now + GARMIN_COOLDOWN).isoformat()
            self.persist()
        except (GarminAuthError, GarminFetchError) as exc:
            self.logger.warning("scheduled %s Garmin failure: %s", kind, type(exc).__name__)
            self.state["job_retry_at"][retry_key] = (now + timedelta(minutes=5)).isoformat()
            self.persist()
        except TelegramSendError as exc:
            self.logger.warning("scheduled %s Telegram send failure: %s", kind, "definite" if exc.definite else "uncertain")
        except DeliveryUncertainError:
            self.logger.warning("scheduled %s delivery outcome unknown; manual reconciliation needed", kind)
        except DeliveryDeferredError:
            pass

    def finish_sent_done(self, update_id: int) -> bool:
        result = self.state["deliveries"].get(f"done-result:{update_id}", {})
        if result.get("status") == "sent":
            self.apply_sent_delivery(f"done-result:{update_id}")
            return True
        if self.state["deliveries"].get(f"done-final:{update_id}", {}).get("status") == "sent":
            self.apply_sent_delivery(f"done-final:{update_id}")
            return True
        return False

    def handle_done(self, update_id: int, now: datetime) -> None:
        if self.finish_sent_done(update_id):
            return
        snapshot = self.fetch_snapshot(now)
        prompt_at = parse_instant(self.state.get("last_prompt_at")) if self.state.get("awaiting_sync") else None
        freshness = assess_freshness(snapshot, self.state, now, after=prompt_at)
        self.observe(snapshot, now)
        if freshness.status == "fresh" and snapshot.calories.total is not None:
            self.send_once(f"done-result:{update_id}", calories_message(snapshot.calories, now.date(), "Updated Garmin Calories"), now,
                           purpose="done_result",
                           effect={"kind": "verified", "day": now.date().isoformat(),
                                   "signature": snapshot.calories.signature, "done_update_id": update_id})
        else:
            self.send_once(f"done-ack:{update_id}", "Sync is not verified yet. I will retry briefly; no updated total has been confirmed.", now,
                           purpose="done_ack",
                           effect={"kind": "done_ack", "day": now.date().isoformat(), "update_id": update_id})

    def retry_done(self, now: datetime) -> None:
        retry = self.state.get("done_retry")
        if not retry:
            return
        update_id = retry["update_id"]
        if self.finish_sent_done(update_id):
            return
        started = parse_instant(retry.get("started_at"))
        if retry.get("day") != now.date().isoformat() or not started or now - started > timedelta(minutes=10):
            self.state["done_retry"] = None
            self.persist()
            return
        if (parse_instant(retry.get("next_at")) or now + DONE_RETRY_INTERVAL) > now:
            return
        try:
            self.ensure_delivery_ready(f"done-result:{update_id}", now)
            self.ensure_delivery_ready(f"done-final:{update_id}", now)
        except (DeliveryUncertainError, DeliveryDeferredError):
            return
        try:
            snapshot = self.fetch_snapshot(now)
            prompt_at = parse_instant(self.state.get("last_prompt_at")) if self.state.get("awaiting_sync") else None
            freshness = assess_freshness(snapshot, self.state, now, after=prompt_at)
            self.observe(snapshot, now)
            if freshness.status == "fresh" and snapshot.calories.total is not None:
                self.send_once(f"done-result:{update_id}", calories_message(snapshot.calories, now.date(), "Updated Garmin Calories"), now,
                               purpose="done_result",
                               effect={"kind": "verified", "day": now.date().isoformat(),
                                       "signature": snapshot.calories.signature, "done_update_id": update_id})
            elif retry["attempts"] >= DONE_MAX_ATTEMPTS:
                self.send_once(f"done-final:{update_id}", "Still unable to verify a new sync. Please sync again and reply Done.", now,
                               purpose="done_final", effect={"kind": "done_final", "update_id": update_id,
                                                             "sync_prompt": True})
            else:
                retry["attempts"] += 1
                retry["next_at"] = (now + DONE_RETRY_INTERVAL).isoformat()
            self.persist()
        except GarminRateLimitError:
            self.logger.warning("Done retry Garmin rate limit")
            retry["next_at"] = self.state["garmin_cooldown_until"] or (now + GARMIN_COOLDOWN).isoformat()
            self.persist()
        except (GarminAuthError, GarminFetchError) as exc:
            self.logger.warning("Done retry Garmin failure: %s", type(exc).__name__)
            if retry["attempts"] >= DONE_MAX_ATTEMPTS:
                self.send_once(f"done-final:{update_id}", "Unable to verify a new sync. Please try again later.", now,
                               purpose="done_final", effect={"kind": "done_final", "update_id": update_id,
                                                             "sync_prompt": False})
            else:
                retry["attempts"] += 1
                retry["next_at"] = (now + DONE_RETRY_INTERVAL).isoformat()
            self.persist()
        except TelegramSendError as exc:
            self.logger.warning("Done retry Telegram failure: %s", "definite" if exc.definite else "uncertain")
            if exc.definite:
                retry["next_at"] = (now + timedelta(minutes=1)).isoformat()
                self.persist()
        except DeliveryUncertainError:
            self.logger.warning("Done retry delivery outcome unknown; manual reconciliation needed")
        except DeliveryDeferredError:
            pass

    def status_text(self) -> str:
        return ("GarminCalBot status\n"
                f"Awaiting sync: {self.state['awaiting_sync']}\n"
                f"Last verified sync: {self.state.get('last_verified_sync_at') or 'None'}\n"
                f"Timezone: {self.config.zone.key}\nReminder: 22:30 | Check: 23:05")

    def process_update(self, update: dict, now: datetime) -> None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return
        msg = update.get("message") or update.get("edited_message") or {}
        chat_id = str(msg.get("chat", {}).get("id"))
        if chat_id != self.config.telegram_chat_id:
            self.state["offset"] = update_id + 1
            self.persist()
            return
        word = str(msg.get("text") or "").strip().lower()
        key = f"cmd:{update_id}"
        if self.state["deliveries"].get(key, {}).get("status") == "sent":
            self.apply_sent_delivery(key)
            self.state["offset"] = update_id + 1
            self.persist()
            return
        self.ensure_delivery_ready(key, now)
        try:
            if word in STATUS_WORDS:
                self.send_once(key, self.status_text(), now)
            elif word in CALORIES_WORDS:
                snapshot = self.fetch_snapshot(now)
                self.observe(snapshot, now)
                if snapshot.calories.total is None:
                    self.prompt(key, "Total calories are unavailable.", snapshot, now)
                else:
                    self.send_once(key, calories_message(snapshot.calories, now.date(), "Garmin Calories (sync not verified)"), now)
            elif word in DONE_WORDS:
                self.handle_done(update_id, now)
        except GarminRateLimitError:
            self.logger.warning("command Garmin rate limit")
            self.send_once(key, "Garmin is rate-limited. Please try again later.", now)
        except (GarminAuthError, GarminFetchError) as exc:
            self.logger.warning("command Garmin failure: %s", type(exc).__name__)
            self.send_once(key, "Garmin is temporarily unavailable. Please try again later.", now)
        self.state["offset"] = update_id + 1
        self.persist()  # Advance only after successful processing/delivery.

    def run_cycle(self) -> bool:
        now = self.now()
        self.daily_action("reminder", now)
        self.daily_action("check", now)
        self.retry_done(now)
        try:
            updates = self.telegram.poll(self.state.get("offset"))
        except TelegramPollError as exc:
            self.logger.warning("Telegram polling failure: %s", type(exc).__name__)
            return False
        for update in updates:
            try:
                self.process_update(update, self.now())
            except TelegramSendError as exc:
                self.logger.warning("command Telegram send failure: %s", "definite" if exc.definite else "uncertain")
                break
            except DeliveryUncertainError:
                self.logger.warning("command delivery outcome unknown; manual reconciliation needed")
                break
            except DeliveryDeferredError:
                break
        return True

    def run_forever(self) -> None:
        # Intentionally no startup Telegram message: launchd restarts cannot flood a chat.
        while not self.stopping:
            try:
                polled = self.run_cycle()
                if polled:
                    self.poll_failures = 0
                else:
                    self.poll_failures = min(self.poll_failures + 1, 6)
            except StatePersistenceError:
                self.logger.error("state persistence failed; cycle paused")
            except Exception as exc:
                self.logger.error("unexpected cycle failure: %s", type(exc).__name__)
            delay = (min(POLL_BACKOFF_INITIAL * 2 ** (self.poll_failures - 1), POLL_BACKOFF_CAP)
                     if self.poll_failures else 1)
            self.sleeper(delay)


def main() -> None:
    config = Config.from_env()
    logger = make_logger(config)
    store = StateStore(config.state_dir / "state.json", logger)
    bot = Bot(config, store, GarminClient(config, logger), TelegramClient(config), logger,
              lambda: datetime.now(timezone.utc))

    def stop(_signum, _frame):
        bot.stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    bot.run_forever()


if __name__ == "__main__":
    main()
