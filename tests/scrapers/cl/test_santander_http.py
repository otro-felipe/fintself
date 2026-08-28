import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from fintself.core.exceptions import DataExtractionError, LoginError
from fintself.scrapers.base import BaseScraper
from fintself.scrapers.cl.santander import (
    CREDIT_CARD_INTERNATIONAL_URL,
    CREDIT_CARD_NATIONAL_URL,
    CREDIT_CARD_UNBILLED_URL,
    CURRENT_ACCOUNT_TRANSACTIONS_URL,
    TOKEN_URL,
    PublicJavascriptChallengeProvider,
    SantanderScraper,
)


FIXTURES = Path(__file__).parents[2] / "fixtures" / "cl" / "santander_http"
SENSITIVE_RUT = "12.345.678-5"
SENSITIVE_PASSWORD = "SENSITIVE_PASSWORD_SENTINEL"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses=None, get_responses=None):
        self.responses = list(responses or [])
        self.get_responses = list(get_responses or [])
        self.post_calls = []
        self.get_calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.get_responses.pop(0)

    def close(self):
        self.closed = True


def challenge_headers(_session):
    return {
        "tokentbk": "SYNTHETIC_PUBLIC_CHALLENGE",
        "Akamai-BM-Telemetry": "SYNTHETIC_EPHEMERAL_TELEMETRY",
    }


def test_santander_is_http_only_and_keeps_the_factory_constructor_contract():
    assert not issubclass(SantanderScraper, BaseScraper)

    scraper = SantanderScraper(headless=True, debug_mode=True)

    assert scraper._headless_compat is True
    assert scraper._debug_mode_compat is True


def test_santander_factory_import_does_not_require_playwright_runtime():
    script = """
import builtins
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'playwright' or name.startswith('playwright.'):
        raise ModuleNotFoundError('playwright deliberately unavailable')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
from fintself import get_scraper
scraper = get_scraper('cl_santander', headless=True, debug_mode=False)
assert scraper.__class__.__name__ == 'SantanderScraper'
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_auth_normalizes_rut_and_posts_exact_public_contract_without_cookies():
    session = FakeSession(responses=[FakeResponse(payload=load_fixture("auth_success.json"))])
    scraper = SantanderScraper(
        session_factory=lambda: session,
        challenge_provider=challenge_headers,
    )

    auth = scraper._authenticate(session, SENSITIVE_RUT, SENSITIVE_PASSWORD)

    assert auth.access_token == "SYNTHETIC_ACCESS_TOKEN"
    assert auth.products["currentAccounts"][0]["contractOffice"] == "0123"
    assert len(session.post_calls) == 1
    url, kwargs = session.post_calls[0]
    assert url == TOKEN_URL
    assert kwargs["impersonate"] == "chrome"
    assert kwargs["timeout"] == 30
    assert kwargs["allow_redirects"] is False
    assert kwargs["data"] == {
        "grant_type": "password",
        "client_id": "4e9af62c-6563-42cd-aab6-0dd7d50a9131",
        "scope": "Completa",
        "username": "00123456785",
        "password": SENSITIVE_PASSWORD,
    }
    assert kwargs["headers"] == {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://mibanco.santander.cl",
        "Referer": "https://mibanco.santander.cl/",
        "app": "007",
        "canal": "003",
        "nro_ser": "1",
        "tokentbk": "SYNTHETIC_PUBLIC_CHALLENGE",
        "Akamai-BM-Telemetry": "SYNTHETIC_EPHEMERAL_TELEMETRY",
    }


def test_real_product_matrix_is_normalized_without_exposing_provider_fields():
    products = {
        "MATRICES": {
            "MATRIZCAPTACIONES": {
                "e1": [
                    {
                        "AGRUPACIONCOMERCIAL": "SYNTHETIC CHECKING GROUP",
                        "CODIGOMONEDA": "CLP",
                        "GLOSACORTA": "CUENTA CORRIENTE",
                        "NUMEROCONTRATO": "000012345678",
                        "NUMEROPAN": "",
                        "OFICINACONTRATO": "0123",
                        "PRODUCTO": "CUENTA",
                        "SUBPRODUCTO": "CORRIENTE",
                    },
                    {
                        "AGRUPACIONCOMERCIAL": "SYNTHETIC CARD GROUP",
                        "CODIGOMONEDA": "USD",
                        "GLOSACORTA": "TARJETA DE CREDITO",
                        "NUMEROCONTRATO": "000000009876",
                        "NUMEROPAN": "0000000000009876",
                        "OFICINACONTRATO": "4321",
                        "PRODUCTO": "TARJETA",
                        "SUBPRODUCTO": "CREDITO",
                    },
                    {
                        "GLOSACORTA": "CREDITO DE CONSUMO",
                        "NUMEROCONTRATO": "SHOULD_NOT_BE_SCRAPED",
                        "NUMEROPAN": "",
                        "OFICINACONTRATO": "9999",
                        "PRODUCTO": "PRESTAMO",
                        "SUBPRODUCTO": "CONSUMO",
                    },
                ]
            }
        }
    }

    accounts = SantanderScraper._product_list(products, "currentAccounts")
    cards = SantanderScraper._product_list(products, "creditCards")

    assert accounts == [
        {
            "accountId": "0123000012345678",
            "commercialGroup": "SYNTHETIC CHECKING GROUP",
            "contractNumber": "000012345678",
            "contractOffice": "0123",
            "currency": "CLP",
        }
    ]
    assert cards == [
        {
            "cardId": "0000000000009876",
            "commercialGroup": "SYNTHETIC CARD GROUP",
            "contractNumber": "000000009876",
            "contractOffice": "4321",
            "currency": "USD",
        }
    ]


def test_scrape_maps_synthetic_current_account_movements_and_closes_session():
    session = FakeSession(
        responses=[
            FakeResponse(payload=load_fixture("auth_success.json")),
            FakeResponse(payload=load_fixture("current_account_transactions.json")),
        ]
    )
    scraper = SantanderScraper(
        session_factory=lambda: session,
        challenge_provider=challenge_headers,
        today_provider=lambda: date(2026, 8, 27),
    )

    movements = scraper.scrape(SENSITIVE_RUT, SENSITIVE_PASSWORD)

    assert session.closed is True
    assert [(movement.description, str(movement.amount)) for movement in movements] == [
        ("SYNTHETIC DEBIT MOVEMENT", "-12345"),
        ("SYNTHETIC CREDIT MOVEMENT", "50000"),
    ]
    assert {movement.account_id for movement in movements} == {"5678"}
    assert {movement.account_type for movement in movements} == {"corriente"}
    assert {movement.currency for movement in movements} == {"CLP"}

    url, kwargs = session.post_calls[1]
    assert url == CURRENT_ACCOUNT_TRANSACTIONS_URL
    assert kwargs["impersonate"] == "chrome"
    assert kwargs["json"] == {
        "accountId": "0123000012345678",
        "currency": "CLP",
        "commercialGroup": "",
        "openingDate": "2026-05-29",
        "closingDate": "2026-08-27",
    }
    assert kwargs["headers"] == {
        "Authorization": "Bearer SYNTHETIC_ACCESS_TOKEN",
        "Content-Type": "application/json",
        "X-Client-Code": "STD-PER-FPP",
        "X-Organization-Code": "Santander",
        "X-Santander-Client-Id": "33N9W6H2qf2G9mGbOeQnal68IqlteL7L",
        "x-B3-SpanId": "AL43243287438243P",
        "x-schema-id": "GHOBP",
    }


def test_scrape_posts_credit_card_contract_and_maps_debits_and_refunds():
    session = FakeSession(
        responses=[
            FakeResponse(payload=load_fixture("auth_credit_success.json")),
            FakeResponse(payload=load_fixture("credit_card_unbilled.json")),
            FakeResponse(
                payload={
                    "DATA": {
                        "AS_TIB_WM01_CONCuentasDisponibles": {
                            "INFO": {"CODERR": "04"},
                            "OUTPUT": {"MATRIZ": []},
                        }
                    }
                }
            ),
        ]
    )
    scraper = SantanderScraper(
        session_factory=lambda: session,
        challenge_provider=challenge_headers,
    )

    movements = scraper.scrape(SENSITIVE_RUT, SENSITIVE_PASSWORD)

    assert [(movement.description, str(movement.amount)) for movement in movements] == [
        ("SYNTHETIC CARD PURCHASE", "-15990"),
        ("SYNTHETIC CARD REFUND", "2000"),
    ]
    assert {movement.account_id for movement in movements} == {"9876"}
    assert {movement.account_type for movement in movements} == {"credito"}

    url, kwargs = session.post_calls[1]
    assert url == CREDIT_CARD_UNBILLED_URL
    assert kwargs["json"] == {
        "Cabecera": {
            "HOST": {
                "USUARIO-ALT": "GHOBP",
                "TERMINAL-ALT": "",
                "CANAL-ID": "078",
            },
            "CanalFisico": "003",
            "CanalLogico": "74",
            "RutCliente": "00123456785",
            "RutUsuario": "00123456785",
            "IpCliente": "",
            "InfoDispositivo": "InfoDispositivo",
        },
        "Entrada": {
            "Entidad": "0035",
            "Centro": "4321",
            "Cuenta": "000000009876",
            "Moneda": "CLP",
        },
    }
    assert kwargs["headers"]["X-Santander-Client-Id"] == (
        "O2XRSU4kVspEGbLDDGfFC5BOTrGKh5Ts"
    )


def test_credit_card_periods_map_national_and_international_billed_movements():
    periods = {
        "DATA": {
            "AS_TIB_WM01_CONCuentasDisponibles": {
                "INFO": {"CODERR": "00"},
                "OUTPUT": {
                    "MATRIZ": [
                        {
                            "CODENT": "0035",
                            "CENTALT": "4321",
                            "CUENTA": "000000009876",
                            "NUMEXT": "100",
                            "MONEDA": "CLP",
                        },
                        {
                            "CODENT": "0035",
                            "CENTALT": "4321",
                            "CUENTA": "000000009876",
                            "NUMEXT": "101",
                            "MONEDA": "USD",
                        },
                    ]
                },
            }
        }
    }
    national = {
        "DATA": {
            "AS_TIB_WM02_CONEstCtaNacional_Response": {
                "OUTPUT": {
                    "Matriz": [
                        {
                            "FechaTxs": "20/08/2026",
                            "MontoTxs": "10.000",
                            "NombreComercio": "SYNTHETIC NATIONAL",
                        }
                    ]
                }
            }
        }
    }
    international = {
        "DATA": {
            "AS_TIB_WM03_CONEstCtaInternacional_Response": {
                "OUTPUT": {
                    "MATRIZDATOS": [
                        {
                            "FechaTxs": "21/08/2026",
                            "MontoTransaccion": "12,50",
                            "NombreComercio": "SYNTHETIC INTERNATIONAL",
                        }
                    ]
                }
            }
        }
    }
    session = FakeSession(
        responses=[
            FakeResponse(payload=load_fixture("auth_credit_success.json")),
            FakeResponse(payload={"DATA": {"MatrizMovimientos": []}}),
            FakeResponse(payload=periods),
            FakeResponse(payload=national),
            FakeResponse(payload=international),
        ]
    )
    scraper = SantanderScraper(
        session_factory=lambda: session,
        challenge_provider=challenge_headers,
    )

    movements = scraper.scrape(SENSITIVE_RUT, SENSITIVE_PASSWORD)

    assert [(item.currency, item.description, str(item.amount)) for item in movements] == [
        ("CLP", "SYNTHETIC NATIONAL", "-10000"),
        ("USD", "SYNTHETIC INTERNATIONAL", "-12.50"),
    ]
    assert session.post_calls[3][0] == CREDIT_CARD_NATIONAL_URL
    assert session.post_calls[3][1]["json"]["INPUT"]["CentAlt"] == "4321"
    assert session.post_calls[4][0] == CREDIT_CARD_INTERNATIONAL_URL
    assert session.post_calls[4][1]["json"]["INPUT"]["CentAlta"] == "4321"


@pytest.mark.parametrize(
    "payload_path",
    [
        ("data", "MovimientosDepositos"),
        ("data", "Movimientos"),
        ("data", "movements"),
        ("data", "DATA", "Movimientos"),
        ("data", "DATA", "MovimientosDepositos"),
    ],
)
def test_current_account_parser_accepts_all_public_response_variants(payload_path):
    movement = {
        "fecha": "27/08/2026",
        "glosa": "SYNTHETIC VARIANT",
        "monto": "1.234",
        "tipoMovimiento": "ABONO",
    }
    payload = {}
    cursor = payload
    for key in payload_path[:-1]:
        cursor[key] = {}
        cursor = cursor[key]
    cursor[payload_path[-1]] = [movement]

    parsed = SantanderScraper()._parse_current_account_movements(
        payload,
        account_id="000000001111",
        currency="CLP",
    )

    assert len(parsed) == 1
    assert parsed[0].amount == 1234
    assert parsed[0].account_id == "1111"


@pytest.mark.parametrize("status_code", [302, 400, 401, 403])
def test_auth_rejections_are_login_errors_without_leaking_provider_body(
    status_code,
    capsys,
):
    provider_body = (
        f"rut={SENSITIVE_RUT} password={SENSITIVE_PASSWORD} movement=SENSITIVE_BODY"
    )
    session = FakeSession(
        responses=[FakeResponse(status_code=status_code, payload={}, text=provider_body)]
    )
    scraper = SantanderScraper(
        session_factory=lambda: session,
        challenge_provider=challenge_headers,
    )

    with pytest.raises(LoginError, match="Santander rejected the authentication"):
        scraper.scrape(SENSITIVE_RUT, SENSITIVE_PASSWORD)

    captured = capsys.readouterr()
    assert SENSITIVE_RUT not in captured.out + captured.err
    assert SENSITIVE_PASSWORD not in captured.out + captured.err
    assert "SENSITIVE_BODY" not in captured.out + captured.err


def test_invalid_json_is_classified_without_echoing_response(capsys):
    session = FakeSession(
        responses=[
            FakeResponse(
                payload=ValueError("SENSITIVE_PROVIDER_BODY"),
                text="SENSITIVE_PROVIDER_BODY",
            )
        ]
    )
    scraper = SantanderScraper(
        session_factory=lambda: session,
        challenge_provider=challenge_headers,
    )

    with pytest.raises(DataExtractionError, match="invalid JSON"):
        scraper.scrape(SENSITIVE_RUT, SENSITIVE_PASSWORD)

    captured = capsys.readouterr()
    assert "SENSITIVE_PROVIDER_BODY" not in captured.out + captured.err


def test_public_challenge_provider_discovers_current_token_from_public_js_only():
    frame_html = '<script src="main.synthetic.js" defer></script>'
    runtime_js = 'e.u=o=>o+"."+{4451:"synthetichash"}[o]+".js"'
    app_js = 'headers.push({key:"tokentbk",value:"TOKEN@SYNTHETIC_PUBLIC"})'
    session = FakeSession(
        get_responses=[
            FakeResponse(text=frame_html),
            FakeResponse(text=runtime_js),
            FakeResponse(text=app_js),
        ]
    )

    headers = PublicJavascriptChallengeProvider()(session)

    assert headers == {"tokentbk": "TOKEN@SYNTHETIC_PUBLIC"}
    assert all(call[1]["impersonate"] == "chrome" for call in session.get_calls)


def test_missing_public_challenge_fails_before_sending_credentials():
    session = FakeSession(get_responses=[FakeResponse(text="no scripts")])
    scraper = SantanderScraper(
        session_factory=lambda: session,
        challenge_provider=PublicJavascriptChallengeProvider(),
    )

    with pytest.raises(DataExtractionError, match="public login challenge"):
        scraper.scrape(SENSITIVE_RUT, SENSITIVE_PASSWORD)

    assert session.post_calls == []
