from importlib import import_module
from typing import Any, Dict, Optional, Tuple

from fintself.core.exceptions import ScraperNotFound
from fintself.utils.logging import logger

_SCRAPERS: Dict[str, Tuple[str, str]] = {
    "cl_santander": ("fintself.scrapers.cl.santander", "SantanderScraper"),
    "cl_cencosud": ("fintself.scrapers.cl.cencosud", "CencosudScraper"),
    "cl_banco_chile": ("fintself.scrapers.cl.banco_chile", "BancoChileScraper"),
    "cl_estado": ("fintself.scrapers.cl.estado", "BancoEstadoScraper"),
    "cl_bice": ("fintself.scrapers.cl.bice", "BiceScraper"),
}


def get_scraper(
    bank_id: str, headless: Optional[bool] = None, debug_mode: Optional[bool] = None
) -> Any:
    """Return a scraper instance without importing unrelated browser runtimes."""
    scraper_path = _SCRAPERS.get(bank_id)
    if not scraper_path:
        logger.error(f"Scraper '{bank_id}' not found.")
        raise ScraperNotFound(bank_id)

    module_name, class_name = scraper_path
    scraper_class = getattr(import_module(module_name), class_name)
    logger.debug(
        f"Instantiating scraper for '{bank_id}'. "
        f"Debug override: {debug_mode}, Headless override: {headless}"
    )
    return scraper_class(headless=headless, debug_mode=debug_mode)


def list_available_scrapers() -> Dict[str, str]:
    """List all registered bank scraper identifiers and descriptions."""
    descriptions = {
        "cl_santander": "Scraper for Banco Santander (Chile).",
        "cl_cencosud": "Scraper for Tarjeta Cencosud Scotiabank (Chile).",
        "cl_banco_chile": "Scraper for Banco de Chile (Chile).",
        "cl_estado": "Scraper for Cuenta RUT Banco Estado (Chile).",
        "cl_bice": "Scraper for Banco Bice (Chile).",
    }
    return {bank_id: descriptions[bank_id] for bank_id in _SCRAPERS}
