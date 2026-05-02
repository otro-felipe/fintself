"""Tests for the Banco Bice scraper."""

from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from fintself.core.exceptions import DataExtractionError
from fintself.scrapers.cl.bice import BiceScraper


@pytest.fixture
def scraper() -> BiceScraper:
    s = BiceScraper.__new__(BiceScraper)
    s.debug_mode = False
    s.default_timeout = 30000
    s.min_human_delay_ms = 0
    s.max_human_delay_ms = 0
    s.user = None
    s.password = None
    s.playwright = None
    s.browser = None
    s.page = MagicMock()
    return s


# ─── Parser tests ─────────────────────────────────────────────────────────────


class TestParseBiceAmount:
    def test_dot_thousands(self, scraper):
        assert scraper._parse_bice_amount("1.000") == "1000"

    def test_plain_integer(self, scraper):
        assert scraper._parse_bice_amount("500") == "500"

    def test_large_amount(self, scraper):
        assert scraper._parse_bice_amount("1.234.567") == "1234567"

    def test_negative(self, scraper):
        assert scraper._parse_bice_amount("-50.000") == "-50000"

    def test_empty_string(self, scraper):
        assert scraper._parse_bice_amount("") == "0"

    def test_strips_currency_symbols(self, scraper):
        assert scraper._parse_bice_amount("$ 1.000 CLP") == "1000"


class TestParseIntlAmount:
    def test_decimal_usd(self, scraper):
        assert scraper._parse_intl_amount("4,70 US$") == "4,70"

    def test_larger_decimal_usd(self, scraper):
        assert scraper._parse_intl_amount("1.234,56 US$") == "1.234,56"

    def test_negative_decimal_usd(self, scraper):
        assert scraper._parse_intl_amount("-12,50 US$") == "-12,50"

    def test_whole_number_usd(self, scraper):
        assert scraper._parse_intl_amount("100 US$") == "100"


class TestParseBiceDate:
    @pytest.mark.parametrize(
        "date_str,expected",
        [
            ("1 ene 2025", datetime(2025, 1, 1)),
            ("14 feb 2025", datetime(2025, 2, 14)),
            ("15 mar 2025", datetime(2025, 3, 15)),
            ("30 abr 2024", datetime(2024, 4, 30)),
            ("10 may 2024", datetime(2024, 5, 10)),
            ("20 jun 2024", datetime(2024, 6, 20)),
            ("5 jul 2024", datetime(2024, 7, 5)),
            ("8 ago 2024", datetime(2024, 8, 8)),
            ("27 sep 2025", datetime(2025, 9, 27)),
            ("27 sept 2025", datetime(2025, 9, 27)),
            ("3 oct 2024", datetime(2024, 10, 3)),
            ("12 nov 2024", datetime(2024, 11, 12)),
            ("31 dic 2024", datetime(2024, 12, 31)),
            ("25 feb 2026", datetime(2026, 2, 25)),
        ],
    )
    def test_months(self, scraper, date_str, expected):
        assert scraper._parse_bice_date(date_str) == expected

    def test_invalid_format_returns_none(self, scraper):
        assert scraper._parse_bice_date("2025-01-15") is None

    def test_incomplete_parts_returns_none(self, scraper):
        assert scraper._parse_bice_date("25 feb") is None


# ─── Debit card extraction helpers & tests ────────────────────────────────────


def _make_debit_row(
    date_str: str, type_str: str, description: str, amount_str: str
) -> MagicMock:
    """Builds a mock for a debit card table row."""
    date_loc = MagicMock()
    date_loc.inner_text.return_value = date_str

    type_loc = MagicMock()
    type_loc.inner_text.return_value = type_str

    desc_loc = MagicMock()
    desc_loc.inner_text.return_value = description

    amount_loc = MagicMock()
    amount_loc.inner_text.return_value = amount_str

    row = MagicMock()

    def _row_locator(sel):
        if "nth-child(1)" in sel:
            return date_loc
        if "nth-child(2)" in sel:
            return type_loc
        if "nth-child(3)" in sel:
            return desc_loc
        if "transaction-table__amount" in sel and "nth-child" not in sel:
            return amount_loc
        return MagicMock()

    row.locator.side_effect = _row_locator
    return row


def _setup_debit_page(scraper: BiceScraper, rows: list) -> None:
    """Configures the page mock for _extract_debit_card_movements."""
    page = scraper.page
    scraper._wait_for_selector = MagicMock()

    row_selector = "tbody.table-body tr"
    next_button_selector = 'ds-button[label="Siguiente"] button'

    rows_locator = MagicMock()
    rows_locator.all.return_value = rows

    next_button = MagicMock()
    next_button.is_visible.return_value = False

    def _locator(sel):
        if sel == row_selector:
            return rows_locator
        if sel == next_button_selector:
            return next_button
        return MagicMock()

    page.locator.side_effect = _locator


class TestExtractDebitCardMovements:
    def test_cargo_is_negative(self, scraper):
        cell = _make_debit_row("15 mar 2025", "Cargo", "Supermercado Ejemplo", "50.000")
        _setup_debit_page(scraper, [cell])
        movements = scraper._extract_debit_card_movements()
        assert len(movements) == 1
        assert movements[0].amount == Decimal("-50000")
        assert movements[0].currency == "CLP"
        assert movements[0].account_type == "corriente"

    def test_abono_is_positive(self, scraper):
        cell = _make_debit_row(
            "10 ene 2025", "Abono", "Transferencia recibida", "100.000"
        )
        _setup_debit_page(scraper, [cell])
        movements = scraper._extract_debit_card_movements()
        assert len(movements) == 1
        assert movements[0].amount == Decimal("100000")
        assert movements[0].transaction_type == "Abono"

    def test_empty_page_returns_empty(self, scraper):
        scraper._wait_for_selector = MagicMock(
            side_effect=DataExtractionError("timeout")
        )
        movements = scraper._extract_debit_card_movements()
        assert movements == []

    def test_zero_amount_is_skipped(self, scraper):
        cell = _make_debit_row("15 mar 2025", "Cargo", "Anulado", "0")
        _setup_debit_page(scraper, [cell])
        movements = scraper._extract_debit_card_movements()
        assert movements == []


# ─── Unbilled national credit helpers & tests ─────────────────────────────────


def _make_credit_row(
    date_str: str,
    amount_str: str,
    row_class: str,
    category_name: str = "Categoria",
    category_desc: str = "SubCat",
    transaction_detail: str = "Descripcion",
) -> MagicMock:
    """Builds a mock for a national credit card transaction row."""
    date_loc = MagicMock()
    date_loc.inner_text.return_value = date_str

    amount_loc = MagicMock()
    amount_loc.inner_text.return_value = amount_str

    class_loc = MagicMock()
    class_loc.get_attribute.return_value = row_class

    cat_name_loc = MagicMock()
    cat_name_loc.inner_text.return_value = category_name

    cat_desc_loc = MagicMock()
    cat_desc_loc.inner_text.return_value = category_desc

    detail_loc = MagicMock()
    detail_loc.inner_text.return_value = transaction_detail

    installments_loc = MagicMock()
    installments_loc.inner_text.return_value = ""

    def _row_locator(sel):
        if sel == ".date":
            return date_loc
        if sel == ".transaction-amount":
            return amount_loc
        if sel == "div.wm-100":
            return class_loc
        if sel == ".category-name":
            return cat_name_loc
        if sel == ".category-description":
            return cat_desc_loc
        if sel == ".transaction-detail":
            return detail_loc
        if sel == ".transaction-installments":
            return installments_loc
        return MagicMock()

    row = MagicMock()
    row.locator.side_effect = _row_locator
    return row


def _setup_unbilled_national_page(scraper: BiceScraper, rows: list) -> None:
    """Configures the page mock for _extract_unbilled_national_credit_movements."""
    page = scraper.page
    scraper._wait_for_selector = MagicMock()

    row_selector = "app-transaction-row div.row.transaction"
    rows_locator = MagicMock()
    rows_locator.all.return_value = rows

    page.locator.side_effect = (
        lambda sel: rows_locator if sel == row_selector else MagicMock()
    )
    page.wait_for_selector.return_value = None


class TestExtractUnbilledNationalCreditMovements:
    def test_cargo_is_negative(self, scraper):
        row = _make_credit_row(
            "20 mar 2025", "50.000", "cargos transaction", transaction_detail="Netflix"
        )
        _setup_unbilled_national_page(scraper, [row])
        movements = scraper._extract_unbilled_national_credit_movements("4920")
        assert len(movements) == 1
        assert movements[0].amount == Decimal("-50000")
        assert movements[0].currency == "CLP"
        assert movements[0].account_type == "credito"
        assert movements[0].account_id == "4920"
        assert movements[0].transaction_type == "Cargo"

    def test_abono_is_positive(self, scraper):
        row = _make_credit_row(
            "10 feb 2025", "10.000", "abonos transaction", transaction_detail="Reverso"
        )
        _setup_unbilled_national_page(scraper, [row])
        movements = scraper._extract_unbilled_national_credit_movements("4920")
        assert len(movements) == 1
        assert movements[0].amount == Decimal("10000")
        assert movements[0].transaction_type == "Abono"

    def test_no_rows_returns_empty(self, scraper):
        scraper.page.wait_for_selector.side_effect = PlaywrightTimeoutError("timeout")
        movements = scraper._extract_unbilled_national_credit_movements("4920")
        assert movements == []

    def test_zero_amount_is_skipped(self, scraper):
        row = _make_credit_row("20 mar 2025", "0", "cargos transaction")
        _setup_unbilled_national_page(scraper, [row])
        movements = scraper._extract_unbilled_national_credit_movements("4920")
        assert movements == []


# ─── Billed national credit helpers & tests ───────────────────────────────────


def _make_accordion(
    period_label: str, rows: list, is_expanded: bool = False
) -> MagicMock:
    """Builds a mock for a billing period accordion."""
    period_strong = MagicMock()
    period_strong.inner_text.return_value = period_label

    toggle_btn = MagicMock()
    toggle_btn.get_attribute.return_value = "true" if is_expanded else "false"

    rows_locator = MagicMock()
    rows_locator.all.return_value = rows

    def _acc_locator(sel):
        if sel == ".period-accordion-header strong":
            return period_strong
        if sel == ".card-header button":
            return toggle_btn
        if sel == "app-transaction-row div.row.transaction":
            return rows_locator
        return MagicMock()

    acc = MagicMock()
    acc.locator.side_effect = _acc_locator
    return acc


def _setup_billed_national_page(scraper: BiceScraper, accordions: list) -> None:
    """Configures the page mock for _extract_billed_national_credit_movements."""
    page = scraper.page
    scraper._click = MagicMock()
    scraper._wait_for_selector = MagicMock()

    acc_locator = MagicMock()
    acc_locator.count.return_value = len(accordions)
    acc_locator.nth.side_effect = lambda i: accordions[i]

    page.locator.side_effect = (
        lambda sel: acc_locator if sel == "[id^='acc-']" else MagicMock()
    )


class TestExtractBilledNationalCreditMovements:
    def test_cargo_in_accordion(self, scraper):
        row = _make_credit_row(
            "20 feb 2026", "15.000", "cargos transaction", transaction_detail="Farmacia"
        )
        acc = _make_accordion("25 feb 2026 - 26 mar 2026", [row])
        _setup_billed_national_page(scraper, [acc])
        movements = scraper._extract_billed_national_credit_movements("4920")
        assert len(movements) == 1
        assert movements[0].amount == Decimal("-15000")
        assert movements[0].raw_data["billing_period"] == "25 feb 2026 - 26 mar 2026"

    def test_abono_in_accordion(self, scraper):
        # Billed abonos have a negative amount in HTML (credit to the card)
        row = _make_credit_row(
            "10 feb 2026", "-5.000", "abonos transaction", transaction_detail="Reverso"
        )
        acc = _make_accordion("25 feb 2026 - 26 mar 2026", [row])
        _setup_billed_national_page(scraper, [acc])
        movements = scraper._extract_billed_national_credit_movements("4920")
        assert len(movements) == 1
        assert movements[0].amount == Decimal("5000")

    def test_no_accordions_returns_empty(self, scraper):
        scraper._click = MagicMock()
        scraper._wait_for_selector = MagicMock(
            side_effect=DataExtractionError("timeout")
        )
        movements = scraper._extract_billed_national_credit_movements("4920")
        assert movements == []

    def test_multiple_accordions(self, scraper):
        row1 = _make_credit_row("20 feb 2026", "10.000", "cargos transaction")
        row2 = _make_credit_row("15 ene 2026", "20.000", "cargos transaction")
        acc1 = _make_accordion("periodo 1", [row1])
        acc2 = _make_accordion("periodo 2", [row2])
        _setup_billed_national_page(scraper, [acc1, acc2])
        movements = scraper._extract_billed_national_credit_movements("4920")
        assert len(movements) == 2


# ─── Unbilled international credit helpers & tests ────────────────────────────


def _setup_unbilled_intl_page(scraper: BiceScraper, rows: list) -> None:
    """Configures the page mock for _extract_unbilled_intl_credit_movements."""
    page = scraper.page
    scraper._click = MagicMock()

    row_selector = "app-transaction-row div.row.transaction"
    rows_locator = MagicMock()
    rows_locator.all.return_value = rows

    page.locator.side_effect = (
        lambda sel: rows_locator if sel == row_selector else MagicMock()
    )
    page.wait_for_selector.return_value = None


class TestExtractUnbilledIntlCreditMovements:
    def test_usd_cargo_is_negative(self, scraper):
        row = _make_credit_row(
            "20 mar 2025",
            "4,70 US$",
            "cargos transaction",
            transaction_detail="Netflix US",
        )
        _setup_unbilled_intl_page(scraper, [row])
        movements = scraper._extract_unbilled_intl_credit_movements("4920")
        assert len(movements) == 1
        assert movements[0].amount == Decimal("-4.70")
        assert movements[0].currency == "USD"
        assert movements[0].transaction_type == "Cargo"

    def test_usd_abono_is_positive(self, scraper):
        row = _make_credit_row(
            "10 feb 2025",
            "-1,50 US$",
            "abonos transaction",
            transaction_detail="Reverso",
        )
        _setup_unbilled_intl_page(scraper, [row])
        movements = scraper._extract_unbilled_intl_credit_movements("4920")
        assert len(movements) == 1
        assert movements[0].amount == Decimal("1.50")
        assert movements[0].currency == "USD"

    def test_no_rows_returns_empty(self, scraper):
        scraper._click = MagicMock()
        scraper.page.wait_for_selector.side_effect = PlaywrightTimeoutError("timeout")
        movements = scraper._extract_unbilled_intl_credit_movements("4920")
        assert movements == []


# ─── Billed international credit helpers & tests ──────────────────────────────


class TestExtractBilledIntlCreditMovements:
    def test_usd_cargo_in_accordion(self, scraper):
        row = _make_credit_row(
            "20 feb 2026",
            "12,50 US$",
            "cargos transaction",
            transaction_detail="Amazon",
        )
        acc = _make_accordion("25 feb 2026 - 26 mar 2026", [row])
        _setup_billed_national_page(scraper, [acc])
        movements = scraper._extract_billed_intl_credit_movements("4920")
        assert len(movements) == 1
        assert movements[0].amount == Decimal("-12.50")
        assert movements[0].currency == "USD"
        assert movements[0].raw_data["billing_period"] == "25 feb 2026 - 26 mar 2026"

    def test_usd_abono_in_accordion(self, scraper):
        row = _make_credit_row(
            "10 feb 2026",
            "-8,00 US$",
            "abonos transaction",
            transaction_detail="Reverso",
        )
        acc = _make_accordion("25 feb 2026 - 26 mar 2026", [row])
        _setup_billed_national_page(scraper, [acc])
        movements = scraper._extract_billed_intl_credit_movements("4920")
        assert len(movements) == 1
        assert movements[0].amount == Decimal("8.00")
        assert movements[0].currency == "USD"

    def test_no_accordions_returns_empty(self, scraper):
        scraper._click = MagicMock()
        scraper._wait_for_selector = MagicMock(
            side_effect=DataExtractionError("timeout")
        )
        movements = scraper._extract_billed_intl_credit_movements("4920")
        assert movements == []
