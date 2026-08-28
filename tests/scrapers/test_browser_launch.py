import pytest

from fintself.scrapers.cl.bice import BiceScraper


@pytest.mark.parametrize("headless", [False, True])
def test_other_banks_keep_the_existing_browser_launch_options(headless):
    scraper = BiceScraper.__new__(BiceScraper)
    scraper.headless = headless
    scraper.slow_mo = 137

    assert scraper._browser_launch_options() == {
        "headless": headless,
        "slow_mo": 137,
    }
