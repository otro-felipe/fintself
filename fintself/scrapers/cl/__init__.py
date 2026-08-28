"""Lazily exposed Chilean scrapers."""

from importlib import import_module

_SCRAPER_MODULES = {
    "BancoChileScraper": "fintself.scrapers.cl.banco_chile",
    "BancoEstadoScraper": "fintself.scrapers.cl.estado",
    "BiceScraper": "fintself.scrapers.cl.bice",
    "CencosudScraper": "fintself.scrapers.cl.cencosud",
    "SantanderScraper": "fintself.scrapers.cl.santander",
}

__all__ = list(_SCRAPER_MODULES)


def __getattr__(name: str):
    module_name = _SCRAPER_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    scraper_class = getattr(import_module(module_name), name)
    globals()[name] = scraper_class
    return scraper_class
