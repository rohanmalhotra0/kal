"""Strategy registry. Each module defines STRATEGY = SomeStrategy()."""
import importlib
import logging

MODULES = ["basket_arb", "ladder_arb", "bracket_arb", "favorites", "mentions"]
log = logging.getLogger(__name__)


def load_all():
    out = {}
    for name in MODULES:
        try:
            mod = importlib.import_module(f"{__name__}.{name}")
        except ModuleNotFoundError as e:
            if e.name != f"{__name__}.{name}":
                raise
            log.warning("strategy module %s not found", name)
            continue
        out[mod.STRATEGY.name] = mod.STRATEGY
    return out
