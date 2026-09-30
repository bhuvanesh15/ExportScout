"""Load the YAML config packs that ship with ExportScout."""
from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).parent


@cache
def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / f"{name}.yaml").read_text(encoding="utf-8"))


def market(code: str) -> dict[str, Any]:
    """Settings for one target market, e.g. ``market("uk")``."""
    markets = _load("markets")
    try:
        return markets[code]
    except KeyError:
        raise KeyError(f"unknown market {code!r}; known: {', '.join(markets)}") from None


def category(name: str) -> dict[str, Any]:
    """A product-family pack, e.g. ``category("metal_handicrafts")``."""
    categories = _load("categories")
    try:
        return categories[name]
    except KeyError:
        raise KeyError(f"unknown category {name!r}; known: {', '.join(categories)}") from None
