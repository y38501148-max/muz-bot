from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, FrozenSet, Sequence, Tuple

from x_monitor_core import (
    FetchBatch,
    XMonitorConfig,
    XPost,
    match_keywords,
    snowflake_created_at,
)


@dataclass(frozen=True)
class ProfileEntry:
    post_id: str
    card_text: str


async def discover_profile_entries(
    page: Any,
    username: str,
) -> Tuple[ProfileEntry, ...]:
    raw_entries = await page.locator("article").evaluate_all(
        """
        (articles, username) => {
          const escaped = username.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&");
          const statusPattern = new RegExp(
            "^/" + escaped + "/status/(\\\\d+)$",
            "i"
          );
          return articles
            .filter((article) => !article.parentElement?.closest("article"))
            .filter(
              (article) =>
                !article.innerText.toLocaleLowerCase().includes("replying to")
            )
            .map((article) => {
              const links = Array.from(article.querySelectorAll("a[href]"));
              const ownLink = links.find((link) => {
                const href = (link.getAttribute("href") || "").split("?")[0];
                return statusPattern.test(href);
              });
              if (!ownLink) {
                return null;
              }
              const href = (ownLink.getAttribute("href") || "").split("?")[0];
              const match = href.match(statusPattern);
              return match
                ? {post_id: match[1], card_text: article.innerText || ""}
                : null;
            })
            .filter(Boolean);
        }
        """,
        username,
    )
    seen_ids = set()
    entries = []
    for raw_entry in raw_entries:
        post_id = str(raw_entry.get("post_id", ""))
        if not post_id.isdigit() or post_id in seen_ids:
            continue
        seen_ids.add(post_id)
        entries.append(
            ProfileEntry(
                post_id=post_id,
                card_text=str(raw_entry.get("card_text", "")),
            )
        )
    return tuple(entries)


class PlaywrightXFetcher:
    def __init__(self, config: XMonitorConfig):
        self.config = config

    async def fetch_recent_posts(
        self,
        seen_ids: FrozenSet[str],
        now: datetime,
        lookback_hours: int,
    ) -> FetchBatch:
        from playwright.async_api import async_playwright

        timeout_ms = int(self.config.page_timeout_seconds * 1000)
        launch_options: Dict[str, Any] = {"headless": True}
        if self.config.proxy_url:
            launch_options["proxy"] = {"server": self.config.proxy_url}

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch_options)
            try:
                context = await browser.new_context(
                    locale="en-US",
                    timezone_id="UTC",
                )
                profile_page = await context.new_page()
                detail_page = await context.new_page()
                for page in (profile_page, detail_page):
                    page.set_default_timeout(timeout_ms)
                    await page.route(
                        "**/*",
                        lambda route: (
                            route.abort()
                            if route.request.resource_type
                            in {"image", "media", "font"}
                            else route.continue_()
                        ),
                    )
                entries = await self._load_profile_entries(
                    profile_page,
                    now,
                    lookback_hours,
                    timeout_ms,
                )
                return await self._load_post_details(
                    detail_page,
                    entries,
                    seen_ids,
                    now,
                    lookback_hours,
                    timeout_ms,
                )
            finally:
                await browser.close()

    async def _load_profile_entries(
        self,
        page: Any,
        now: datetime,
        lookback_hours: int,
        timeout_ms: int,
    ) -> Tuple[ProfileEntry, ...]:
        profile_url = f"https://x.com/{self.config.username}"
        await page.goto(
            profile_url,
            wait_until="commit",
            timeout=timeout_ms,
        )
        await page.locator("article").first.wait_for(
            state="attached",
            timeout=timeout_ms,
        )

        cutoff = now - timedelta(hours=lookback_hours)
        collected: Dict[str, ProfileEntry] = {}
        for _ in range(4):
            entries = await discover_profile_entries(
                page,
                self.config.username,
            )
            collected = {
                **collected,
                **{entry.post_id: entry for entry in entries},
            }
            if any(
                snowflake_created_at(entry.post_id) < cutoff
                for entry in collected.values()
            ):
                break
            await page.mouse.wheel(0, 4000)
            await page.wait_for_timeout(750)
        return tuple(collected.values())

    async def _load_post_details(
        self,
        page: Any,
        entries: Sequence[ProfileEntry],
        seen_ids: FrozenSet[str],
        now: datetime,
        lookback_hours: int,
        timeout_ms: int,
    ) -> FetchBatch:
        cutoff = now - timedelta(hours=lookback_hours)
        discovered_ids = []
        candidates = []
        errors = []
        has_cutoff_boundary = False
        for entry in entries:
            try:
                created_at = snowflake_created_at(entry.post_id)
            except ValueError as error:
                errors.append(f"无效帖子 ID {entry.post_id}: {error}")
                continue
            discovered_ids.append(entry.post_id)
            if created_at < cutoff:
                has_cutoff_boundary = True
                continue
            if created_at > now + timedelta(minutes=5):
                errors.append(f"帖子 {entry.post_id} 的时间戳位于未来")
                continue
            if entry.post_id not in seen_ids:
                candidates.append((entry.post_id, created_at))

        posts = []
        for post_id, created_at in sorted(
            candidates,
            key=lambda item: (item[1], item[0]),
        ):
            try:
                post = await self._load_single_post(
                    page,
                    post_id,
                    created_at,
                    timeout_ms,
                )
                posts.append(post)
            except Exception as error:
                errors.append(
                    f"读取帖子 {post_id} 失败: "
                    f"{error.__class__.__name__}: {str(error).splitlines()[0]}"
                )

        coverage_warning = None
        if entries and not has_cutoff_boundary:
            coverage_warning = (
                "X 未登录页面在出现 24 小时边界前停止展示帖子，"
                "首次回溯可能不完整"
            )
        return FetchBatch(
            discovered_ids=tuple(discovered_ids),
            posts=tuple(posts),
            coverage_warning=coverage_warning,
            errors=tuple(errors),
        )

    async def _load_single_post(
        self,
        page: Any,
        post_id: str,
        created_at: datetime,
        timeout_ms: int,
    ) -> XPost:
        url = (
            f"https://x.com/{self.config.username}/status/{post_id}"
        )
        await page.goto(url, wait_until="commit", timeout=timeout_ms)
        description = page.locator('meta[property="og:description"]')
        await description.wait_for(state="attached", timeout=timeout_ms)
        text = (await description.get_attribute("content") or "").strip()
        if not text:
            raise RuntimeError("帖子正文为空")
        return XPost(
            id=post_id,
            created_at=created_at,
            text=text,
            url=url,
            matched_keywords=match_keywords(
                text,
                self.config.keywords,
            ),
        )
