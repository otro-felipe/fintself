from contextlib import nullcontext
from unittest.mock import MagicMock

import pytest

from fintself.scrapers.cl.bice import BiceScraper
from fintself.scrapers.cl.santander import SantanderScraper


@pytest.mark.parametrize("headless", [False, True])
def test_other_banks_keep_the_existing_browser_launch_options(headless):
    scraper = BiceScraper.__new__(BiceScraper)
    scraper.headless = headless
    scraper.slow_mo = 137

    assert scraper._browser_launch_options() == {
        "headless": headless,
        "slow_mo": 137,
    }


def test_visible_santander_keeps_the_existing_browser_launch_options():
    scraper = SantanderScraper.__new__(SantanderScraper)
    scraper.headless = False
    scraper.slow_mo = 137

    assert scraper._browser_launch_options() == {
        "headless": False,
        "slow_mo": 137,
    }


def test_headless_santander_uses_chromium_channel_with_bounded_renderer_memory():
    scraper = SantanderScraper.__new__(SantanderScraper)
    scraper.headless = True
    scraper.slow_mo = 137

    assert scraper._browser_launch_options() == {
        "headless": True,
        "slow_mo": 137,
        "channel": "chromium",
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--renderer-process-limit=1",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--disable-extensions",
            "--disable-sync",
            "--no-first-run",
        ],
    }


def test_scrape_passes_the_bank_specific_options_to_playwright(monkeypatch):
    playwright = MagicMock()
    browser = playwright.chromium.launch.return_value
    context = browser.new_context.return_value
    page = context.new_page.return_value
    monkeypatch.setattr(
        "fintself.scrapers.base.sync_playwright",
        lambda: nullcontext(playwright),
    )

    scraper = SantanderScraper(headless=True, debug_mode=False)
    scraper._login = MagicMock()
    scraper._scrape_movements = MagicMock(return_value=[])

    assert scraper.scrape(
        user="SENSITIVE_USER_SENTINEL",
        password="SENSITIVE_PASSWORD_SENTINEL",
    ) == []

    playwright.chromium.launch.assert_called_once_with(
        headless=True,
        slow_mo=scraper.slow_mo,
        channel="chromium",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--renderer-process-limit=1",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--disable-extensions",
            "--disable-sync",
            "--no-first-run",
        ],
    )
    page.set_default_timeout.assert_called_once_with(scraper.default_timeout)
