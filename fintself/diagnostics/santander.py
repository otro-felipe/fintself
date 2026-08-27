"""Run a privacy-preserving, visible Santander diagnostic locally."""

import contextlib
import subprocess
import sys
from getpass import getpass
from typing import Callable, Optional, Sequence, Tuple

from fintself.core.exceptions import DataExtractionError, LoginError
from fintself.scrapers import get_scraper
from fintself.utils.logging import logger


SUCCESS = 0
LOGIN_ERROR = 2
DATA_EXTRACTION_ERROR = 3
UNEXPECTED_ERROR = 4
INPUT_ERROR = 5
INVALID_ARGUMENTS = 64


class _DiscardStream:
    """A text stream that discards writes without retaining their contents."""

    encoding = "utf-8"

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


def _read_password() -> str:
    return getpass("")


def _read_terminal_credentials(
    input_func: Callable[[], str],
    getpass_func: Callable[[], str],
) -> Tuple[str, str]:
    print("stage=credentials status=prompt_rut")
    user = input_func()
    print("stage=credentials status=prompt_password")
    password = getpass_func()
    return user, password


def _read_hidden_mac_value(
    label: str,
    dialog_runner: Callable[..., subprocess.CompletedProcess],
) -> str:
    script = (
        f'set dialogResult to display dialog "{label}" default answer "" '
        'with hidden answer buttons {"Cancelar", "Continuar"} '
        'default button "Continuar" cancel button "Cancelar" '
        'with title "Diagnóstico seguro de Santander"\n'
        "return text returned of dialogResult\n"
    )
    result = dialog_runner(
        ["/usr/bin/osascript"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("dialog_cancelled")
    return result.stdout.rstrip("\r\n")


def _read_mac_dialog_credentials(
    dialog_runner: Callable[..., subprocess.CompletedProcess],
) -> Tuple[str, str]:
    user = _read_hidden_mac_value("Ingresa tu RUT", dialog_runner)
    password = _read_hidden_mac_value("Ingresa tu clave", dialog_runner)
    return user, password


@contextlib.contextmanager
def _silence_scraper_output():
    """Suppress scraper and third-party output without buffering its contents."""
    discard = _DiscardStream()
    logger.disable("fintself")
    try:
        with contextlib.redirect_stdout(discard), contextlib.redirect_stderr(discard):
            yield
    finally:
        logger.enable("fintself")


def _clear_scraper_credentials(scraper) -> None:
    if scraper is None:
        return
    for attribute in ("user", "password"):
        try:
            setattr(scraper, attribute, None)
        except Exception:
            pass


def run_diagnostic(
    *,
    use_mac_dialog: bool = False,
    input_func: Callable[[], str] = input,
    getpass_func: Callable[[], str] = _read_password,
    dialog_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    scraper_factory: Callable = get_scraper,
) -> int:
    """Run Santander visibly and emit only fixed stage/status fields."""
    user: Optional[str] = None
    password: Optional[str] = None
    scraper = None

    try:
        if use_mac_dialog:
            user, password = _read_mac_dialog_credentials(dialog_runner)
        else:
            user, password = _read_terminal_credentials(input_func, getpass_func)
    except (Exception, KeyboardInterrupt):
        print("stage=credentials status=input_error")
        return INPUT_ERROR

    if not user.strip() or not password:
        user = None
        password = None
        print("stage=credentials status=input_error")
        return INPUT_ERROR

    print("stage=credentials status=ready")
    print("stage=scrape status=started")

    try:
        with _silence_scraper_output():
            scraper = scraper_factory(
                "cl_santander",
                headless=False,
                debug_mode=False,
            )
            movements = scraper.scrape(user=user, password=password)
            movement_count = len(movements)
            movements = None
    except LoginError:
        print("stage=scrape status=LoginError")
        return LOGIN_ERROR
    except DataExtractionError:
        print("stage=scrape status=DataExtractionError")
        return DATA_EXTRACTION_ERROR
    except Exception:
        print("stage=scrape status=unexpected_error")
        return UNEXPECTED_ERROR
    finally:
        with _silence_scraper_output():
            _clear_scraper_credentials(scraper)
        user = None
        password = None

    print(f"stage=scrape status=success movement_count={movement_count}")
    return SUCCESS


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == []:
        return run_diagnostic()
    if arguments == ["--mac-dialog"]:
        return run_diagnostic(use_mac_dialog=True)

    # Do not let a parser echo rejected values: someone might paste a credential.
    print("stage=arguments status=invalid")
    return INVALID_ARGUMENTS


def cli() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    cli()
