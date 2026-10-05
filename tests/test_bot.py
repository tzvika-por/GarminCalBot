"""Offline deterministic tests: fake Garmin and Telegram ports only."""

import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import garmin_cal_bot as g


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, **delta):
        self.value += timedelta(**delta)


class FakeGarmin:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot
        self.error = None
        self.calls = 0

    def fetch(self, day, now):
        self.calls += 1
        if self.error:
            raise self.error
        return self.snapshot


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.updates = []
        self.failure = None
        self.polled = []

    def send(self, text):
        if self.failure:
            error = self.failure
            self.failure = None
            raise error
        self.messages.append(text)

    def poll(self, offset):
        self.polled.append(offset)
        return self.updates


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {
            "GARMIN_EMAIL": "example@example.invalid",
            "GARMIN_PASSWORD": "synthetic-password",
            "TELEGRAM_BOT_TOKEN": "123456789:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            "TELEGRAM_CHAT_ID": "42",
            "BOT_TIMEZONE": "UTC",
            "BOT_STATE_DIR": str(self.root),
        }
        self.config = g.Config.from_env(self.env)
        self.logger = logging.getLogger(f"test.{self.id()}")
        self.logger.addHandler(logging.NullHandler())
        self.clock = Clock(datetime(2026, 1, 2, 12, tzinfo=timezone.utc))
        self.store = g.StateStore(self.root / "state.json", self.logger)
        self.snapshot = self.make_snapshot()
        self.garmin = FakeGarmin(self.snapshot)
        self.telegram = FakeTelegram()
        self.bot = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)

    def make_snapshot(self, summary=None, sync_at=None):
        summary = summary or {"totalKilocalories": 2100, "activeKilocalories": 500,
                              "bmrKilocalories": 1600, "totalSteps": 3000}
        return g.Snapshot(g.parse_calories(summary, date(2026, 1, 2)), sync_at)

    def update(self, word, ident=1, chat=42):
        return {"update_id": ident, "message": {"chat": {"id": chat}, "text": word}}

    def command_crashes_before_offset_save(self, word):
        original_save = self.store.save
        def save(state):
            if state["offset"] == 2:
                raise g.StatePersistenceError("synthetic crash before offset save")
            original_save(state)
        with patch.object(self.store, "save", side_effect=save):
            with self.assertRaises(g.StatePersistenceError):
                self.bot.process_update(self.update(word), self.clock())
        self.assertIsNone(self.store.load()["offset"])

    def done_crashes_before_offset_save(self):
        self.command_crashes_before_offset_save("Done")

    def crash_after_confirmed_send(self, action):
        with patch.object(self.bot, "apply_sent_delivery", side_effect=g.StatePersistenceError("synthetic crash")):
            with self.assertRaises(g.StatePersistenceError):
                action()

    def test_config_requires_all_values_without_disclosing_them(self):
        env = dict(self.env)
        del env["GARMIN_PASSWORD"]
        with self.assertRaisesRegex(ValueError, "GARMIN_PASSWORD") as caught:
            g.Config.from_env(env)
        self.assertNotIn("synthetic-password", str(caught.exception))

    def test_config_rejects_invalid_zone(self):
        env = dict(self.env, BOT_TIMEZONE="Never/Exists")
        with self.assertRaises(ValueError):
            g.Config.from_env(env)

    def test_calorie_fields_and_format(self):
        calories = self.snapshot.calories
        self.assertEqual((calories.total, calories.active, calories.resting), (2100, 500, 1600))
        message = g.calories_message(calories, date(2026, 1, 2))
        self.assertIn("Total: 2100 kcal", message)
        self.assertIn("Active: 500 kcal", message)
        self.assertIn("Resting: 1600 kcal", message)

    def test_calorie_fallbacks_and_missing(self):
        result = g.parse_calories({"activeCalories": 400, "restingCalories": 1500}, date(2026, 1, 2))
        self.assertEqual(result.total, 1900)
        self.assertEqual(g.fmt(None), "N/A")
        self.assertIsNone(g.number(float("nan")))

    def test_sync_metadata_parser_and_timezone(self):
        zone = ZoneInfo("Asia/Jerusalem")
        now = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
        devices = [{"lastSyncTime": "2026-01-02T13:58:00+02:00", "lastUploadTs": 0}]
        self.assertEqual(g.latest_device_sync(devices, zone, now), datetime(2026, 1, 2, 13, 58, tzinfo=timezone(timedelta(hours=2))))
        self.assertIsNone(g.parse_sync_value(0, zone))

    def test_fresh_sync_metadata(self):
        snap = self.make_snapshot(sync_at=self.clock() - timedelta(minutes=2))
        self.assertEqual(g.assess_freshness(snap, self.bot.state, self.clock()).status, "fresh")

    def test_metadata_must_follow_prompt_for_done(self):
        now = self.clock()
        snap = self.make_snapshot(sync_at=now - timedelta(minutes=3))
        self.bot.state.update(baseline_date=now.date().isoformat(), baseline_signature=snap.calories.signature)
        self.assertEqual(g.assess_freshness(snap, self.bot.state, now, after=now - timedelta(minutes=2)).status, "stale")

    def test_stale_metadata_takes_precedence_over_changed_signature(self):
        now = self.clock()
        snap = self.make_snapshot(sync_at=now - timedelta(minutes=90))
        self.bot.state.update(baseline_date=now.date().isoformat(), baseline_signature="different")
        self.assertEqual(g.assess_freshness(snap, self.bot.state, now).status, "stale")

    def test_changed_baseline_is_fresh(self):
        self.bot.state.update(baseline_date="2026-01-02", baseline_signature="old")
        self.assertEqual(g.assess_freshness(self.snapshot, self.bot.state, self.clock()).status, "fresh")

    def test_unchanged_baseline_is_stale(self):
        self.bot.state.update(baseline_date="2026-01-02", baseline_signature=self.snapshot.calories.signature)
        self.assertEqual(g.assess_freshness(self.snapshot, self.bot.state, self.clock()).status, "stale")

    def test_first_run_baseline_unknown(self):
        self.assertEqual(g.assess_freshness(self.snapshot, self.bot.state, self.clock()).status, "unknown")
        self.bot.observe(self.snapshot, self.clock())
        self.assertEqual(self.bot.state["baseline_signature"], self.snapshot.calories.signature)

    def test_status_command(self):
        self.bot.process_update(self.update("status"), self.clock())
        self.assertIn("Timezone: UTC", self.telegram.messages[0])
        self.assertEqual(self.bot.state["offset"], 2)

    def test_calories_command_without_claimed_freshness(self):
        self.bot.process_update(self.update("calories"), self.clock())
        self.assertIn("sync not verified", self.telegram.messages[0])
        self.assertIn("Total: 2100 kcal", self.telegram.messages[0])

    def test_missing_total_prompts_sync(self):
        self.garmin.snapshot = self.make_snapshot({"totalSteps": 100})
        self.bot.process_update(self.update("calories"), self.clock())
        self.assertTrue(self.bot.state["awaiting_sync"])
        self.assertIn("Please sync", self.telegram.messages[0])

    def test_done_confirms_only_newer_sync(self):
        now = self.clock()
        self.bot.state.update(awaiting_sync=True, last_prompt_at=(now - timedelta(minutes=4)).isoformat(),
                              baseline_date="2026-01-02", baseline_signature=self.snapshot.calories.signature)
        self.garmin.snapshot = self.make_snapshot(sync_at=now - timedelta(minutes=1))
        self.bot.process_update(self.update("Done"), now)
        self.assertIn("Updated", self.telegram.messages[0])
        self.assertIsNotNone(self.bot.state["last_verified_sync_at"])
        self.assertFalse(self.bot.state["awaiting_sync"])
        self.assertEqual(self.bot.state["deliveries"]["done-result:1"]["status"], "sent")

    def test_done_unchanged_does_not_verify(self):
        self.bot.state.update(baseline_date="2026-01-02", baseline_signature=self.snapshot.calories.signature)
        self.bot.process_update(self.update("Done"), self.clock())
        self.assertIsNone(self.bot.state["last_verified_sync_at"])
        self.assertIsNotNone(self.bot.state["done_retry"])
        self.assertIn("not verified", self.telegram.messages[0])
        self.assertEqual(self.bot.state["deliveries"]["done-ack:1"]["status"], "sent")
        self.assertNotIn("done-result:1", self.bot.state["deliveries"])

    def test_done_first_run_unknown(self):
        self.bot.process_update(self.update("Done"), self.clock())
        self.assertIsNone(self.bot.state["last_verified_sync_at"])
        self.assertEqual(self.bot.state["baseline_signature"], self.snapshot.calories.signature)

    def test_done_retry_detects_change(self):
        self.bot.process_update(self.update("Done"), self.clock())
        self.garmin.snapshot = self.make_snapshot({"totalKilocalories": 2200, "activeKilocalories": 600,
                                                   "bmrKilocalories": 1600})
        self.clock.advance(seconds=31)
        self.bot.retry_done(self.clock())
        self.assertIsNone(self.bot.state["done_retry"])
        self.assertIsNotNone(self.bot.state["last_verified_sync_at"])
        self.assertEqual(len(self.telegram.messages), 2)
        self.assertEqual(self.bot.state["deliveries"]["done-result:1"]["status"], "sent")

    def test_done_stale_ack_crash_then_fresh_replay_delivers_result_once(self):
        self.bot.state.update(baseline_date="2026-01-02", baseline_signature=self.snapshot.calories.signature)
        self.done_crashes_before_offset_save()
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertEqual(self.store.load()["deliveries"]["done-ack:1"]["status"], "sent")
        self.assertIsNotNone(self.store.load()["done_retry"])
        self.garmin.snapshot = self.make_snapshot({"totalKilocalories": 2200, "activeKilocalories": 600,
                                                   "bmrKilocalories": 1600})
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        again.process_update(self.update("Done"), self.clock())
        self.assertEqual(len(self.telegram.messages), 2)
        self.assertEqual(sum("Updated Garmin Calories" in msg for msg in self.telegram.messages), 1)
        self.assertEqual(again.state["deliveries"]["done-result:1"]["status"], "sent")
        self.assertEqual(again.state["deliveries"]["done-ack:1"]["status"], "sent")
        self.assertIsNotNone(again.state["last_verified_sync_at"])
        self.assertIsNone(again.state["done_retry"])
        self.assertEqual(again.state["offset"], 2)
        self.garmin.snapshot = self.snapshot
        again.process_update(self.update("Done"), self.clock())
        self.assertEqual(len(self.telegram.messages), 2)

    def test_done_stale_replay_does_not_resend_ack_or_reset_retry(self):
        self.bot.state.update(baseline_date="2026-01-02", baseline_signature=self.snapshot.calories.signature)
        self.done_crashes_before_offset_save()
        retry = self.store.load()["done_retry"]
        self.clock.advance(seconds=10)
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        again.process_update(self.update("Done"), self.clock())
        again.process_update(self.update("Done"), self.clock())
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertEqual(again.state["done_retry"], retry)
        self.assertEqual(again.state["offset"], 2)

    def test_done_initial_fresh_result_replay_after_offset_crash(self):
        now = self.clock()
        self.bot.state.update(awaiting_sync=True, last_prompt_at=(now - timedelta(minutes=4)).isoformat())
        self.garmin.snapshot = self.make_snapshot(sync_at=now - timedelta(minutes=1))
        self.done_crashes_before_offset_save()
        self.assertEqual(self.store.load()["deliveries"]["done-result:1"]["status"], "sent")
        self.garmin.snapshot = self.snapshot
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        again.process_update(self.update("Done"), self.clock())
        again.process_update(self.update("Done"), self.clock())
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertIsNotNone(again.state["last_verified_sync_at"])
        self.assertIsNone(again.state["done_retry"])
        self.assertEqual(again.state["offset"], 2)

    def test_done_retry_recovers_sent_result_after_crash_before_effect(self):
        self.bot.process_update(self.update("Done"), self.clock())
        self.garmin.snapshot = self.make_snapshot({"totalKilocalories": 2200, "activeKilocalories": 600,
                                                   "bmrKilocalories": 1600})
        delivered_signature = self.garmin.snapshot.calories.signature
        old_signature = self.store.load()["baseline_signature"]
        self.clock.advance(seconds=31)
        with patch.object(self.bot, "apply_sent_delivery", side_effect=g.StatePersistenceError("synthetic crash")):
            with self.assertRaises(g.StatePersistenceError):
                self.bot.retry_done(self.clock())
        self.assertEqual(self.store.load()["deliveries"]["done-result:1"]["status"], "sent")
        self.assertNotIn("effect_applied", self.store.load()["deliveries"]["done-result:1"])
        self.assertEqual(self.store.load()["baseline_signature"], old_signature)
        self.assertIsNotNone(self.store.load()["done_retry"])
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        self.assertEqual(again.state["baseline_signature"], delivered_signature)
        self.assertEqual(again.state["baseline_date"], "2026-01-02")
        again.retry_done(self.clock())
        again.retry_done(self.clock())
        self.assertEqual(len(self.telegram.messages), 2)
        self.assertEqual(sum("Updated Garmin Calories" in msg for msg in self.telegram.messages), 1)
        self.assertIsNotNone(again.state["last_verified_sync_at"])
        self.assertIsNone(again.state["done_retry"])
        again.process_update(self.update("Done", ident=2), self.clock())
        self.assertEqual(sum("Updated Garmin Calories" in msg for msg in self.telegram.messages), 1)
        self.assertEqual(again.state["deliveries"]["done-ack:2"]["status"], "sent")

    def test_done_initial_result_recovers_hash_after_confirmed_send_crash(self):
        now = self.clock()
        self.bot.state.update(awaiting_sync=True, last_prompt_at=(now - timedelta(minutes=4)).isoformat(),
                              baseline_date=now.date().isoformat(), baseline_signature=self.snapshot.calories.signature)
        self.garmin.snapshot = self.make_snapshot({"totalKilocalories": 2200}, sync_at=now - timedelta(minutes=1))
        delivered_signature = self.garmin.snapshot.calories.signature
        self.crash_after_confirmed_send(lambda: self.bot.process_update(self.update("Done"), self.clock()))
        self.assertEqual(self.store.load()["deliveries"]["done-result:1"]["status"], "sent")
        self.assertNotEqual(self.store.load()["baseline_signature"], delivered_signature)
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        self.assertEqual(again.state["baseline_signature"], delivered_signature)
        self.assertFalse(again.state["awaiting_sync"])
        again.process_update(self.update("Done"), self.clock())
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertEqual(again.state["offset"], 2)

    def test_recovery_metadata_uses_hash_and_no_raw_values(self):
        self.bot.state.update(baseline_date="2026-01-02", baseline_signature=self.snapshot.calories.signature)
        self.garmin.snapshot = self.make_snapshot({"totalKilocalories": 2200, "activeKilocalories": 600,
                                                   "bmrKilocalories": 1600})
        self.bot.process_update(self.update("Done"), self.clock())
        effect = self.store.load()["deliveries"]["done-result:1"]["effect"]
        self.assertEqual(set(effect), {"kind", "day", "signature", "done_update_id"})
        self.assertRegex(effect["signature"], r"^[0-9a-f]{64}$")
        self.assertEqual(effect["signature"], self.garmin.snapshot.calories.signature)
        self.assertFalse(any(value in effect.values() for value in (2200, 600, 1600)))
        serialized = json.dumps(effect)
        for key in ("GARMIN_EMAIL", "GARMIN_PASSWORD", "TELEGRAM_BOT_TOKEN"):
            self.assertNotIn(self.env[key], serialized)
        self.assertNotIn("chat_id", serialized)

    def test_done_ack_recovery_starts_retry_without_resending(self):
        self.bot.state.update(baseline_date="2026-01-02", baseline_signature=self.snapshot.calories.signature)
        self.crash_after_confirmed_send(lambda: self.bot.process_update(self.update("Done"), self.clock()))
        recorded = self.store.load()
        self.assertEqual(recorded["deliveries"]["done-ack:1"]["status"], "sent")
        self.assertIsNone(recorded["done_retry"])
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        retry = again.state["done_retry"]
        self.assertEqual(retry["update_id"], 1)
        self.assertEqual(retry["attempts"], 1)
        self.assertEqual(g.parse_instant(retry["next_at"]), self.clock() + g.DONE_RETRY_INTERVAL)
        again.process_update(self.update("Done"), self.clock())
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertEqual(again.state["done_retry"], retry)
        self.assertEqual(again.state["offset"], 2)

    def test_done_final_prompt_recovers_awaiting_state_without_resend(self):
        self.bot.process_update(self.update("Done"), self.clock())
        self.bot.state["done_retry"]["attempts"] = g.DONE_MAX_ATTEMPTS
        self.bot.state["done_retry"]["next_at"] = self.clock().isoformat()
        self.bot.state["awaiting_sync"] = False
        self.bot.state["last_prompt_at"] = None
        self.bot.persist()
        self.crash_after_confirmed_send(lambda: self.bot.retry_done(self.clock()))
        recorded = self.store.load()
        self.assertEqual(recorded["deliveries"]["done-final:1"]["status"], "sent")
        self.assertFalse(recorded["awaiting_sync"])
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        self.assertTrue(again.state["awaiting_sync"])
        self.assertEqual(again.state["last_prompt_at"], recorded["deliveries"]["done-final:1"]["at"])
        self.assertIsNone(again.state["done_retry"])
        again.retry_done(self.clock())
        again.process_update(self.update("Done"), self.clock())
        self.assertEqual(len(self.telegram.messages), 2)
        self.assertEqual(again.state["offset"], 2)

    def test_done_final_auth_failure_recovers_retry_clear_without_prompt(self):
        self.bot.process_update(self.update("Done"), self.clock())
        self.bot.state["done_retry"]["attempts"] = g.DONE_MAX_ATTEMPTS
        self.bot.state["done_retry"]["next_at"] = self.clock().isoformat()
        self.bot.state["awaiting_sync"] = False
        self.bot.state["last_prompt_at"] = None
        self.bot.persist()
        self.garmin.error = g.GarminAuthError("synthetic")
        self.crash_after_confirmed_send(lambda: self.bot.retry_done(self.clock()))
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        self.assertIsNone(again.state["done_retry"])
        self.assertFalse(again.state["awaiting_sync"])
        self.assertIsNone(again.state["last_prompt_at"])
        again.retry_done(self.clock())
        self.assertEqual(len(self.telegram.messages), 2)

    def test_done_retry_exhaustion(self):
        self.bot.process_update(self.update("Done"), self.clock())
        for _ in range(g.DONE_MAX_ATTEMPTS):
            self.clock.advance(seconds=31)
            self.bot.retry_done(self.clock())
        self.assertIsNone(self.bot.state["done_retry"])
        self.assertIsNone(self.bot.state["last_verified_sync_at"])
        self.assertIn("unable to verify", self.telegram.messages[-1])
        self.assertEqual(self.bot.state["deliveries"]["done-final:1"]["status"], "sent")

    def test_done_terminal_failure_replay_does_not_resend_ack(self):
        self.bot.process_update(self.update("Done"), self.clock())
        for _ in range(g.DONE_MAX_ATTEMPTS):
            self.clock.advance(seconds=31)
            self.bot.retry_done(self.clock())
        self.bot.state["offset"] = None
        self.bot.persist()
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        again.process_update(self.update("Done"), self.clock())
        again.process_update(self.update("Done"), self.clock())
        self.assertEqual(len(self.telegram.messages), 2)
        self.assertIsNone(again.state["done_retry"])
        self.assertEqual(again.state["offset"], 2)

    def test_done_retry_does_not_confirm_on_next_day(self):
        self.bot.process_update(self.update("Done"), self.clock())
        self.clock.advance(days=1)
        self.garmin.snapshot = self.make_snapshot(sync_at=self.clock() - timedelta(minutes=1))
        self.bot.retry_done(self.clock())
        self.assertIsNone(self.bot.state["done_retry"])
        self.assertIsNone(self.bot.state["last_verified_sync_at"])
        self.assertEqual(len(self.telegram.messages), 1)

    def test_reminder_catches_up_after_target_minute(self):
        self.clock.value = datetime(2026, 1, 2, 22, 47, tzinfo=timezone.utc)
        self.bot.daily_action("reminder", self.clock())
        self.assertEqual(self.bot.state["jobs"]["2026-01-02"]["reminder"], "complete")
        self.assertEqual(len(self.telegram.messages), 1)

    def test_reminder_skips_when_fresh(self):
        self.clock.value = datetime(2026, 1, 2, 22, 31, tzinfo=timezone.utc)
        self.garmin.snapshot = self.make_snapshot(sync_at=self.clock() - timedelta(minutes=1))
        self.bot.daily_action("reminder", self.clock())
        self.assertEqual(self.telegram.messages, [])
        self.assertEqual(self.bot.state["jobs"]["2026-01-02"]["reminder"], "complete")

    def test_daily_check_sends_after_missed_minute(self):
        self.clock.value = datetime(2026, 1, 2, 23, 45, tzinfo=timezone.utc)
        self.garmin.snapshot = self.make_snapshot(sync_at=self.clock() - timedelta(minutes=1))
        self.bot.daily_action("check", self.clock())
        self.assertIn("Total: 2100 kcal", self.telegram.messages[0])
        self.assertEqual(self.bot.state["jobs"]["2026-01-02"]["check"], "complete")

    def test_successful_job_does_not_repeat_after_restart(self):
        self.clock.value = datetime(2026, 1, 2, 23, 6, tzinfo=timezone.utc)
        self.garmin.snapshot = self.make_snapshot(sync_at=self.clock() - timedelta(minutes=1))
        self.bot.daily_action("check", self.clock())
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        again.daily_action("check", self.clock())
        self.assertEqual(len(self.telegram.messages), 1)

    def test_daily_result_reconciles_baseline_and_job_after_confirmed_send_crash(self):
        self.clock.value = datetime(2026, 1, 2, 23, 6, tzinfo=timezone.utc)
        self.bot.state.update(baseline_date="2026-01-02", baseline_signature=self.snapshot.calories.signature,
                              awaiting_sync=True)
        self.bot.persist()
        self.garmin.snapshot = self.make_snapshot({"totalKilocalories": 2200},
                                                  sync_at=self.clock() - timedelta(minutes=1))
        delivered_signature = self.garmin.snapshot.calories.signature
        self.crash_after_confirmed_send(lambda: self.bot.daily_action("check", self.clock()))
        recorded = self.store.load()
        self.assertEqual(recorded["deliveries"]["job:2026-01-02:check"]["status"], "sent")
        self.assertNotEqual(recorded["baseline_signature"], delivered_signature)
        self.assertNotEqual(recorded["jobs"]["2026-01-02"].get("check"), "complete")
        self.garmin.snapshot = self.make_snapshot({"totalKilocalories": 2200})
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        self.assertEqual(again.state["baseline_signature"], delivered_signature)
        self.assertEqual(again.state["baseline_date"], "2026-01-02")
        self.assertEqual(again.state["last_verified_sync_at"], recorded["deliveries"]["job:2026-01-02:check"]["at"])
        self.assertFalse(again.state["awaiting_sync"])
        self.assertEqual(again.state["jobs"]["2026-01-02"]["check"], "complete")
        prior_fetches = self.garmin.calls
        again.daily_action("check", self.clock())
        self.assertEqual(self.garmin.calls, prior_fetches)
        self.assertEqual(len(self.telegram.messages), 1)
        again.process_update(self.update("Done", ident=2), self.clock())
        self.assertEqual(sum("Updated Garmin Calories" in msg for msg in self.telegram.messages), 0)
        self.assertEqual(again.state["deliveries"]["done-ack:2"]["status"], "sent")

    def test_scheduled_prompt_reconciles_after_confirmed_send_crash(self):
        self.clock.value = datetime(2026, 1, 2, 22, 31, tzinfo=timezone.utc)
        self.crash_after_confirmed_send(lambda: self.bot.daily_action("reminder", self.clock()))
        recorded = self.store.load()
        self.assertEqual(recorded["deliveries"]["job:2026-01-02:reminder"]["status"], "sent")
        self.assertFalse(recorded["awaiting_sync"])
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        self.assertTrue(again.state["awaiting_sync"])
        self.assertEqual(again.state["last_prompt_at"], recorded["deliveries"]["job:2026-01-02:reminder"]["at"])
        self.assertEqual(again.state["jobs"]["2026-01-02"]["reminder"], "complete")
        again.daily_action("reminder", self.clock())
        self.assertEqual(len(self.telegram.messages), 1)

    def test_failed_garmin_does_not_complete_job(self):
        self.clock.value = datetime(2026, 1, 2, 22, 31, tzinfo=timezone.utc)
        self.garmin.error = g.GarminFetchError("synthetic")
        self.bot.daily_action("reminder", self.clock())
        self.assertNotEqual(self.bot.state["jobs"]["2026-01-02"].get("reminder"), "complete")
        self.bot.daily_action("reminder", self.clock())
        self.assertEqual(self.garmin.calls, 1)  # Five-minute retry backoff.

    def test_garmin_rate_limit_sets_cooldown(self):
        self.clock.value = datetime(2026, 1, 2, 22, 31, tzinfo=timezone.utc)
        self.garmin.error = g.GarminRateLimitError()
        self.bot.daily_action("reminder", self.clock())
        until = g.parse_instant(self.bot.state["garmin_cooldown_until"])
        self.assertEqual(until, self.clock() + timedelta(hours=6))
        self.clock.advance(minutes=1)
        self.bot.daily_action("reminder", self.clock())
        self.assertEqual(self.garmin.calls, 1)

    def test_failed_daily_send_is_not_complete(self):
        self.clock.value = datetime(2026, 1, 2, 23, 6, tzinfo=timezone.utc)
        self.garmin.snapshot = self.make_snapshot(sync_at=self.clock() - timedelta(minutes=1))
        self.telegram.failure = g.TelegramSendError(definite=True)
        self.bot.daily_action("check", self.clock())
        self.assertNotEqual(self.bot.state["jobs"]["2026-01-02"].get("check"), "complete")
        self.clock.advance(seconds=61)
        self.bot.daily_action("check", self.clock())
        self.assertEqual(self.bot.state["jobs"]["2026-01-02"]["check"], "complete")
        self.assertEqual(len(self.telegram.messages), 1)

    def test_sent_prompt_recovered_after_crash_without_reinterpretation(self):
        self.clock.value = datetime(2026, 1, 2, 23, 6, tzinfo=timezone.utc)
        self.bot.send_once("job:2026-01-02:check", "Please sync", self.clock(), purpose="prompt",
                           effect={"kind": "prompt", "job_day": "2026-01-02", "job_kind": "check"})
        self.garmin.snapshot = self.make_snapshot(sync_at=self.clock() - timedelta(minutes=1))
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        again.daily_action("check", self.clock())
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertTrue(again.state["awaiting_sync"])
        self.assertEqual(again.state["jobs"]["2026-01-02"]["check"], "complete")

    def test_command_prompt_replay_restores_meaning_without_garmin_refetch(self):
        self.garmin.snapshot = self.make_snapshot({"totalSteps": 100})
        self.crash_after_confirmed_send(lambda: self.bot.process_update(self.update("calories"), self.clock()))
        recorded = self.store.load()
        self.assertEqual(recorded["deliveries"]["cmd:1"]["status"], "sent")
        self.assertIsNone(recorded["offset"])
        self.assertFalse(recorded["awaiting_sync"])
        self.garmin.snapshot = self.snapshot
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        self.assertTrue(again.state["awaiting_sync"])
        self.assertEqual(again.state["last_prompt_at"], recorded["deliveries"]["cmd:1"]["at"])
        prior_fetches = self.garmin.calls
        again.process_update(self.update("calories"), self.clock())
        self.assertEqual(self.garmin.calls, prior_fetches)
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertEqual(again.state["offset"], 2)

    def test_uncertain_daily_delivery_does_not_repeat_garmin_fetch(self):
        self.clock.value = datetime(2026, 1, 2, 23, 6, tzinfo=timezone.utc)
        self.garmin.snapshot = self.make_snapshot(sync_at=self.clock() - timedelta(minutes=1))
        self.telegram.failure = g.TelegramSendError(definite=False)
        self.bot.daily_action("check", self.clock())
        self.bot.daily_action("check", self.clock())
        self.assertEqual(self.garmin.calls, 1)
        self.assertNotEqual(self.bot.state["jobs"]["2026-01-02"].get("check"), "complete")

    def test_failed_send_does_not_advance_offset(self):
        self.telegram.updates = [self.update("status")]
        self.telegram.failure = g.TelegramSendError(definite=True)
        self.bot.run_cycle()
        self.assertIsNone(self.bot.state["offset"])
        self.assertEqual(self.bot.state["deliveries"]["cmd:1"]["status"], "pending")
        self.clock.advance(seconds=61)
        self.bot.run_cycle()
        self.assertEqual(self.bot.state["offset"], 2)
        self.assertEqual(len(self.telegram.messages), 1)

    def test_ambiguous_send_not_duplicated_after_restart(self):
        self.telegram.failure = g.TelegramSendError(definite=False)
        with self.assertRaises(g.TelegramSendError):
            self.bot.process_update(self.update("status"), self.clock())
        self.assertIsNone(self.bot.state["offset"])
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        with self.assertRaises(g.DeliveryUncertainError):
            again.process_update(self.update("status"), self.clock())
        self.assertEqual(self.telegram.messages, [])
        self.assertEqual(again.state["deliveries"]["cmd:1"]["status"], "uncertain")

    def test_sending_intent_remains_manual_after_crash(self):
        with patch.object(self.telegram, "send", side_effect=g.StatePersistenceError("synthetic crash")):
            with self.assertRaises(g.StatePersistenceError):
                self.bot.process_update(self.update("status"), self.clock())
        self.assertEqual(self.store.load()["deliveries"]["cmd:1"]["status"], "sending")
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        with self.assertRaises(g.DeliveryUncertainError):
            again.process_update(self.update("status"), self.clock())
        self.assertEqual(again.state["deliveries"]["cmd:1"]["status"], "sending")
        self.assertIsNone(again.state["offset"])
        self.assertEqual(self.telegram.messages, [])

    def test_sent_calories_replay_skips_garmin_and_advances_offset(self):
        self.command_crashes_before_offset_save("calories")
        self.assertEqual(self.store.load()["deliveries"]["cmd:1"]["status"], "sent")
        prior_fetches = self.garmin.calls
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        again.process_update(self.update("calories"), self.clock())
        again.process_update(self.update("calories"), self.clock())
        self.assertEqual(self.garmin.calls, prior_fetches)
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertEqual(again.state["offset"], 2)

    def test_sent_command_error_replay_skips_garmin(self):
        self.garmin.error = g.GarminFetchError("synthetic")
        self.command_crashes_before_offset_save("calories")
        prior_fetches = self.garmin.calls
        self.garmin.error = None
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        again.process_update(self.update("calories"), self.clock())
        self.assertEqual(self.garmin.calls, prior_fetches)
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertIn("temporarily unavailable", self.telegram.messages[0])
        self.assertEqual(again.state["offset"], 2)

    def test_sent_status_replay_advances_offset_without_duplicate(self):
        self.command_crashes_before_offset_save("status")
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        again.process_update(self.update("status"), self.clock())
        again.process_update(self.update("status"), self.clock())
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertEqual(again.state["offset"], 2)

    def test_sent_command_replay_advances_without_resend(self):
        self.bot.process_update(self.update("status"), self.clock())
        self.bot.state["offset"] = None
        self.bot.persist()
        again = g.Bot(self.config, self.store, self.garmin, self.telegram, self.logger, self.clock)
        again.process_update(self.update("status"), self.clock())
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertEqual(again.state["offset"], 2)

    def test_unauthorized_chat_is_ignored(self):
        self.bot.process_update(self.update("status", chat=99), self.clock())
        self.assertEqual(self.telegram.messages, [])
        self.assertEqual(self.bot.state["offset"], 2)

    def test_state_is_atomic_and_private(self):
        self.bot.state["offset"] = 12
        self.bot.persist()
        self.assertEqual(json.loads(self.store.path.read_text())["offset"], 12)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.glob(".state-*.tmp")), [])

    def test_failed_atomic_replace_keeps_previous_state(self):
        self.bot.state["offset"] = 12
        self.bot.persist()
        previous = self.store.path.read_bytes()
        self.bot.state["offset"] = 13
        with patch("garmin_cal_bot.os.replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(g.StatePersistenceError):
                self.bot.persist()
        self.assertEqual(self.store.path.read_bytes(), previous)
        self.assertEqual(list(self.root.glob(".state-*.tmp")), [])

    def test_insecure_existing_state_dir_is_rejected_without_chmod(self):
        directory = self.root / "shared"
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
        with self.assertRaises(PermissionError):
            g.ensure_private_directory(directory)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o755)

    def test_corrupt_state_recovers_defaults(self):
        self.store.path.write_text("{bad")
        self.assertEqual(self.store.load(), g.default_state())

    def test_token_redaction(self):
        text = f"failure https://api.telegram.org/bot{self.env['TELEGRAM_BOT_TOKEN']}/sendMessage {self.env['GARMIN_PASSWORD']}"
        safe = g.redact(text, (self.env["GARMIN_PASSWORD"],))
        self.assertNotIn(self.env["TELEGRAM_BOT_TOKEN"], safe)
        self.assertNotIn(self.env["GARMIN_PASSWORD"], safe)

    def test_telegram_adapter_send_and_poll_are_mocked(self):
        sender = g.TelegramClient(self.config)
        response = Mock(ok=True)
        response.json.return_value = {"ok": True}
        with patch("requests.post", return_value=response) as post:
            sender.send("synthetic message")
        self.assertEqual(post.call_args.kwargs["timeout"], 20)
        self.assertEqual(post.call_args.kwargs["json"]["chat_id"], "42")
        poll_response = Mock()
        poll_response.json.return_value = {"ok": True, "result": [self.update("status")]}
        with patch("requests.get", return_value=poll_response) as get:
            self.assertEqual(len(sender.poll(7)), 1)
        self.assertEqual(get.call_args.kwargs["params"]["offset"], 7)

    def test_telegram_adapter_network_failure_is_uncertain_and_safe(self):
        import requests
        sender = g.TelegramClient(self.config)
        error = requests.ConnectionError(f"https://api.telegram.org/bot{self.env['TELEGRAM_BOT_TOKEN']}/sendMessage")
        with patch("requests.post", side_effect=error):
            with self.assertRaises(g.TelegramSendError) as caught:
                sender.send("synthetic message")
        self.assertFalse(caught.exception.definite)
        self.assertNotIn(self.env["TELEGRAM_BOT_TOKEN"], str(caught.exception))

    def test_garmin_adapter_uses_expected_api_without_network(self):
        client = Mock()
        client.login.return_value = (None, None)
        client.get_user_summary.return_value = {"totalKilocalories": 2100, "activeKilocalories": 500,
                                                "bmrKilocalories": 1600}
        client.get_devices.return_value = [{"lastSyncTime": "2026-01-02T11:59:00Z"}]
        with patch("garminconnect.Garmin", return_value=client) as constructor:
            snap = g.GarminClient(self.config, self.logger).fetch(date(2026, 1, 2), self.clock())
        constructor.assert_called_once_with(self.env["GARMIN_EMAIL"], self.env["GARMIN_PASSWORD"])
        client.login.assert_called_once_with()
        client.get_user_summary.assert_called_once_with("2026-01-02")
        self.assertEqual(snap.calories.total, 2100)
        self.assertIsNotNone(snap.sync_at)

    def test_garmin_reuses_one_authenticated_client(self):
        client = Mock()
        client.login.return_value = (None, None)
        client.get_user_summary.return_value = {"totalKilocalories": 2100}
        client.get_devices.return_value = []
        adapter = g.GarminClient(self.config, self.logger)
        with patch.dict("os.environ", {"GARMINTOKENS": ""}), patch("garminconnect.Garmin", return_value=client) as constructor:
            adapter.fetch(date(2026, 1, 2), self.clock())
            adapter.fetch(date(2026, 1, 2), self.clock())
        constructor.assert_called_once()
        client.login.assert_called_once()
        self.assertEqual(client.get_user_summary.call_count, 2)

    def test_garmin_auth_failure_invalidates_session_then_reauthenticates(self):
        auth_error = type("GarminConnectAuthenticationError", (Exception,), {})
        first, second = Mock(), Mock()
        for client in (first, second):
            client.login.return_value = (None, None)
            client.get_user_summary.return_value = {"totalKilocalories": 2100}
            client.get_devices.return_value = []
        first.get_user_summary.side_effect = auth_error("synthetic")
        adapter = g.GarminClient(self.config, self.logger)
        with patch.dict("os.environ", {"GARMINTOKENS": ""}), patch("garminconnect.Garmin", side_effect=[first, second]) as constructor:
            with self.assertRaises(g.GarminAuthError):
                adapter.fetch(date(2026, 1, 2), self.clock())
            adapter.fetch(date(2026, 1, 2), self.clock())
        self.assertEqual(constructor.call_count, 2)
        first.login.assert_called_once()
        second.login.assert_called_once()

    def test_garmin_device_auth_failure_invalidates_session(self):
        auth_error = type("GarminConnectAuthenticationError", (Exception,), {})
        client = Mock()
        client.login.return_value = (None, None)
        client.get_user_summary.return_value = {"totalKilocalories": 2100}
        client.get_devices.side_effect = auth_error("synthetic")
        adapter = g.GarminClient(self.config, self.logger)
        with patch.dict("os.environ", {"GARMINTOKENS": ""}), patch("garminconnect.Garmin", return_value=client):
            with self.assertRaises(g.GarminAuthError):
                adapter.fetch(date(2026, 1, 2), self.clock())
        self.assertIsNone(adapter._client)

    def test_garmin_rate_limit_cooldown_prevents_login_storm(self):
        limited = type("GarminConnectTooManyRequestsError", (Exception,), {})
        client = Mock()
        client.login.return_value = (None, None)
        client.get_user_summary.side_effect = limited("synthetic")
        adapter = g.GarminClient(self.config, self.logger)
        self.bot.garmin = adapter
        with patch.dict("os.environ", {"GARMINTOKENS": ""}), patch("garminconnect.Garmin", return_value=client) as constructor:
            with self.assertRaises(g.GarminRateLimitError):
                self.bot.fetch_snapshot(self.clock())
            self.clock.advance(minutes=1)
            with self.assertRaises(g.GarminRateLimitError):
                self.bot.fetch_snapshot(self.clock())
        constructor.assert_called_once()
        client.login.assert_called_once()
        client.get_user_summary.assert_called_once()

    def test_garmin_login_rate_limit_cooldown_prevents_relogin(self):
        limited = type("GarminConnectTooManyRequestsError", (Exception,), {})
        client = Mock()
        client.login.side_effect = limited("synthetic")
        self.bot.garmin = g.GarminClient(self.config, self.logger)
        with patch.dict("os.environ", {"GARMINTOKENS": ""}), patch("garminconnect.Garmin", return_value=client) as constructor:
            with self.assertRaises(g.GarminRateLimitError):
                self.bot.fetch_snapshot(self.clock())
            self.clock.advance(minutes=1)
            with self.assertRaises(g.GarminRateLimitError):
                self.bot.fetch_snapshot(self.clock())
        constructor.assert_called_once()
        client.login.assert_called_once()

    def test_garmin_never_uses_upstream_token_file(self):
        token_path = self.root / "garmin_tokens.json"
        adapter = g.GarminClient(self.config, self.logger)
        with patch.dict("os.environ", {"GARMINTOKENS": str(token_path)}), patch("garminconnect.Garmin") as constructor:
            with self.assertRaisesRegex(g.GarminAuthError, "GARMINTOKENS"):
                adapter.fetch(date(2026, 1, 2), self.clock())
        constructor.assert_not_called()
        self.assertFalse(token_path.exists())

    def test_garmin_mfa_result_fails_without_caching_client(self):
        client = Mock()
        client.login.return_value = ("needs_mfa", None)
        adapter = g.GarminClient(self.config, self.logger)
        with patch.dict("os.environ", {"GARMINTOKENS": ""}), patch("garminconnect.Garmin", return_value=client):
            with self.assertRaisesRegex(g.GarminAuthError, "MFA"):
                adapter.fetch(date(2026, 1, 2), self.clock())
        self.assertIsNone(adapter._client)

    def test_garmin_mfa_exception_is_clear_and_does_not_cache_client(self):
        client = Mock()
        client.login.side_effect = RuntimeError("MFA Required but no prompt_mfa mechanism supplied")
        adapter = g.GarminClient(self.config, self.logger)
        with patch.dict("os.environ", {"GARMINTOKENS": ""}), patch("garminconnect.Garmin", return_value=client):
            with self.assertRaisesRegex(g.GarminAuthError, "MFA"):
                adapter.fetch(date(2026, 1, 2), self.clock())
        self.assertIsNone(adapter._client)

    def test_poll_failures_back_off_and_cap_then_success_resets(self):
        outcomes = [g.TelegramPollError("synthetic")] * 7 + [[], g.TelegramPollError("synthetic")]
        delays = []
        self.telegram.poll = Mock(side_effect=outcomes)
        def sleeper(seconds):
            delays.append(seconds)
            if len(delays) == len(outcomes):
                self.bot.stopping = True
        self.bot.sleeper = sleeper
        self.bot.run_forever()
        self.assertEqual(delays, [10, 20, 40, 80, 160, 300, 300, 1, 10])
        self.assertEqual(self.telegram.poll.call_count, len(outcomes))

    def test_logger_private_and_bounded(self):
        logger = g.make_logger(self.config)
        logger.info("secret %s", self.env["TELEGRAM_BOT_TOKEN"])
        handler = logger.handlers[0]
        handler.flush()
        self.assertEqual((self.root / "bot.log").stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.env["TELEGRAM_BOT_TOKEN"], (self.root / "bot.log").read_text())
        self.assertEqual(handler.maxBytes, 1_000_000)
        self.assertEqual(handler.backupCount, 3)
        handler.close()

    def test_startup_has_no_chat_notification(self):
        self.bot.run_cycle()
        self.assertEqual(self.telegram.messages, [])

    def test_local_clock_uses_configured_timezone(self):
        env = dict(self.env, BOT_TIMEZONE="Asia/Jerusalem")
        config = g.Config.from_env(env)
        instant = datetime(2026, 1, 2, 20, 31, tzinfo=timezone.utc)
        self.assertEqual(g.local_now(config.zone, lambda: instant).hour, 22)
        self.assertEqual(g.local_now(config.zone, lambda: instant).minute, 31)


if __name__ == "__main__":
    unittest.main()
