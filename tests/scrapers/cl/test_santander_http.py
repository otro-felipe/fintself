import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from fintself.core.exceptions import DataExtractionError, LoginError
from fintself.scrapers.base import BaseScraper
from fintself.scrapers.cl.santander import (
    CREDIT_CARD_INTERNATIONAL_URL,
    CREDIT_CARD_NATIONAL_URL,
    CREDIT_CARD_UNBILLED_URL,
    CURRENT_ACCOUNT_TRANSACTIONS_URL,
    TOKEN_URL,
    NodeAkamaiTelemetryProvider,
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
                        "NUMEROPAN": "00000000000000001",
                        "OFICINACONTRATO": "0123",
                        "PRODUCTO": "CUENTA",
                        "SUBPRODUCTO": "CORRIENTE",
                    },
                    {
                        "AGRUPACIONCOMERCIAL": "SYNTHETIC CARD GROUP",
                        "CODIGOMONEDA": "USD",
                        "GLOSACORTA": "VISA WORLD",
                        "NUMEROCONTRATO": "000000009876",
                        "NUMEROPAN": "0000000000009876",
                        "OFICINACONTRATO": "4321",
                        "PRODUCTO": "TARJETA",
                        "SUBPRODUCTO": "CREDITO",
                    },
                    {
                        "GLOSACORTA": "CREDITO DE CONSUMO",
                        "NUMEROCONTRATO": "SHOULD_NOT_BE_SCRAPED",
                        "NUMEROPAN": "000000000000000002",
                        "OFICINACONTRATO": "9999",
                        "PRODUCTO": "PRESTAMO",
                        "SUBPRODUCTO": "CONSUMO",
                    },
                    {
                        "AGRUPACIONCOMERCIAL": "LCR",
                        "GLOSACORTA": "LINEA DE CREDITO LCR",
                        "NUMEROCONTRATO": "000000001111",
                        "NUMEROPAN": "000000000000000003",
                        "OFICINACONTRATO": "0123",
                        "PRODUCTO": "LINEA",
                        "SUBPRODUCTO": "LCR",
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
        },
        {
            "accountId": "0123000000001111",
            "commercialGroup": "LCR",
            "contractNumber": "000000001111",
            "contractOffice": "0123",
            "currency": "CLP",
        },
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


def test_scrape_uses_official_current_account_contract_and_closes_session():
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
        ("SYNTHETIC EXPANDED DEBIT", "-12345"),
        ("SYNTHETIC CREDIT OBSERVATION", "50000"),
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
        "openingDate": "2026-07-18",
        "closingDate": "2026-09-15",
    }
    assert kwargs["headers"] == {
        "Authorization": "Bearer SYNTHETIC_ACCESS_TOKEN",
        "Content-Type": "application/json",
        "X-Client-Code": "STD-PER-FPP",
        "X-Organization-Code": "Santander",
        "X-Santander-Client-Id": "O2XRSU4kVspEGbLDDGfFC5BOTrGKh5Ts",
        "x-B3-SpanId": "AL43243287438243P",
        "x-schema-id": "GHOBP",
    }


def test_current_account_paginates_with_repositioning_and_maps_lcr_to_lca():
    products = {
        "currentAccounts": [
            {
                "contractOffice": "0123",
                "contractNumber": "000012345678",
                "currency": "CLP",
                "commercialGroup": "LCR",
            }
        ]
    }
    first_page = {
        "data": {
            "movements": [],
            "repositioningExit": {
                "startMovement": "SYNTHETIC_START",
                "endMovement": "SYNTHETIC_END",
            },
        }
    }
    second_page = {
        "data": {
            "movements": [
                {
                    "movementAmount": "1.250",
                    "observation": "SYNTHETIC NEXT PAGE",
                    "accountingDate": "2026-08-26",
                    "movementNumber": "SYNTHETIC_MOVEMENT_NUMBER",
                    "chargePaymentFlag": "P",
                }
            ]
        }
    }
    session = FakeSession(
        responses=[FakeResponse(payload=first_page), FakeResponse(payload=second_page)]
    )
    scraper = SantanderScraper(today_provider=lambda: date(2026, 12, 30))
    auth = SimpleNamespace(access_token="SYNTHETIC_ACCESS_TOKEN", products=products)

    movements = scraper._scrape_current_accounts(session, auth)

    assert [(movement.description, str(movement.amount)) for movement in movements] == [
        ("SYNTHETIC NEXT PAGE", "1250")
    ]
    first_payload = session.post_calls[0][1]["json"]
    assert first_payload == {
        "accountId": "0123000012345678",
        "currency": "CLP",
        "commercialGroup": "LCA",
        "openingDate": "2026-11-20",
        "closingDate": "2027-01-15",
    }
    assert session.post_calls[1][1]["json"] == {
        **first_payload,
        "startMovement": "SYNTHETIC_START",
        "endMovement": "SYNTHETIC_END",
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

    received = []
    headers = PublicJavascriptChallengeProvider(
        telemetry_provider=lambda html: received.append(html)
        or "SYNTHETIC_EPHEMERAL_TELEMETRY"
    )(session)

    assert headers == {
        "tokentbk": "TOKEN@SYNTHETIC_PUBLIC",
        "Akamai-BM-Telemetry": "SYNTHETIC_EPHEMERAL_TELEMETRY",
    }
    assert received == [frame_html]
    assert all(call[1]["impersonate"] == "chrome" for call in session.get_calls)


def test_node_telemetry_is_passed_only_over_stdin_and_captured_in_memory(capsys):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="SYNTHETIC_EPHEMERAL_TELEMETRY",
            stderr="SENSITIVE_INTERNAL_RUNTIME_DETAIL",
        )

    provider = NodeAkamaiTelemetryProvider(
        node_binary="/synthetic/node",
        subprocess_runner=run,
    )

    telemetry = provider('<script src="/akamai.js"></script>')

    assert telemetry == "SYNTHETIC_EPHEMERAL_TELEMETRY"
    command, kwargs = calls[0]
    assert command[0] == "/synthetic/node"
    assert command[1].endswith("akamai_runtime/telemetry.mjs")
    assert json.loads(kwargs["input"]) == {
        "frameUrl": "https://mibanco.santander.cl/UI.Web.HB/Private_new/frame/",
        "html": '<script src="/akamai.js"></script>',
    }
    assert kwargs == {
        "input": kwargs["input"],
        "capture_output": True,
        "text": True,
        "timeout": 30,
        "check": False,
    }
    captured = capsys.readouterr()
    assert telemetry not in captured.out + captured.err
    assert "SENSITIVE_INTERNAL_RUNTIME_DETAIL" not in captured.out + captured.err


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (FileNotFoundError(), "Node.js and jsdom are required"),
        (
            subprocess.TimeoutExpired("node", 30),
            "Unable to generate Santander telemetry",
        ),
        (
            SimpleNamespace(returncode=78, stdout="", stderr="SENSITIVE_MISSING_JSDOM"),
            "Node.js and jsdom are required",
        ),
        (
            SimpleNamespace(returncode=1, stdout="", stderr="SENSITIVE_RUNTIME_ERROR"),
            "Unable to generate Santander telemetry",
        ),
        (
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            "Unable to generate Santander telemetry",
        ),
        (
            SimpleNamespace(returncode=0, stdout="x" * 16385, stderr=""),
            "Unable to generate Santander telemetry",
        ),
        (
            SimpleNamespace(returncode=0, stdout="INVALID\nHEADER", stderr=""),
            "Unable to generate Santander telemetry",
        ),
    ],
)
def test_node_telemetry_failures_are_clear_and_never_leak_runtime_output(
    result, message, capsys
):
    def run(*_args, **_kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    provider = NodeAkamaiTelemetryProvider(subprocess_runner=run)

    with pytest.raises(DataExtractionError, match=message):
        provider("SYNTHETIC_PUBLIC_HTML")

    captured = capsys.readouterr()
    assert "SENSITIVE_" not in captured.out + captured.err


@pytest.mark.parametrize(
    "responses",
    [
        [FakeResponse(status_code=503)],
        [FakeResponse(text="no main bundle")],
        [
            FakeResponse(text='<script src="main.synthetic.js"></script>'),
            FakeResponse(status_code=503),
        ],
        [
            FakeResponse(text='<script src="main.synthetic.js"></script>'),
            FakeResponse(text="no chunk"),
        ],
        [
            FakeResponse(text='<script src="main.synthetic.js"></script>'),
            FakeResponse(text='4451:"hash"'),
            FakeResponse(status_code=503),
        ],
        [
            FakeResponse(text='<script src="main.synthetic.js"></script>'),
            FakeResponse(text='4451:"hash"'),
            FakeResponse(text="no token"),
        ],
    ],
)
def test_public_challenge_stage_failures_use_only_safe_errors(responses, capsys):
    session = FakeSession(get_responses=responses)

    with pytest.raises(DataExtractionError, match="public login challenge"):
        PublicJavascriptChallengeProvider(telemetry_provider=lambda _html: "unused")(
            session
        )

    assert "SENSITIVE_" not in capsys.readouterr().out + capsys.readouterr().err


def test_public_challenge_unexpected_transport_error_is_sanitized(capsys):
    class BrokenSession:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("SENSITIVE_PUBLIC_TRANSPORT_DETAIL")

    with pytest.raises(DataExtractionError, match="public login challenge"):
        PublicJavascriptChallengeProvider()(BrokenSession())

    captured = capsys.readouterr()
    assert "SENSITIVE_PUBLIC_TRANSPORT_DETAIL" not in captured.out + captured.err


def test_core_safe_error_and_cleanup_branches_never_echo_provider_data(capsys):
    scraper = SantanderScraper(challenge_provider=challenge_headers)

    assert scraper._get_bank_id() == "cl_santander"

    with pytest.raises(DataExtractionError, match="authentication service unavailable"):
        scraper._authenticate(
            FakeSession(responses=[FakeResponse(status_code=500)]),
            SENSITIVE_RUT,
            SENSITIVE_PASSWORD,
        )
    with pytest.raises(LoginError, match="rejected the authentication"):
        scraper._authenticate(
            FakeSession(responses=[FakeResponse(payload={})]),
            SENSITIVE_RUT,
            SENSITIVE_PASSWORD,
        )

    class BrokenPostSession:
        def post(self, *_args, **_kwargs):
            raise RuntimeError("SENSITIVE_HTTP_TRANSPORT_DETAIL")

    with pytest.raises(DataExtractionError, match="HTTP request failed"):
        scraper._post(BrokenPostSession(), "https://synthetic.invalid")
    with pytest.raises(DataExtractionError, match="invalid JSON"):
        scraper._response_json(FakeResponse(payload=[]))
    with pytest.raises(DataExtractionError, match="movement service unavailable"):
        scraper._post_json(
            FakeSession(responses=[FakeResponse(status_code=503)]),
            "https://synthetic.invalid",
            {},
            {},
        )

    class BrokenCloseSession(FakeSession):
        def close(self):
            raise RuntimeError("SENSITIVE_CLOSE_DETAIL")

    close_session = BrokenCloseSession(
        responses=[FakeResponse(payload={"access_token": "SYNTHETIC_ACCESS_TOKEN"})]
    )
    assert (
        SantanderScraper(
            session_factory=lambda: close_session,
            challenge_provider=challenge_headers,
        ).scrape(SENSITIVE_RUT, SENSITIVE_PASSWORD)
        == []
    )

    captured = capsys.readouterr()
    assert "SENSITIVE_" not in captured.out + captured.err


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"ready": True}, {"ready": True}),
        ('{"ready": true}', {"ready": True}),
        ("not-json", {}),
        ("[]", {}),
        (None, {}),
    ],
)
def test_json_product_container_accepts_only_objects(value, expected):
    assert SantanderScraper._json_object(value) == expected


def test_product_discovery_handles_legacy_dicts_and_ignores_malformed_products():
    assert SantanderScraper._product_list(
        {"currentAccounts": {"first": {"accountId": "1234"}, "bad": "value"}},
        "currentAccounts",
    ) == [{"accountId": "1234"}]
    assert SantanderScraper._product_list({}, "currentAccounts") == []

    matrix = {
        "MATRICES": {
            "MATRIZCAPTACIONES": {
                "e1": [
                    "not-an-object",
                    {
                        "GLOSACORTA": "TARJETA DE CREDITO",
                        "OFICINACONTRATO": "",
                        "NUMEROCONTRATO": "1234",
                    },
                    {
                        "GLOSACORTA": "TARJETA DE CREDITO",
                        "OFICINACONTRATO": "4321",
                        "NUMEROCONTRATO": "9876",
                        "NUMEROPAN": "",
                    },
                ]
            }
        }
    }
    assert SantanderScraper._product_list(matrix, "creditCards") == [
        {
            "cardId": "9876",
            "commercialGroup": "",
            "contractNumber": "9876",
            "contractOffice": "4321",
            "currency": "CLP",
        }
    ]


def test_empty_products_and_repositioning_variants_are_safe():
    scraper = SantanderScraper(today_provider=lambda: date(2026, 8, 27))
    auth = SimpleNamespace(
        access_token="SYNTHETIC_ACCESS_TOKEN",
        products={
            "currentAccounts": [{}],
            "creditCards": [{"contractOffice": ""}],
        },
    )

    assert scraper._scrape_current_accounts(FakeSession(), auth) == []
    assert scraper._scrape_credit_cards(FakeSession(), auth, "00123456785") == []
    malformed_period_session = FakeSession(
        responses=[
            FakeResponse(payload={"DATA": {"MatrizMovimientos": []}}),
            FakeResponse(
                payload={
                    "DATA": {
                        "AS_TIB_WM01_CONCuentasDisponibles": {
                            "INFO": {"CODERR": "00"},
                            "OUTPUT": {"MATRIZ": ["not-an-object"]},
                        }
                    }
                }
            ),
        ]
    )
    assert (
        scraper._scrape_credit_card(
            malformed_period_session,
            auth,
            "00123456785",
            {"contractOffice": "4321", "contractNumber": "9876"},
        )
        == []
    )
    assert scraper._repositioning({"data": {"DATA": {"repositioningExit": {}}}}) is None
    assert scraper._repositioning({"repositioningExit": "invalid"}) is None
    assert scraper._repositioning(
        {
            "repositioningExit": {
                "startMovement": "START",
                "endMovement": "END",
            }
        }
    ) == ("START", "END")


def test_malformed_movement_rows_are_skipped_and_fallbacks_are_covered():
    scraper = SantanderScraper()
    current = scraper._parse_current_account_movements(
        {
            "data": {
                "movements": [
                    "bad-row",
                    {"transactionDate": "2026-08-20", "movementAmount": "0"},
                    {"movementAmount": "10"},
                ]
            }
        },
        "1234",
        "CLP",
    )
    assert current == []
    assert scraper._parse_billed_credit({}, "1234", "CLP") == []
    assert (
        scraper._parse_credit_movements(
            [
                "bad-row",
                {"Fecha": "2026-08-20", "Importe": "0"},
                {"Importe": "10"},
                {
                    "Fecha": "2026-08-20",
                    "Importe": "10",
                    "Descripcion": "SYNTHETIC POSITIVE",
                },
            ],
            "1234",
            "CLP",
            date_key="Fecha",
            amount_key="Importe",
            description_keys=("Descripcion",),
        )[0].amount
        == 10
    )


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"debitAmount": "1.000"}, -1000),
        ({"creditAmount": "1.000"}, 1000),
        ({"movementAmount": "1.000", "chargePaymentFlag": "CHARGE"}, -1000),
        ({"movementAmount": "1.000", "chargePaymentFlag": "PAYMENT"}, 1000),
        ({"amount": "1.000", "tipoMovimiento": "DEBITO"}, -1000),
        ({"monto": "1.000", "IndicadorDebeHaber": "C"}, 1000),
        ({"Importe": "-1.000"}, -1000),
    ],
)
def test_current_movement_amount_contract_and_legacy_fallbacks(row, expected):
    assert SantanderScraper._movement_amount(row) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("2026-08-27", date(2026, 8, 27)),
        ("2026-08-27T12:34:56", date(2026, 8, 27)),
        ("27/08/2026", date(2026, 8, 27)),
        ("invalid", None),
    ],
)
def test_current_date_contract_and_safe_fallback(value, expected):
    parsed = SantanderScraper._parse_date(value)
    assert (parsed.date() if parsed else None) == expected


def test_nested_lookup_stops_safely_on_non_objects():
    assert SantanderScraper._dig({"data": []}, "data", "nested") is None


def test_missing_public_challenge_fails_before_sending_credentials():
    session = FakeSession(get_responses=[FakeResponse(text="no scripts")])
    scraper = SantanderScraper(
        session_factory=lambda: session,
        challenge_provider=PublicJavascriptChallengeProvider(),
    )

    with pytest.raises(DataExtractionError, match="public login challenge"):
        scraper.scrape(SENSITIVE_RUT, SENSITIVE_PASSWORD)

    assert session.post_calls == []
