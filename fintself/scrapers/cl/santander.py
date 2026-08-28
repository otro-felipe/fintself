"""HTTP-only Santander Chile scraper."""

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from importlib import resources
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin

from curl_cffi import requests

from fintself.core.exceptions import DataExtractionError, LoginError
from fintself.core.models import MovementModel
from fintself.utils.parsers import parse_chilean_amount, parse_chilean_date


TOKEN_URL = (
    "https://apideveloper.santander.cl/sancl/privado/"
    "party_authentication_restricted/party_auth_dss/v1/oauth2/token"
)
CURRENT_ACCOUNT_TRANSACTIONS_URL = (
    "https://openbanking.santander.cl/"
    "account_balances_transactions_and_withholdings_retail/v1/"
    "current-accounts/transactions"
)
CREDIT_CARD_BASE_URL = "https://api-dsk.santander.cl/perdsk/tarjetasDeCredito"
CREDIT_CARD_UNBILLED_URL = f"{CREDIT_CARD_BASE_URL}/consultaUltimosMovimientos"
CREDIT_CARD_PERIODS_URL = f"{CREDIT_CARD_BASE_URL}/cuentasDisponibles"
CREDIT_CARD_NATIONAL_URL = f"{CREDIT_CARD_BASE_URL}/estadoCuentaNacional"
CREDIT_CARD_INTERNATIONAL_URL = f"{CREDIT_CARD_BASE_URL}/estadoCuentaInternacional"
CREDIT_CARD_STATEMENT_URL = f"{CREDIT_CARD_BASE_URL}/estadoDeCuenta"
MAZON_BASE_URL = "https://api-app.santander.cl/appper/Mazon"
MAZON_UNBILLED_URL = f"{MAZON_BASE_URL}/MovimientosPorFacturar"
MAZON_NATIONAL_URL = f"{MAZON_BASE_URL}/ResumenEECCNacional"
MAZON_INTERNATIONAL_URL = f"{MAZON_BASE_URL}/ResumenEECCInternacional"

AUTH_CLIENT_ID = "4e9af62c-6563-42cd-aab6-0dd7d50a9131"
ACCOUNT_CLIENT_ID = "O2XRSU4kVspEGbLDDGfFC5BOTrGKh5Ts"
CARD_CLIENT_ID = "O2XRSU4kVspEGbLDDGfFC5BOTrGKh5Ts"
FRAME_URL = "https://mibanco.santander.cl/UI.Web.HB/Private_new/frame/"


@dataclass
class SantanderAuth:
    access_token: str
    token_jwt: Optional[str]
    products: Dict[str, Any]


class NodeAkamaiTelemetryProvider:
    """Generate Akamai telemetry from public HTML in an isolated Node process."""

    def __init__(
        self,
        *,
        node_binary: str = "node",
        subprocess_runner: Callable = subprocess.run,
    ):
        self._node_binary = node_binary
        self._subprocess_runner = subprocess_runner

    def __call__(self, frame_html: str) -> str:
        helper = resources.files("fintself.scrapers.cl").joinpath(
            "akamai_runtime/telemetry.mjs"
        )
        request = json.dumps({"frameUrl": FRAME_URL, "html": frame_html})
        try:
            result = self._subprocess_runner(
                [self._node_binary, str(helper)],
                input=request,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (FileNotFoundError, OSError):
            raise DataExtractionError(
                "Santander telemetry unavailable: Node.js and jsdom are required."
            )
        except subprocess.TimeoutExpired:
            raise DataExtractionError("Unable to generate Santander telemetry.")

        if result.returncode == 78:
            raise DataExtractionError(
                "Santander telemetry unavailable: Node.js and jsdom are required."
            )
        if result.returncode != 0:
            raise DataExtractionError("Unable to generate Santander telemetry.")
        telemetry = result.stdout.strip()
        if not telemetry or len(telemetry) > 16384 or any(
            character in telemetry for character in "\r\n\x00"
        ):
            raise DataExtractionError("Unable to generate Santander telemetry.")
        return telemetry


class PublicJavascriptChallengeProvider:
    """Discover public auth material without persisting response bodies.

    The public bundle provides ``tokentbk``. A small Node+jsdom helper evaluates the
    public Akamai script and returns ephemeral telemetry held only in memory.
    """

    def __init__(self, telemetry_provider: Optional[Callable] = None):
        self._telemetry_provider = telemetry_provider or NodeAkamaiTelemetryProvider()

    def __call__(self, session) -> Dict[str, str]:
        try:
            frame = session.get(FRAME_URL, impersonate="chrome", timeout=30)
            if frame.status_code != 200:
                raise DataExtractionError("Unable to load Santander public login challenge.")
            main_match = re.search(
                r'<script[^>]+src=["\']([^"\']*main\.[^"\']+\.js)',
                frame.text,
                re.IGNORECASE,
            )
            if not main_match:
                raise DataExtractionError("Unable to find Santander public login challenge.")

            main_url = urljoin(FRAME_URL, main_match.group(1))
            runtime = session.get(main_url, impersonate="chrome", timeout=30)
            if runtime.status_code != 200:
                raise DataExtractionError("Unable to load Santander public login challenge.")
            chunk_match = re.search(r'4451:"([0-9a-z]+)"', runtime.text)
            if not chunk_match:
                raise DataExtractionError("Unable to find Santander public login challenge.")

            app_url = urljoin(main_url, f"4451.{chunk_match.group(1)}.js")
            app = session.get(app_url, impersonate="chrome", timeout=30)
            if app.status_code != 200:
                raise DataExtractionError("Unable to load Santander public login challenge.")
            token_match = re.search(
                r'"tokentbk",value:"(TOKEN@[0-9A-Za-z._-]+)"',
                app.text,
            )
            if not token_match:
                raise DataExtractionError("Unable to find Santander public login challenge.")
            telemetry = self._telemetry_provider(frame.text)
            return {
                "tokentbk": token_match.group(1),
                "Akamai-BM-Telemetry": telemetry,
            }
        except DataExtractionError:
            raise
        except Exception:
            raise DataExtractionError("Unable to load Santander public login challenge.")


class SantanderScraper:
    """Extract Santander movements through the bank's HTTP APIs only."""

    def __init__(
        self,
        headless: Optional[bool] = None,
        debug_mode: Optional[bool] = None,
        *,
        session_factory: Callable = requests.Session,
        challenge_provider: Optional[Callable] = None,
        today_provider: Callable[[], date] = date.today,
    ):
        self._headless_compat = headless
        self._debug_mode_compat = debug_mode
        self._session_factory = session_factory
        self._challenge_provider = challenge_provider or PublicJavascriptChallengeProvider()
        self._today_provider = today_provider

    def _get_bank_id(self) -> str:
        return "cl_santander"

    @staticmethod
    def _normalize_rut(user: str) -> str:
        return re.sub(r"[^0-9Kk]", "", user).upper().zfill(11)

    def scrape(self, user: str, password: str) -> List[MovementModel]:
        session = self._session_factory()
        normalized_user = self._normalize_rut(user)
        try:
            auth = self._authenticate(session, normalized_user, password)
            movements = self._scrape_current_accounts(session, auth)
            movements.extend(self._scrape_credit_cards(session, auth, normalized_user))
            return movements
        finally:
            password = ""
            user = ""
            normalized_user = ""
            try:
                session.close()
            except Exception:
                pass

    def _authenticate(self, session, user: str, password: str) -> SantanderAuth:
        normalized_user = self._normalize_rut(user)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://mibanco.santander.cl",
            "Referer": "https://mibanco.santander.cl/",
            "app": "007",
            "canal": "003",
            "nro_ser": "1",
        }
        headers.update(self._challenge_provider(session))
        response = self._post(
            session,
            TOKEN_URL,
            allow_redirects=False,
            data={
                "grant_type": "password",
                "client_id": AUTH_CLIENT_ID,
                "scope": "Completa",
                "username": normalized_user,
                "password": password,
            },
            headers=headers,
        )
        if response.status_code in (302, 400, 401, 403):
            raise LoginError("Santander rejected the authentication.")
        if response.status_code >= 400:
            raise DataExtractionError("Santander authentication service unavailable.")

        payload = self._response_json(response)
        auth_payload = payload.get("data", payload)
        access_token = auth_payload.get("access_token")
        if not access_token:
            raise LoginError("Santander rejected the authentication.")
        return SantanderAuth(
            access_token=access_token,
            token_jwt=auth_payload.get("tokenJWT"),
            products=self._json_object(auth_payload.get("CrucedeProducto")),
        )

    @staticmethod
    def _post(session, url: str, **kwargs):
        try:
            return session.post(url, impersonate="chrome", timeout=30, **kwargs)
        except Exception:
            raise DataExtractionError("Santander HTTP request failed.")

    @staticmethod
    def _response_json(response) -> Dict[str, Any]:
        try:
            payload = response.json()
        except Exception:
            text = getattr(response, "text", "")
            if not isinstance(text, str) or not re.match(r"^\)\]\}',?\n", text):
                raise DataExtractionError("Santander returned invalid JSON.") from None
            try:
                payload = json.loads(re.sub(r"^\)\]\}',?\n", "", text, count=1))
            except (TypeError, ValueError):
                raise DataExtractionError("Santander returned invalid JSON.") from None
        if not isinstance(payload, dict):
            raise DataExtractionError("Santander returned invalid JSON.")
        return payload

    @staticmethod
    def _json_object(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _product_list(products: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
        value = products.get(name, [])
        if isinstance(value, dict):
            value = list(value.values())
        normalized = [item for item in value if isinstance(item, dict)]
        if normalized:
            return normalized

        matrix = SantanderScraper._dig(
            products, "MATRICES", "MATRIZCAPTACIONES", "e1"
        )
        if not isinstance(matrix, list):
            return []

        discovered = []
        for product in matrix:
            if not isinstance(product, dict):
                continue
            label = " ".join(
                str(product.get(key) or "").upper()
                for key in (
                    "AGRUPACIONCOMERCIAL",
                    "GLOSACORTA",
                    "PRODUCTO",
                    "SUBPRODUCTO",
                )
            )
            pan = str(product.get("NUMEROPAN") or "")
            pan_digits = re.sub(r"\D", "", pan)
            is_card = len(pan_digits) == 16 or any(
                marker in label
                for marker in ("TARJETA", "VISA", "MASTERCARD", "MASTER CARD", "AMEX", "WORLD")
            )
            is_current_account = any(
                marker in label
                for marker in (
                    "CUENTA CORRIENTE",
                    "CTA CTE",
                    "CUENTA VISTA",
                    "LINEA DE CREDITO",
                    "LCR",
                )
            )
            if (name == "creditCards" and not is_card) or (
                name == "currentAccounts" and (is_card or not is_current_account)
            ):
                continue

            office = str(product.get("OFICINACONTRATO") or "")
            contract = str(product.get("NUMEROCONTRATO") or "")
            if not office or not contract:
                continue
            common = {
                "commercialGroup": str(product.get("AGRUPACIONCOMERCIAL") or ""),
                "contractNumber": contract,
                "contractOffice": office,
                "currency": str(product.get("CODIGOMONEDA") or "CLP"),
            }
            if name == "creditCards":
                common["cardId"] = pan or contract
            else:
                common["accountId"] = f"{office}{contract}"
            discovered.append(common)
        return discovered

    @staticmethod
    def _account_headers(access_token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://mibanco.santander.cl",
            "Referer": "https://mibanco.santander.cl/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "X-Client-Code": "STD-PER-FPP",
            "X-Organization-Code": "Santander",
            "X-Santander-Client-Id": ACCOUNT_CLIENT_ID,
            "x-B3-SpanId": "AL43243287438243P",
            "x-schema-id": "GHOBP",
        }

    @staticmethod
    def _card_headers(access_token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "X-Santander-Client-Id": CARD_CLIENT_ID,
        }

    def _post_json(self, session, url: str, payload: dict, headers: dict) -> dict:
        response = self._post(session, url, json=payload, headers=headers)
        if response.status_code >= 400:
            raise DataExtractionError("Santander movement service unavailable.")
        return self._response_json(response)

    def _scrape_current_accounts(self, session, auth: SantanderAuth) -> List[MovementModel]:
        movements: List[MovementModel] = []
        today = self._today_provider()
        closing_date = date(
            today.year + (1 if today.month == 12 else 0),
            1 if today.month == 12 else today.month + 1,
            15,
        )
        for account in self._product_list(auth.products, "currentAccounts"):
            account_id = account.get("accountId") or (
                f"{account.get('contractOffice', '')}{account.get('contractNumber', '')}"
            )
            if not account_id:
                continue
            currency = account.get("currency") or "CLP"
            request = {
                "accountId": account_id,
                "currency": currency,
                "commercialGroup": "LCA"
                if account.get("commercialGroup") == "LCR"
                else "",
                "openingDate": (today - timedelta(days=40)).isoformat(),
                "closingDate": closing_date.isoformat(),
            }
            repositionings = set()
            while True:
                body = self._post_json(
                    session,
                    CURRENT_ACCOUNT_TRANSACTIONS_URL,
                    request,
                    self._account_headers(auth.access_token),
                )
                movements.extend(
                    self._parse_current_account_movements(body, account_id, currency)
                )
                repositioning = self._repositioning(body)
                if not repositioning or repositioning in repositionings:
                    break
                repositionings.add(repositioning)
                request = {
                    **request,
                    "startMovement": repositioning[0],
                    "endMovement": repositioning[1],
                }
        return movements

    @classmethod
    def _repositioning(cls, payload: dict) -> Optional[tuple]:
        value = (
            cls._dig(payload, "data", "repositioningExit")
            or cls._dig(payload, "data", "DATA", "repositioningExit")
            or payload.get("repositioningExit")
        )
        if not isinstance(value, dict):
            return None
        if str(value.get("recordRecover")) != "50":
            return None
        start = value.get("initialMove")
        end = value.get("finalMove")
        return (str(start), str(end)) if start and end else None

    def _scrape_credit_cards(
        self, session, auth: SantanderAuth, user: str
    ) -> List[MovementModel]:
        movements: List[MovementModel] = []
        for card in self._product_list(auth.products, "creditCards"):
            movements.extend(self._scrape_credit_card(session, auth, user, card))
        return movements

    def _scrape_credit_card(
        self, session, auth: SantanderAuth, user: str, card: Dict[str, Any]
    ) -> List[MovementModel]:
        office = str(card.get("contractOffice") or card.get("Centro") or "")
        contract = str(card.get("contractNumber") or card.get("Cuenta") or "")
        account_id = str(card.get("cardId") or contract)
        currency = str(card.get("currency") or "CLP")
        if not office or not contract:
            return []
        headers = self._card_headers(auth.access_token)
        body = self._post_json(
            session,
            CREDIT_CARD_UNBILLED_URL,
            {
                "Cabecera": self._legacy_header(user, "InfoDispositivo"),
                "Entrada": {
                    "Entidad": "0035",
                    "Centro": office,
                    "Cuenta": contract,
                    "Moneda": currency,
                },
            },
            headers,
        )
        movements = self._parse_credit_movements(
            self._dig(body, "DATA", "MatrizMovimientos") or [],
            account_id,
            currency,
            date_key="Fecha",
            amount_key="Importe",
            description_keys=("Comercio", "Descripcion"),
            indicator_key="IndicadorDebeHaber",
        )

        periods_body = self._post_json(
            session,
            CREDIT_CARD_PERIODS_URL,
            self._periods_payload(user, office, contract),
            headers,
        )
        periods_root = self._dig(
            periods_body, "DATA", "AS_TIB_WM01_CONCuentasDisponibles"
        ) or {}
        if self._dig(periods_root, "INFO", "CODERR") != "00":
            return movements
        for period in self._dig(periods_root, "OUTPUT", "MATRIZ") or []:
            if not isinstance(period, dict):
                continue
            period_currency = str(period.get("MONEDA") or currency)
            international = period_currency.upper() not in ("CLP", "PESO", "PESOS")
            period_body = self._post_json(
                session,
                CREDIT_CARD_INTERNATIONAL_URL
                if international
                else CREDIT_CARD_NATIONAL_URL,
                self._statement_payload(user, period, international),
                headers,
            )
            movements.extend(
                self._parse_billed_credit(period_body, account_id, period_currency)
            )
        return movements

    @staticmethod
    def _legacy_header(user: str, info_device: str = "003") -> dict:
        return {
            "HOST": {
                "USUARIO-ALT": "GHOBP",
                "TERMINAL-ALT": "",
                "CANAL-ID": "078",
            },
            "CanalFisico": "003",
            "CanalLogico": "74",
            "RutCliente": user,
            "RutUsuario": user,
            "IpCliente": "",
            "InfoDispositivo": info_device,
        }

    def _periods_payload(self, user: str, office: str, contract: str) -> dict:
        return {
            "cabecera": self._legacy_header(user),
            "INPUT": {
                "USUARIO-ALT": "GHOBP",
                "CANAL-ID": "078",
                "CODENT": "0035",
                "CENTALT": office,
                "CUENTA": contract,
                "PAN": "",
            },
        }

    def _statement_payload(self, user: str, period: dict, international: bool) -> dict:
        input_data = {
            "USUARIO-ALT": "GHOBP",
            "TERMINAL-ALT": "",
            "CANAL-ID": "078",
            "FILLER": "",
            "CodEnt": period.get("CODENT") or "0035",
            "Cuenta": period.get("CUENTA") or "",
            "Pan": "",
            "NumExtracto": period.get("NUMEXT") or "",
            "NumMov": "",
            "FilasRecuperar": "",
            "ID-RECALL": "",
        }
        input_data["CentAlta" if international else "CentAlt"] = (
            period.get("CENTALT") or ""
        )
        return {"cabecera": self._legacy_header(user), "INPUT": input_data}

    def _parse_current_account_movements(
        self, payload: dict, account_id: str, currency: str
    ) -> List[MovementModel]:
        candidates = (
            self._dig(payload, "data", "MovimientosDepositos"),
            self._dig(payload, "data", "Movimientos"),
            self._dig(payload, "data", "movements"),
            self._dig(payload, "data", "DATA", "Movimientos"),
            self._dig(payload, "data", "DATA", "MovimientosDepositos"),
        )
        rows = next(
            (candidate for candidate in candidates if isinstance(candidate, list)), []
        )
        parsed: List[MovementModel] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            movement_date = self._parse_date(
                row.get("transactionDate")
                or row.get("operationTime")
                or row.get("accountingDate")
                or row.get("fecha")
                or row.get("Fecha")
            )
            amount = self._movement_amount(row)
            if not movement_date or amount == 0:
                continue
            description = str(
                row.get("expandedCode")
                or row.get("observation")
                or row.get("description")
                or row.get("descripcion")
                or row.get("glosa")
                or ""
            )
            parsed.append(
                MovementModel(
                    date=movement_date,
                    description=description,
                    amount=amount,
                    currency=str(row.get("currency") or currency),
                    transaction_type="Cargo" if amount < 0 else "Abono",
                    account_id=account_id,
                    account_type="corriente",
                    raw_data=row,
                )
            )
        return parsed

    def _parse_billed_credit(
        self, payload: dict, account_id: str, currency: str
    ) -> List[MovementModel]:
        national = self._dig(
            payload,
            "DATA",
            "AS_TIB_WM02_CONEstCtaNacional_Response",
            "OUTPUT",
            "Matriz",
        )
        international = self._dig(
            payload,
            "DATA",
            "AS_TIB_WM03_CONEstCtaInternacional_Response",
            "OUTPUT",
            "MATRIZDATOS",
        )
        rows = national if isinstance(national, list) else international
        if not isinstance(rows, list):
            return []
        return self._parse_credit_movements(
            rows,
            account_id,
            currency,
            date_key="FechaTxs",
            amount_key="MontoTxs" if isinstance(national, list) else "MontoTransaccion",
            description_keys=("NombreComercio", "Glosa1", "Glosa2"),
            invert_unsigned=True,
        )

    def _parse_credit_movements(
        self,
        rows: list,
        account_id: str,
        currency: str,
        *,
        date_key: str,
        amount_key: str,
        description_keys: tuple,
        indicator_key: Optional[str] = None,
        invert_unsigned: bool = False,
    ) -> List[MovementModel]:
        movements: List[MovementModel] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            movement_date = self._parse_date(row.get(date_key))
            amount = parse_chilean_amount(str(row.get(amount_key) or "0"))
            indicator = str(row.get(indicator_key) or "") if indicator_key else ""
            if indicator.upper() == "D":
                amount = -abs(amount)
            elif indicator:
                amount = abs(amount)
            elif invert_unsigned and amount > 0:
                amount = -amount
            if not movement_date or amount == 0:
                continue
            description = next(
                (str(row.get(key)) for key in description_keys if row.get(key)), ""
            )
            movements.append(
                MovementModel(
                    date=movement_date,
                    description=description,
                    amount=amount,
                    currency=currency,
                    transaction_type="Cargo" if amount < 0 else "Abono",
                    account_id=account_id,
                    account_type="credito",
                    raw_data=row,
                )
            )
        return movements

    @staticmethod
    def _movement_amount(row: dict) -> Decimal:
        if row.get("debitAmount") not in (None, ""):
            return -abs(parse_chilean_amount(str(row["debitAmount"])))
        if row.get("creditAmount") not in (None, ""):
            return abs(parse_chilean_amount(str(row["creditAmount"])))
        amount = parse_chilean_amount(
            str(
                row.get("movementAmount")
                or row.get("amount")
                or row.get("monto")
                or row.get("Importe")
                or "0"
            )
        )
        charge_payment_flag = str(row.get("chargePaymentFlag") or "").upper()
        if charge_payment_flag in ("C", "CHARGE"):
            return -abs(amount)
        if charge_payment_flag in ("P", "PAYMENT"):
            return abs(amount)
        movement_type = str(
            row.get("tipoMovimiento") or row.get("IndicadorDebeHaber") or ""
        ).upper()
        if movement_type in ("D", "DEBITO", "CARGO"):
            return -abs(amount)
        if movement_type in ("H", "C", "PAGO", "CREDITO", "ABONO"):
            return abs(amount)
        return amount

    @staticmethod
    def _parse_date(value: Any) -> Optional[datetime]:
        if not value:
            return None
        text = str(value).strip()
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                pass
        return parse_chilean_date(text)

    @staticmethod
    def _dig(value: Any, *keys: str) -> Any:
        current = value
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current
