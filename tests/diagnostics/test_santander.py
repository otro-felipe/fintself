import subprocess

import pytest

from fintself.core.exceptions import DataExtractionError, LoginError


RUT = "SENSITIVE_RUT_SENTINEL"
PASSWORD = "SENSITIVE_PASSWORD_SENTINEL"
ACCOUNT_ID = "SENSITIVE_ACCOUNT_SENTINEL"
MOVEMENT = "SENSITIVE_MOVEMENT_SENTINEL"


class FakeScraper:
    def __init__(self, result=None, error=None):
        self.result = [] if result is None else result
        self.error = error
        self.user = None
        self.password = None

    def scrape(self, user, password):
        self.user = user
        self.password = password
        print(f"internal rut={user} account={ACCOUNT_ID} movement={MOVEMENT}")
        import sys

        print(f"internal password={password}", file=sys.stderr)
        if self.error:
            raise self.error
        return self.result


def assert_sensitive_values_are_absent(captured):
    combined = captured.out + captured.err
    for sensitive_value in (RUT, PASSWORD, ACCOUNT_ID, MOVEMENT):
        assert sensitive_value not in combined


def prompt_credentials():
    return {
        "input_func": lambda: RUT,
        "getpass_func": lambda: PASSWORD,
    }


def test_success_uses_visible_non_debug_scraper_and_reports_only_count(capsys):
    from fintself.diagnostics.santander import run_diagnostic

    scraper = FakeScraper(result=[object(), object()])
    calls = []

    def scraper_factory(bank_id, **kwargs):
        calls.append((bank_id, kwargs))
        return scraper

    exit_code = run_diagnostic(
        scraper_factory=scraper_factory,
        **prompt_credentials(),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.splitlines() == [
        "stage=credentials status=prompt_rut",
        "stage=credentials status=prompt_password",
        "stage=credentials status=ready",
        "stage=scrape status=started",
        "stage=scrape status=success movement_count=2",
    ]
    assert captured.err == ""
    assert calls == [
        ("cl_santander", {"headless": False, "debug_mode": False}),
    ]
    assert scraper.user is None
    assert scraper.password is None
    assert_sensitive_values_are_absent(captured)


@pytest.mark.parametrize(
    ("error", "status", "exit_code"),
    [
        (
            LoginError(
                f"bad login rut={RUT} password={PASSWORD} account={ACCOUNT_ID}"
            ),
            "LoginError",
            2,
        ),
        (
            DataExtractionError(
                f"failed movement={MOVEMENT} account={ACCOUNT_ID} rut={RUT}"
            ),
            "DataExtractionError",
            3,
        ),
        (
            RuntimeError(
                f"unexpected password={PASSWORD} movement={MOVEMENT} "
                f"account={ACCOUNT_ID}"
            ),
            "unexpected_error",
            4,
        ),
    ],
)
def test_provider_errors_are_classified_without_raw_messages_or_logs(
    error,
    status,
    exit_code,
    capsys,
):
    from fintself.diagnostics.santander import run_diagnostic

    scraper = FakeScraper(error=error)

    result = run_diagnostic(
        scraper_factory=lambda *_args, **_kwargs: scraper,
        **prompt_credentials(),
    )

    captured = capsys.readouterr()
    assert result == exit_code
    assert captured.out.splitlines()[-1] == f"stage=scrape status={status}"
    assert captured.err == ""
    assert_sensitive_values_are_absent(captured)


def test_empty_or_interrupted_credentials_stop_before_scraper(capsys):
    from fintself.diagnostics.santander import run_diagnostic

    scraper_factory_called = False

    def scraper_factory(*_args, **_kwargs):
        nonlocal scraper_factory_called
        scraper_factory_called = True
        raise AssertionError("must not create scraper")

    exit_code = run_diagnostic(
        scraper_factory=scraper_factory,
        input_func=lambda: RUT,
        getpass_func=lambda: "",
    )

    captured = capsys.readouterr()
    assert exit_code == 5
    assert captured.out.splitlines()[-1] == "stage=credentials status=input_error"
    assert captured.err == ""
    assert scraper_factory_called is False


def test_mac_dialog_hides_both_values_and_never_passes_them_to_process_metadata(
    capsys,
):
    from fintself.diagnostics.santander import run_diagnostic

    calls = []
    responses = iter((RUT, PASSWORD))

    def dialog_runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=next(responses) + "\n",
            stderr="",
        )

    scraper = FakeScraper(result=[])
    exit_code = run_diagnostic(
        use_mac_dialog=True,
        dialog_runner=dialog_runner,
        scraper_factory=lambda *_args, **_kwargs: scraper,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.splitlines() == [
        "stage=credentials status=ready",
        "stage=scrape status=started",
        "stage=scrape status=success movement_count=0",
    ]
    assert captured.err == ""
    assert len(calls) == 2
    for command, kwargs in calls:
        assert command == ["/usr/bin/osascript"]
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False
        assert "with hidden answer" in kwargs["input"]
        serialized_call = repr((command, kwargs))
        assert RUT not in serialized_call
        assert PASSWORD not in serialized_call
    assert_sensitive_values_are_absent(captured)


def test_invalid_cli_arguments_are_rejected_without_echoing_them(capsys):
    from fintself.diagnostics.santander import main

    exit_code = main([PASSWORD])

    captured = capsys.readouterr()
    assert exit_code == 64
    assert captured.out == "stage=arguments status=invalid\n"
    assert captured.err == ""
    assert_sensitive_values_are_absent(captured)
