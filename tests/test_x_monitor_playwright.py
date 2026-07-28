import unittest
from datetime import datetime, timezone

from playwright.async_api import async_playwright

from x_monitor_core import XMonitorConfig
from x_monitor_fetcher import (
    PlaywrightXFetcher,
    ProfileEntry,
    discover_profile_entries,
)

PROFILE_HTML = """
<main>
  <article>
    <a href="/thsottiaux/status/300">1h</a>
    <div>Original reset announcement</div>
  </article>
  <article>
    <a href="/thsottiaux/status/299">2h</a>
    <div>Quoting another post</div>
    <article>
      <a href="/someone/status/100">quoted</a>
    </article>
  </article>
  <article>
    <div>Replying to @someone</div>
    <a href="/thsottiaux/status/298">3h</a>
  </article>
  <article>
    <div>Tibo reposted</div>
    <a href="/someone/status/97">4h</a>
  </article>
  <article>
    <a href="/thsottiaux/status/297">5h</a>
    <div>Quotes his own earlier post</div>
    <article>
      <a href="/thsottiaux/status/250">Jul 20</a>
    </article>
  </article>
</main>
"""


class ProfileExtractionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_extracts_original_and_quotes_but_not_replies_or_reposts(self):
        await self.page.set_content(PROFILE_HTML)

        entries = await discover_profile_entries(self.page, "thsottiaux")

        self.assertEqual(
            [entry.post_id for entry in entries],
            ["300", "299", "297"],
        )

    async def test_nested_quote_is_not_treated_as_an_independent_timeline_post(self):
        await self.page.set_content(PROFILE_HTML)

        entries = await discover_profile_entries(self.page, "thsottiaux")

        self.assertNotIn("250", [entry.post_id for entry in entries])

    async def test_fetches_profile_and_single_post_from_browser_metadata(self):
        newest_id = "2081940052154933696"
        older_id = "2081534792903147881"
        profile_url = "https://x.com/thsottiaux"
        detail_url = f"{profile_url}/status/{newest_id}"
        profile_html = f"""
        <article><a href="/thsottiaux/status/{newest_id}">1h</a></article>
        <article><a href="/thsottiaux/status/{older_id}">Jul 27</a></article>
        """
        detail_html = """
        <html><head>
          <meta property="og:description"
                content="Usage limits have been RESET." />
        </head></html>
        """

        async def route_handler(route):
            if route.request.url == profile_url:
                await route.fulfill(status=200, body=profile_html)
            elif route.request.url == detail_url:
                await route.fulfill(status=200, body=detail_html)
            else:
                await route.abort()

        await self.page.route("**/*", route_handler)
        fetcher = PlaywrightXFetcher(
            XMonitorConfig(proxy_url="", page_timeout_seconds=5)
        )
        now = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)

        entries = await fetcher._load_profile_entries(
            self.page,
            now,
            lookback_hours=24,
            timeout_ms=5000,
        )
        batch = await fetcher._load_post_details(
            self.page,
            entries,
            frozenset(),
            now,
            lookback_hours=24,
            timeout_ms=5000,
        )

        self.assertEqual(
            [entry.post_id for entry in entries],
            [newest_id, older_id],
        )
        self.assertEqual([post.id for post in batch.posts], [newest_id])
        self.assertEqual(batch.posts[0].matched_keywords, ("reset",))
        self.assertIsNone(batch.coverage_warning)
        self.assertEqual(batch.errors, ())

    async def test_seen_post_skips_detail_page_and_reports_backfill_cap(self):
        newest_id = "2081940052154933696"
        fetcher = PlaywrightXFetcher(
            XMonitorConfig(proxy_url="", page_timeout_seconds=1)
        )
        now = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)

        batch = await fetcher._load_post_details(
            self.page,
            (ProfileEntry(newest_id, "card"),),
            frozenset({newest_id}),
            now,
            lookback_hours=24,
            timeout_ms=1000,
        )

        self.assertEqual(batch.posts, ())
        self.assertIn("回溯可能不完整", batch.coverage_warning)

    async def test_detail_failure_is_reported_without_creating_post(self):
        newest_id = "2081940052154933696"

        async def route_handler(route):
            await route.fulfill(
                status=200,
                body="<html><head></head></html>",
            )

        await self.page.route("**/*", route_handler)
        fetcher = PlaywrightXFetcher(
            XMonitorConfig(proxy_url="", page_timeout_seconds=0.1)
        )
        now = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)

        batch = await fetcher._load_post_details(
            self.page,
            (ProfileEntry(newest_id, "card"),),
            frozenset(),
            now,
            lookback_hours=24,
            timeout_ms=100,
        )

        self.assertEqual(batch.posts, ())
        self.assertEqual(len(batch.errors), 1)
        self.assertIn(newest_id, batch.errors[0])


if __name__ == "__main__":
    unittest.main()
