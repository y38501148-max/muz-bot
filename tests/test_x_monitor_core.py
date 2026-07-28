import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from x_monitor_core import (
    FetchBatch,
    JsonStateStore,
    MonitorState,
    StateLoadError,
    XMonitorConfig,
    XMonitorService,
    XPost,
    add_posts,
    can_manage_group,
    disable_group,
    enable_group,
    format_notification,
    format_status,
    load_monitor_config,
    match_keywords,
    prune_state,
    snowflake_created_at,
)

UTC = timezone.utc
NOW = datetime(2026, 7, 28, 4, 0, tzinfo=UTC)


def make_post(
    post_id: str,
    *,
    age_hours: float = 1,
    text: str = "The limits have been reset.",
    matched_keywords: tuple[str, ...] = ("reset",),
) -> XPost:
    return XPost(
        id=post_id,
        created_at=NOW - timedelta(hours=age_hours),
        text=text,
        url=f"https://x.com/thsottiaux/status/{post_id}",
        matched_keywords=matched_keywords,
    )


class SnowflakeAndMatchingTests(unittest.TestCase):
    def test_decodes_x_snowflake_timestamp_in_utc(self):
        created_at = snowflake_created_at("2081940052154933696")

        self.assertEqual(
            created_at,
            datetime(2026, 7, 28, 3, 9, 23, 666000, tzinfo=UTC),
        )

    def test_rejects_non_numeric_snowflake(self):
        with self.assertRaises(ValueError):
            snowflake_created_at("not-an-id")

    def test_matches_any_keyword_case_insensitively(self):
        matches = match_keywords(
            "Usage limits have been RESET for everyone",
            ("reset", "维护"),
        )

        self.assertEqual(matches, ("reset",))

    def test_matches_unicode_keywords_with_casefold(self):
        matches = match_keywords("STRASSE maintenance", ("Straße", "reset"))

        self.assertEqual(matches, ("Straße",))


class ImmutableStateTests(unittest.TestCase):
    def test_enabling_group_queues_cached_matches_within_24_hours(self):
        recent = make_post("101", age_hours=2)
        old = make_post("102", age_hours=25)
        state = MonitorState(posts=(recent, old))

        updated = enable_group(state, "12345", NOW, lookback_hours=24)

        self.assertEqual(state.enabled_group_ids, ())
        self.assertEqual(updated.enabled_group_ids, ("12345",))
        self.assertEqual(
            [(item.group_id, item.post_id) for item in updated.pending],
            [("12345", "101")],
        )

    def test_reenabling_group_does_not_duplicate_pending_delivery(self):
        state = enable_group(
            MonitorState(posts=(make_post("101"),)),
            "12345",
            NOW,
            lookback_hours=24,
        )

        updated = enable_group(state, "12345", NOW, lookback_hours=24)

        self.assertEqual(updated, state)

    def test_disabling_group_removes_pending_but_keeps_delivery_history(self):
        state = enable_group(
            MonitorState(posts=(make_post("101"),)),
            "12345",
            NOW,
            lookback_hours=24,
        )

        updated = disable_group(state, "12345")

        self.assertEqual(updated.enabled_group_ids, ())
        self.assertEqual(updated.pending, ())

    def test_new_matching_post_is_queued_for_each_enabled_group(self):
        state = MonitorState(enabled_group_ids=("100", "200"))

        updated, added = add_posts(state, (make_post("101"),))

        self.assertEqual(added, 1)
        self.assertEqual(
            {(item.group_id, item.post_id) for item in updated.pending},
            {("100", "101"), ("200", "101")},
        )

    def test_non_matching_post_is_stored_without_delivery(self):
        post = make_post(
            "101",
            text="A regular product update",
            matched_keywords=(),
        )

        updated, added = add_posts(
            MonitorState(enabled_group_ids=("100",)),
            (post,),
        )

        self.assertEqual(added, 1)
        self.assertEqual(updated.posts, (post,))
        self.assertEqual(updated.pending, ())

    def test_duplicate_post_is_not_added_or_requeued(self):
        post = make_post("101")
        original, _ = add_posts(
            MonitorState(enabled_group_ids=("100",)),
            (post,),
        )

        updated, added = add_posts(original, (post,))

        self.assertEqual(added, 0)
        self.assertEqual(updated, original)

    def test_pruning_keeps_48_hour_history_and_drops_expired_pending(self):
        recent = make_post("101", age_hours=23)
        retry_expired = make_post("102", age_hours=25)
        retention_expired = make_post("103", age_hours=49)
        state, _ = add_posts(
            MonitorState(enabled_group_ids=("100",)),
            (recent, retry_expired, retention_expired),
        )

        updated = prune_state(
            state,
            NOW,
            lookback_hours=24,
            retention_hours=48,
        )

        self.assertEqual({post.id for post in updated.posts}, {"101", "102"})
        self.assertEqual(
            {(item.group_id, item.post_id) for item in updated.pending},
            {("100", "101")},
        )


class JsonStateStoreTests(unittest.TestCase):
    def test_round_trips_state_across_restart(self):
        state, _ = add_posts(
            MonitorState(enabled_group_ids=("100",)),
            (make_post("101"),),
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = JsonStateStore(path)
            store.save(state)

            loaded = JsonStateStore(path).load()

        self.assertEqual(loaded, state)

    def test_missing_file_returns_empty_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "state.json")

            self.assertEqual(store.load(), MonitorState())

    def test_corrupt_file_raises_instead_of_resetting_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("{broken", encoding="utf-8")

            with self.assertRaises(StateLoadError):
                JsonStateStore(path).load()

            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_schema_validation_rejects_invalid_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                json.dumps({"version": 1, "enabled_group_ids": "100"}),
                encoding="utf-8",
            )

            with self.assertRaises(StateLoadError):
                JsonStateStore(path).load()

    def test_schema_validation_rejects_duplicate_pending_delivery(self):
        state, _ = add_posts(
            MonitorState(enabled_group_ids=("100",)),
            (make_post("101"),),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = JsonStateStore(path)
            store.save(state)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["pending"].append(raw["pending"][0])
            path.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaises(StateLoadError):
                store.load()


class FakeFetcher:
    def __init__(self, batches: list[FetchBatch]):
        self._batches = list(batches)
        self.seen_arguments: list[frozenset[str]] = []

    async def fetch_recent_posts(
        self,
        seen_ids: frozenset[str],
        now: datetime,
        lookback_hours: int,
    ) -> FetchBatch:
        self.seen_arguments.append(seen_ids)
        return self._batches.pop(0)


class MonitorServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = JsonStateStore(
            Path(self.temporary_directory.name) / "state.json"
        )
        self.config = XMonitorConfig(
            username="thsottiaux",
            keywords=("reset",),
            proxy_url="http://127.0.0.1:7890",
            poll_minutes=10,
            lookback_hours=24,
            page_timeout_seconds=30,
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    async def test_skips_browser_when_no_group_is_enabled(self):
        fetcher = FakeFetcher([])
        service = XMonitorService(
            self.config,
            self.store,
            fetcher,
            self._successful_sender,
        )

        report = await service.poll(NOW)

        self.assertTrue(report.skipped)
        self.assertEqual(fetcher.seen_arguments, [])

    async def test_seen_post_is_passed_to_fetcher_and_not_reprocessed(self):
        existing = make_post("101", matched_keywords=())
        self.store.save(
            MonitorState(
                enabled_group_ids=("100",),
                posts=(existing,),
            )
        )
        fetcher = FakeFetcher(
            [
                FetchBatch(
                    discovered_ids=("101",),
                    posts=(),
                )
            ]
        )
        service = XMonitorService(
            self.config,
            self.store,
            fetcher,
            self._successful_sender,
        )

        report = await service.poll(NOW)

        self.assertEqual(fetcher.seen_arguments, [frozenset({"101"})])
        self.assertEqual(report.new_posts, 0)
        self.assertEqual(self.store.load().posts, (existing,))

    async def test_matching_posts_are_sent_oldest_first_once(self):
        sent: list[tuple[str, str]] = []

        async def sender(group_id: str, message: str):
            sent.append((group_id, message))

        self.store.save(MonitorState(enabled_group_ids=("100",)))
        newer = make_post("202", age_hours=1)
        older = make_post("201", age_hours=2)
        fetcher = FakeFetcher(
            [
                FetchBatch(
                    discovered_ids=("202", "201"),
                    posts=(newer, older),
                ),
                FetchBatch(
                    discovered_ids=("202", "201"),
                    posts=(),
                ),
            ]
        )
        service = XMonitorService(self.config, self.store, fetcher, sender)

        first_report = await service.poll(NOW)
        second_report = await service.poll(NOW + timedelta(minutes=10))

        sent_post_ids = [
            message.split("/status/")[-1] for _, message in sent
        ]
        self.assertEqual(sent_post_ids, ["201", "202"])
        self.assertEqual(first_report.sent, 2)
        self.assertEqual(first_report.matched, 2)
        self.assertEqual(second_report.sent, 0)
        self.assertEqual(
            fetcher.seen_arguments[1],
            frozenset({"201", "202"}),
        )

    async def test_failed_delivery_retries_without_refetching_post_detail(self):
        attempts = 0

        async def flaky_sender(group_id: str, message: str):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("QQ unavailable")

        self.store.save(MonitorState(enabled_group_ids=("100",)))
        post = make_post("101")
        fetcher = FakeFetcher(
            [
                FetchBatch(discovered_ids=("101",), posts=(post,)),
                FetchBatch(discovered_ids=("101",), posts=()),
            ]
        )
        service = XMonitorService(
            self.config,
            self.store,
            fetcher,
            flaky_sender,
        )

        first_report = await service.poll(NOW)
        second_report = await service.poll(NOW + timedelta(minutes=10))

        self.assertEqual(first_report.failed, 1)
        self.assertEqual(second_report.sent, 1)
        self.assertEqual(fetcher.seen_arguments[1], frozenset({"101"}))
        self.assertEqual(self.store.load().pending, ())

    async def test_fetch_failure_preserves_posts_and_pending_state(self):
        original, _ = add_posts(
            MonitorState(enabled_group_ids=("100",)),
            (make_post("101"),),
        )
        self.store.save(original)

        class FailingFetcher:
            async def fetch_recent_posts(self, seen_ids, now, lookback_hours):
                raise RuntimeError("X timeout")

        service = XMonitorService(
            self.config,
            self.store,
            FailingFetcher(),
            self._successful_sender,
        )

        report = await service.poll(NOW)
        loaded = self.store.load()

        self.assertEqual(report.failed, 1)
        self.assertEqual(loaded.posts, original.posts)
        self.assertEqual(loaded.pending, original.pending)
        self.assertIn("X timeout", loaded.last_error)

    async def _successful_sender(self, group_id: str, message: str):
        return None


class NotificationTests(unittest.TestCase):
    def test_notification_contains_keyword_time_text_and_canonical_url(self):
        message = format_notification(make_post("101"))

        self.assertIn("关键词「reset」", message)
        self.assertIn("2026-07-28 11:00（北京时间）", message)
        self.assertIn("The limits have been reset.", message)
        self.assertTrue(message.endswith("https://x.com/thsottiaux/status/101"))


class ConfigurationAndCommandTests(unittest.TestCase):
    def test_loads_runtime_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "USERNAME": "thsottiaux",
                        "KEYWORDS": ["reset", "limit"],
                        "PROXY_URL": "http://127.0.0.1:7890",
                        "POLL_MINUTES": 10,
                        "LOOKBACK_HOURS": 24,
                        "PAGE_TIMEOUT_SECONDS": 30,
                    }
                ),
                encoding="utf-8",
            )

            config = load_monitor_config(path)

        self.assertEqual(config.keywords, ("reset", "limit"))
        self.assertEqual(config.poll_minutes, 10)

    def test_rejects_empty_keywords(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"KEYWORDS": []}', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "KEYWORDS"):
                load_monitor_config(path)

    def test_rejects_non_string_keyword(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"KEYWORDS": ["reset", 123]}', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "KEYWORDS"):
                load_monitor_config(path)

    def test_group_owner_admin_and_superuser_can_manage_subscription(self):
        self.assertTrue(can_manage_group("1", "owner", frozenset()))
        self.assertTrue(can_manage_group("2", "admin", frozenset()))
        self.assertTrue(
            can_manage_group("3", "member", frozenset({"3"}))
        )
        self.assertFalse(
            can_manage_group("4", "member", frozenset({"3"}))
        )

    def test_status_reports_group_state_and_last_error(self):
        state = MonitorState(
            enabled_group_ids=("100",),
            last_success_at=NOW,
            last_error="X timeout",
        )
        config = XMonitorConfig()

        message = format_status(config, state, "100")

        self.assertIn("@thsottiaux", message)
        self.assertIn("reset", message)
        self.assertIn("10 分钟", message)
        self.assertIn("当前群：已开启", message)
        self.assertIn("X timeout", message)


if __name__ == "__main__":
    unittest.main()
