from typing import List, Optional
import re
from datetime import datetime

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect

from fintself.core.exceptions import DataExtractionError, LoginError
from fintself.core.models import MovementModel
from fintself.scrapers.base import BaseScraper
from fintself.utils.logging import logger
from fintself.utils.parsers import parse_chilean_amount, parse_chilean_date


class BiceScraper(BaseScraper):
    """Scraper to extract movements from Banco Bice (credit and debit card)."""

    LOGIN_URL = "https://banco.bice.cl/personas"

    def _get_bank_id(self) -> str:
        return "cl_bice"

    def _parse_bice_amount(self, bice_amount: str) -> str:
        """Parser for specific Bice amount format."""
        clean_amount = re.sub(r"[^\d-]", "", bice_amount)

        if not clean_amount:
            return "0"

        return clean_amount

    def _parse_intl_amount(self, amount_str: str) -> str:
        """Parser for international USD amount format like '4,70 US$'."""
        return re.sub(r"[^\d,.-]", "", amount_str)

    def _parse_bice_date(self, date_str: str) -> Optional[datetime]:
        """Parser for specific Bice date format."""
        month_map = {
            "ene": "01",
            "feb": "02",
            "mar": "03",
            "abr": "04",
            "may": "05",
            "jun": "06",
            "jul": "07",
            "ago": "08",
            "sep": "09",
            "sept": "09",
            "oct": "10",
            "nov": "11",
            "dic": "12",
        }

        try:
            parts = date_str.lower().split(" ")
            if len(parts) != 3:
                logger.warning(f"Unexpected date format: {date_str}")
                return None

            day = parts[0]
            month = month_map.get(parts[1])
            year = parts[2]

            formatted_date_str = f"{day}/{month}/{year}"

            return parse_chilean_date(formatted_date_str)

        except Exception as e:
            logger.error(f"Error parsing date '{date_str}': {e}")
            return None

    def _login(self) -> None:
        """Performs the login for Banco Bice."""
        assert self.user is not None, "User must be provided"
        assert self.password is not None, "Password must be provided"

        page = self._ensure_page()
        logger.info("Logging into Banco Bice.")

        self._navigate(self.LOGIN_URL)
        self._save_debug_info("01_main_page")
        logger.info("Clicking login dropdown menu.")

        dropdown_selector = "#login-dropdown"
        self._click(dropdown_selector)
        page.wait_for_timeout(1000)
        self._save_debug_info("02_dropdown_clicked")
        logger.info("Clicking 'Personas' to navigate to auth page.")

        personas_selector = 'a[data-click="Personas"]'
        self._click(personas_selector)

        rut_selector = "#username"
        password_selector = "#password"

        try:
            self._wait_for_selector(rut_selector, timeout_override=30000)
            self._save_debug_info("03_auth_page_loaded")
        except (PlaywrightTimeoutError, DataExtractionError):
            self._save_debug_info("auth_page_failed_to_load")
            raise LoginError("Failed to load the credential page")

        logger.info("Entering credentials.")
        self._type(rut_selector, self.user, delay=100)
        self._type(password_selector, self.password, delay=100)
        self._save_debug_info("04_credentials_entered")

        logger.info("Submitting login form.")
        submit_selector = "#kc-login"
        self._click(submit_selector)

        try:
            success_selector = "#edit-name-txt"
            self._wait_for_selector(success_selector, timeout_override=30000)
            self._save_debug_info("05_login_success")
            logger.info("Login to Bice successful.")
        except (PlaywrightTimeoutError, DataExtractionError):
            self._save_debug_info("login_failed")
            raise LoginError("Timeout or error after login to Bice. Check credentials.")

    def _close_popups(self) -> None:
        """Closes any visible popup (marketing banners, NPS surveys, etc.)."""
        page = self._ensure_page()
        logger.info("Checking for popups.")

        popups = [
            ("marketing popup", "button:has-text('Cerrar X')"),
            (
                "Qualtrics survey",
                "div[class*='QSIWebResponsive'] button[aria-label='Cerrar']",
            ),
        ]

        for name, selector in popups:
            try:
                close_button = page.locator(selector)
                if close_button.is_visible(timeout=3000):
                    logger.info(f"{name.capitalize()} detected. Closing...")
                    self._click(close_button)
                    page.wait_for_timeout(500)
                    logger.info(f"{name.capitalize()} closed.")
            except Exception as e:
                logger.warning(f"Ignored error while checking {name}: {e}")

    def _scrape_movements(self) -> List[MovementModel]:
        """Orchestrates the extraction of debit and credit card movements."""
        logger.info("Starting movements scrapping.")

        try:
            page = self._ensure_page()
            page.wait_for_timeout(5000)
            self._close_popups()
            debit_movements_button_container = page.locator("#collapseCCT0")
            debit_card_movements_button = debit_movements_button_container.locator(
                "a.ultimosMov"
            )
            self._click(debit_card_movements_button)
            debit_movements_table_selector = "tbody.table-body"
            self._wait_for_selector(
                debit_movements_table_selector, timeout_override=30000
            )

            self._save_debug_info("09_debit_card_movements_page")

        except (PlaywrightTimeoutError, DataExtractionError) as e:
            self._save_debug_info("navigate_to_debit_failed")
            raise DataExtractionError(
                f"Failed to navigate to debit card movements: {e}"
            )

        debit_card_movements = self._extract_debit_card_movements()

        logger.info("Navigating back to main dashboard for credit card extraction.")

        self._close_popups()

        try:
            volver_button_selector = "button:has-text('Volver')"
            self._click(volver_button_selector)
            success_selector = "#edit-name-txt"
            self._wait_for_selector(success_selector, timeout_override=30000)
            logger.info("Successfully navigated back to dashboard.")
            self._save_debug_info("08_back_to_dashboard")

        except (PlaywrightTimeoutError, DataExtractionError) as e:
            self._save_debug_info("navigate_back_failed")
            raise DataExtractionError(f"Failed to navigate back to dashboard: {e}")

        try:
            page = self._ensure_page()
            deployer_cuenta_corriente_container = page.locator("#headingOne")
            deployer_cuenta_corriente = deployer_cuenta_corriente_container.locator(
                "button"
            )
            self._click(deployer_cuenta_corriente)
            self._save_debug_info("09_dashboard_no_containers_selected")
            self._human_delay(min_override_ms=5000)
            deployer_credit_card_container = page.locator("#headingTwo")
            deployer_credit_card = deployer_credit_card_container.locator("button")
            self._click(deployer_credit_card)
            self._save_debug_info("10_dashboard_credit_card_container_selected")
            credit_movements_button_container = page.locator("#collapseTC0")
            credit_card_movements_button = credit_movements_button_container.locator(
                "a.ultimosMov"
            )
            self._wait_for_selector(
                credit_card_movements_button, timeout_override=10000
            )
            self._click(credit_card_movements_button)

        except (PlaywrightTimeoutError, DataExtractionError) as e:
            self._save_debug_info("navigating_to_credit_card_movements_failed")
            raise DataExtractionError(f"Failed jumping into credit card container: {e}")

        logger.info("Navigating to Credit Card movements.")

        try:
            credit_movements_container_selector = "#tab0"
            self._wait_for_selector(
                credit_movements_container_selector, timeout_override=30000
            )

            self._save_debug_info("11_credit_card_movements_page")

        except (PlaywrightTimeoutError, DataExtractionError) as e:
            self._save_debug_info("navigate_to_credit_failed")
            raise DataExtractionError(
                f"Failed to navigate to credit card movements: {e}"
            )

        credit_card_movements = self._extract_credit_card_movements()

        all_movements = debit_card_movements + credit_card_movements

        logger.info(
            f"Scraping completed. Total movements extracted: {len(all_movements)}"
        )

        return all_movements

    def _extract_debit_card_movements(self) -> List[MovementModel]:
        """Extracts movements made by debit card from all pages."""
        page = self._ensure_page()
        logger.info("Starting debit card movements extraction.")

        all_debit_movements: List[MovementModel] = []

        logger.info("Extracting movements from table (pagination loop).")
        page_num = 1

        while True:
            logger.info(f"--- Scraping Page {page_num} ---")

            movements_on_page: List[MovementModel] = []

            row_selector = "tbody.table-body tr"

            try:
                self._wait_for_selector(
                    f"{row_selector} >> nth=0", timeout_override=10000
                )
            except (PlaywrightTimeoutError, DataExtractionError):
                logger.info("No movement rows found on this page.")
                return []

            rows = page.locator(row_selector).all()
            logger.debug(f"Found {len(rows)} rows on this page.")

            for i, row in enumerate(rows):
                try:
                    date_str = (
                        row.locator("td:nth-child(1)").inner_text(timeout=5000).strip()
                    )
                    type_str = (
                        row.locator("td:nth-child(2)").inner_text(timeout=5000).strip()
                    )
                    description = (
                        row.locator("td:nth-child(3)").inner_text(timeout=5000).strip()
                    )
                    amount_str = (
                        row.locator(".transaction-table__amount >> nth=0")
                        .inner_text(timeout=5000)
                        .strip()
                    )

                    parsed_date = self._parse_bice_date(date_str)

                    if not parsed_date:
                        logger.warning(
                            f"Could not parse date: {date_str}. Skipping row {i}."
                        )
                        continue

                    parsed_amount = parse_chilean_amount(amount_str)

                    if "Abono" not in type_str:
                        parsed_amount = -parsed_amount

                    if parsed_amount.is_zero():
                        logger.debug(f"Row {i} has zero amount, skipping.")
                        continue

                    movement = MovementModel(
                        date=parsed_date,
                        description=description,
                        amount=parsed_amount,
                        currency="CLP",
                        transaction_type=type_str,
                        account_id=None,
                        account_type="corriente",
                        raw_data={
                            "date_str": date_str,
                            "amount_str": amount_str,
                            "type_str": type_str,
                        },
                    )
                    movements_on_page.append(movement)

                except Exception as e:
                    logger.warning(f"Failed to parse row {i}: {e}")
                    self._save_debug_info(f"07_row_parse_failed_row_{i}")
                    continue

            logger.info(
                f"Successfully extracted {len(movements_on_page)} movements from this page."
            )

            if not movements_on_page:
                if page_num == 1:
                    logger.info("No movements found on the first page. Stopping.")
                else:
                    logger.info("No more movements found on this page. Stopping.")
                break

            all_debit_movements.extend(movements_on_page)
            next_button_selector = 'ds-button[label="Siguiente"] button'

            try:
                next_button = page.locator(next_button_selector)

                if not next_button.is_visible(timeout=5000):
                    logger.info("No 'Siguiente' button visible. Reached the last page.")
                    break

                button_class = next_button.get_attribute("class")
                if button_class and "is-disabled" in button_class:
                    logger.info(
                        "Button 'Siguiente' is disabled. Reached the last page."
                    )
                    break

            except PlaywrightTimeoutError:
                logger.info(
                    "Timeout checking for 'Siguiente' button. Assuming last page."
                )
                break

            logger.info(f"Clicking 'Siguiente' to go to page {page_num + 1}...")

            first_row_description_selector = (
                "tbody.table-body tr:first-child td:nth-child(3)"
            )
            old_first_description = ""
            try:
                if page.locator(first_row_description_selector).count() > 0:
                    old_first_description = page.locator(
                        first_row_description_selector
                    ).inner_text(timeout=5000)
            except Exception:
                pass

            self._click(next_button)

            try:
                expect(page.locator(first_row_description_selector)).not_to_have_text(
                    old_first_description
                )
                logger.info("New page data loaded.")
            except PlaywrightTimeoutError:
                logger.warning(
                    "Page content did not change after clicking 'Siguiente'. Stopping loop."
                )
                break

            page_num += 1
            self._save_debug_info(f"07_movements_page_{page_num}")

        logger.info(
            f"Successfully extracted {len(all_debit_movements)} debit card movements."
        )

        return all_debit_movements

    def _extract_credit_card_movements(self) -> List[MovementModel]:
        """Orchestrates national credit card movement extraction (unbilled and billed)."""
        try:
            account_id_selector = "p.format-number"
            element = self._wait_for_selector(
                account_id_selector, timeout_override=15000
            )
            account_id = element.inner_text(timeout=5000).strip()
            logger.info(f"Found credit card ID: {account_id}")
        except Exception as e:
            logger.warning(
                f"Could not extract credit card ID, will be left blank. Error: {e}"
            )
            self._save_debug_info("account_id_extraction_failed_credit")
            account_id = "credit_card_unknown"

        all_movements: List[MovementModel] = []
        all_movements.extend(
            self._extract_unbilled_national_credit_movements(account_id)
        )
        all_movements.extend(self._extract_billed_national_credit_movements(account_id))

        all_movements.extend(self._extract_unbilled_intl_credit_movements(account_id))
        all_movements.extend(self._extract_billed_intl_credit_movements(account_id))

        logger.info(
            f"Credit card extraction finished. Total: {len(all_movements)} movements."
        )
        return all_movements

    def _extract_unbilled_national_credit_movements(
        self, account_id: str
    ) -> List[MovementModel]:
        """Extracts unbilled national credit card movements (current period, tab0)."""
        page = self._ensure_page()
        logger.info("Starting unbilled national credit card movements extraction.")

        row_selector = "app-transaction-row div.row.transaction"

        try:
            page.wait_for_selector(f"{row_selector} >> nth=0", timeout=10000)
        except (PlaywrightTimeoutError, DataExtractionError):
            logger.info("No unbilled credit card movement rows found on this page.")
            return []

        rows = page.locator(row_selector).all()
        logger.debug(f"Found {len(rows)} unbilled credit card rows.")

        unbilled_movements: List[MovementModel] = []

        for i, row in enumerate(rows):
            try:
                date_str = row.locator(".date").inner_text(timeout=5000).strip()
                parsed_date = self._parse_bice_date(date_str)
                if not parsed_date:
                    logger.warning(
                        f"Could not parse date: {date_str}. Skipping row {i}."
                    )
                    continue

                amount_str = (
                    row.locator(".transaction-amount").inner_text(timeout=5000).strip()
                )
                parsed_amount = parse_chilean_amount(
                    self._parse_bice_amount(amount_str)
                )

                row_class = row.locator("div.wm-100").get_attribute("class")

                if "cargos" in row_class:
                    transaction_type = "Cargo"
                    parsed_amount = -abs(parsed_amount)
                    category_name = (
                        row.locator(".category-name").inner_text(timeout=5000).strip()
                    )
                    category_desc = (
                        row.locator(".category-description")
                        .inner_text(timeout=5000)
                        .strip()
                    )
                    transaction_detail = (
                        row.locator(".transaction-detail")
                        .inner_text(timeout=5000)
                        .strip()
                    )
                    description = (
                        f"{transaction_detail} ({category_name}, {category_desc})"
                    )
                else:
                    transaction_type = "Abono"
                    category_name = (
                        row.locator(".category-name").inner_text(timeout=5000).strip()
                    )
                    transaction_detail = (
                        row.locator(".transaction-detail")
                        .inner_text(timeout=5000)
                        .strip()
                    )
                    description = f"{transaction_detail} ({category_name})"

                if parsed_amount.is_zero():
                    continue

                movement = MovementModel(
                    date=parsed_date,
                    description=description,
                    amount=parsed_amount,
                    currency="CLP",
                    transaction_type=transaction_type,
                    account_id=account_id,
                    account_type="credito",
                    raw_data={
                        "date_str": date_str,
                        "amount_str": amount_str,
                        "bank_category": transaction_type,
                    },
                )
                unbilled_movements.append(movement)

            except Exception as e:
                logger.warning(f"Failed to parse unbilled credit card row {i}: {e}")
                self._save_debug_info(f"unbilled_credit_row_{i}_failed")
                continue

        logger.info(
            f"Successfully extracted {len(unbilled_movements)} unbilled national credit card movements."
        )
        return unbilled_movements

    def _extract_billed_national_credit_movements(
        self, account_id: str
    ) -> List[MovementModel]:
        """Extracts billed national credit card movements from all billing period accordions."""
        page = self._ensure_page()
        logger.info("Navigating to billed national credit card movements (#tab1).")

        self._click("#tab1")

        accordion_locator = page.locator("[id^='acc-']")
        try:
            self._wait_for_selector(accordion_locator, timeout_override=15000)
        except (PlaywrightTimeoutError, DataExtractionError):
            logger.info("No billed movement periods found.")
            return []

        acc_count = accordion_locator.count()
        logger.info(f"Found {acc_count} billing period(s).")
        self._save_debug_info("12_billed_national_periods")

        all_billed_movements: List[MovementModel] = []

        for i in range(acc_count):
            acc = accordion_locator.nth(i)

            period_label = f"Período {i + 1}"
            try:
                period_label = (
                    acc.locator(".period-accordion-header strong")
                    .inner_text(timeout=5000)
                    .strip()
                )
            except Exception:
                pass

            logger.info(f"Processing billing period {i}: {period_label}.")

            toggle_button = acc.locator(".card-header button")
            try:
                is_expanded = toggle_button.get_attribute("aria-expanded") == "true"
                if not is_expanded:
                    self._click(toggle_button)
                    self._wait_for_selector(
                        acc.locator("app-transaction-row"),
                        timeout_override=10000,
                    )
            except (PlaywrightTimeoutError, DataExtractionError) as e:
                logger.warning(f"Failed to expand period {i} ({period_label}): {e}")
                self._save_debug_info(f"billed_period_{i}_expand_failed")
                continue

            rows = acc.locator("app-transaction-row div.row.transaction").all()
            logger.debug(f"Found {len(rows)} rows in period {i}.")

            period_movements: List[MovementModel] = []

            for j, row in enumerate(rows):
                try:
                    date_str = row.locator(".date").inner_text(timeout=5000).strip()
                    parsed_date = self._parse_bice_date(date_str)
                    if not parsed_date:
                        logger.warning(
                            f"Could not parse date '{date_str}' in period {i}, row {j}. Skipping."
                        )
                        continue

                    amount_str = (
                        row.locator(".transaction-amount")
                        .inner_text(timeout=5000)
                        .strip()
                    )
                    parsed_amount = parse_chilean_amount(
                        self._parse_bice_amount(amount_str)
                    )

                    row_class = row.locator("div.wm-100").get_attribute("class")

                    installments_str = ""
                    try:
                        installments_str = (
                            row.locator(".transaction-installments")
                            .inner_text(timeout=5000)
                            .strip()
                        )
                    except Exception:
                        pass

                    if "cargos" in row_class:
                        transaction_type = "Cargo"
                        parsed_amount = -abs(parsed_amount)
                        category_name = (
                            row.locator(".category-name")
                            .inner_text(timeout=5000)
                            .strip()
                        )
                        category_desc = (
                            row.locator(".category-description")
                            .inner_text(timeout=5000)
                            .strip()
                        )
                        transaction_detail = (
                            row.locator(".transaction-detail")
                            .inner_text(timeout=5000)
                            .strip()
                        )
                        description = (
                            f"{transaction_detail} ({category_name}, {category_desc})"
                        )
                    else:
                        transaction_type = "Abono"
                        parsed_amount = abs(parsed_amount)
                        category_name = (
                            row.locator(".category-name")
                            .inner_text(timeout=5000)
                            .strip()
                        )
                        transaction_detail = (
                            row.locator(".transaction-detail")
                            .inner_text(timeout=5000)
                            .strip()
                        )
                        description = f"{transaction_detail} ({category_name})"

                    if parsed_amount.is_zero():
                        logger.debug(f"Period {i}, row {j} has zero amount, skipping.")
                        continue

                    movement = MovementModel(
                        date=parsed_date,
                        description=description,
                        amount=parsed_amount,
                        currency="CLP",
                        transaction_type=transaction_type,
                        account_id=account_id,
                        account_type="credito",
                        raw_data={
                            "date_str": date_str,
                            "amount_str": amount_str,
                            "bank_category": transaction_type,
                            "billing_period": period_label,
                            "installments": installments_str,
                        },
                    )
                    period_movements.append(movement)

                except Exception as e:
                    logger.warning(f"Failed to parse billed row {j} in period {i}: {e}")
                    self._save_debug_info(f"billed_period_{i}_row_{j}_failed")
                    continue

            logger.info(
                f"Extracted {len(period_movements)} movements from period {i}: {period_label}."
            )
            all_billed_movements.extend(period_movements)

        logger.info(
            f"Total billed national credit card movements: {len(all_billed_movements)}."
        )
        return all_billed_movements

    def _extract_unbilled_intl_credit_movements(
        self, account_id: str
    ) -> List[MovementModel]:
        """Extracts unbilled international credit card movements (current period, tab0)."""
        page = self._ensure_page()
        logger.info("Navigating to international credit card movements.")

        self._click("app-segmented-control .selection:has-text('Internacional')")

        row_selector = "app-transaction-row div.row.transaction"
        try:
            page.wait_for_selector(f"{row_selector} >> nth=0", timeout=5000)
        except (PlaywrightTimeoutError, DataExtractionError):
            logger.info("No unbilled international credit card movements found.")
            return []

        rows = page.locator(row_selector).all()
        logger.debug(f"Found {len(rows)} unbilled international rows.")

        unbilled_intl_movements: List[MovementModel] = []

        for i, row in enumerate(rows):
            try:
                date_str = row.locator(".date").inner_text(timeout=5000).strip()
                parsed_date = self._parse_bice_date(date_str)
                if not parsed_date:
                    logger.warning(
                        f"Could not parse date: {date_str}. Skipping row {i}."
                    )
                    continue

                amount_str = (
                    row.locator(".transaction-amount").inner_text(timeout=5000).strip()
                )
                parsed_amount = parse_chilean_amount(
                    self._parse_intl_amount(amount_str)
                )

                row_class = row.locator("div.wm-100").get_attribute("class")

                if "cargos" in row_class:
                    transaction_type = "Cargo"
                    parsed_amount = -abs(parsed_amount)
                    category_name = (
                        row.locator(".category-name").inner_text(timeout=5000).strip()
                    )
                    category_desc = (
                        row.locator(".category-description")
                        .inner_text(timeout=5000)
                        .strip()
                    )
                    transaction_detail = (
                        row.locator(".transaction-detail")
                        .inner_text(timeout=5000)
                        .strip()
                    )
                    description = (
                        f"{transaction_detail} ({category_name}, {category_desc})"
                    )
                else:
                    transaction_type = "Abono"
                    parsed_amount = abs(parsed_amount)
                    category_name = (
                        row.locator(".category-name").inner_text(timeout=5000).strip()
                    )
                    transaction_detail = (
                        row.locator(".transaction-detail")
                        .inner_text(timeout=5000)
                        .strip()
                    )
                    description = f"{transaction_detail} ({category_name})"

                if parsed_amount.is_zero():
                    continue

                movement = MovementModel(
                    date=parsed_date,
                    description=description,
                    amount=parsed_amount,
                    currency="USD",
                    transaction_type=transaction_type,
                    account_id=account_id,
                    account_type="credito",
                    raw_data={
                        "date_str": date_str,
                        "amount_str": amount_str,
                        "bank_category": transaction_type,
                    },
                )
                unbilled_intl_movements.append(movement)

            except Exception as e:
                logger.warning(f"Failed to parse unbilled international row {i}: {e}")
                self._save_debug_info(f"unbilled_intl_row_{i}_failed")
                continue

        logger.info(
            f"Successfully extracted {len(unbilled_intl_movements)} unbilled international credit card movements."
        )
        return unbilled_intl_movements

    def _extract_billed_intl_credit_movements(
        self, account_id: str
    ) -> List[MovementModel]:
        """Extracts billed international credit card movements from all billing period accordions."""
        page = self._ensure_page()
        logger.info("Navigating to billed international credit card movements (#tab1).")

        self._click("#tab1")

        accordion_locator = page.locator("[id^='acc-']")
        try:
            self._wait_for_selector(accordion_locator, timeout_override=15000)
        except (PlaywrightTimeoutError, DataExtractionError):
            logger.info("No billed international movement periods found.")
            return []

        acc_count = accordion_locator.count()
        logger.info(f"Found {acc_count} international billing period(s).")
        self._save_debug_info("13_billed_intl_periods")

        all_billed_intl_movements: List[MovementModel] = []

        for i in range(acc_count):
            acc = accordion_locator.nth(i)

            period_label = f"Período {i + 1}"
            try:
                period_label = (
                    acc.locator(".period-accordion-header strong")
                    .inner_text(timeout=5000)
                    .strip()
                )
            except Exception:
                pass

            logger.info(f"Processing international billing period {i}: {period_label}.")

            toggle_button = acc.locator(".card-header button")
            try:
                is_expanded = toggle_button.get_attribute("aria-expanded") == "true"
                if not is_expanded:
                    self._click(toggle_button)
                    self._wait_for_selector(
                        acc.locator("app-transaction-row"),
                        timeout_override=10000,
                    )
            except (PlaywrightTimeoutError, DataExtractionError) as e:
                logger.warning(
                    f"Failed to expand international period {i} ({period_label}): {e}"
                )
                self._save_debug_info(f"billed_intl_period_{i}_expand_failed")
                continue

            rows = acc.locator("app-transaction-row div.row.transaction").all()
            logger.debug(f"Found {len(rows)} rows in international period {i}.")

            period_movements: List[MovementModel] = []

            for j, row in enumerate(rows):
                try:
                    date_str = row.locator(".date").inner_text(timeout=5000).strip()
                    parsed_date = self._parse_bice_date(date_str)
                    if not parsed_date:
                        logger.warning(
                            f"Could not parse date '{date_str}' in intl period {i}, row {j}. Skipping."
                        )
                        continue

                    amount_str = (
                        row.locator(".transaction-amount")
                        .inner_text(timeout=5000)
                        .strip()
                    )
                    parsed_amount = parse_chilean_amount(
                        self._parse_intl_amount(amount_str)
                    )

                    row_class = row.locator("div.wm-100").get_attribute("class")

                    if "cargos" in row_class:
                        transaction_type = "Cargo"
                        parsed_amount = -abs(parsed_amount)
                        category_name = (
                            row.locator(".category-name")
                            .inner_text(timeout=5000)
                            .strip()
                        )
                        category_desc = (
                            row.locator(".category-description")
                            .inner_text(timeout=5000)
                            .strip()
                        )
                        transaction_detail = (
                            row.locator(".transaction-detail")
                            .inner_text(timeout=5000)
                            .strip()
                        )
                        description = (
                            f"{transaction_detail} ({category_name}, {category_desc})"
                        )
                    else:
                        transaction_type = "Abono"
                        parsed_amount = abs(parsed_amount)
                        category_name = (
                            row.locator(".category-name")
                            .inner_text(timeout=5000)
                            .strip()
                        )
                        transaction_detail = (
                            row.locator(".transaction-detail")
                            .inner_text(timeout=5000)
                            .strip()
                        )
                        description = f"{transaction_detail} ({category_name})"

                    if parsed_amount.is_zero():
                        logger.debug(
                            f"Intl period {i}, row {j} has zero amount, skipping."
                        )
                        continue

                    movement = MovementModel(
                        date=parsed_date,
                        description=description,
                        amount=parsed_amount,
                        currency="USD",
                        transaction_type=transaction_type,
                        account_id=account_id,
                        account_type="credito",
                        raw_data={
                            "date_str": date_str,
                            "amount_str": amount_str,
                            "bank_category": transaction_type,
                            "billing_period": period_label,
                        },
                    )
                    period_movements.append(movement)

                except Exception as e:
                    logger.warning(
                        f"Failed to parse billed intl row {j} in period {i}: {e}"
                    )
                    self._save_debug_info(f"billed_intl_period_{i}_row_{j}_failed")
                    continue

            logger.info(
                f"Extracted {len(period_movements)} movements from international period {i}: {period_label}."
            )
            all_billed_intl_movements.extend(period_movements)

        logger.info(
            f"Total billed international credit card movements: {len(all_billed_intl_movements)}."
        )
        return all_billed_intl_movements
