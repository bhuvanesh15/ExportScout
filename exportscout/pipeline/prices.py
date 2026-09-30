"""Retail price ladder and FOB quote range (plan §6.1). Deterministic; no I/O.

OWNER: agent B.
"""
from __future__ import annotations

import math
import statistics
from typing import Any

from exportscout.models import ChannelStats, Listing, PriceLadder, QuoteRange, Verdict

# Margin thresholds for the verdict, judged at the direct-to-retailer FOB price.
GO_MARGIN = 0.25
TIGHT_MARGIN = 0.10

# Prices further than this factor from the raw median are dropped as outliers.
OUTLIER_FACTOR = 5.0
MIN_PRICES = 3

ASSUMPTION_KEYS = ("vat_rate", "retailer_markup", "importer_markup", "freight_ins_pct", "duty_pct")


def quantile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolation quantile (numpy's default "linear" method).

    pos = (n - 1) * q;  value = v[floor(pos)] + (v[ceil(pos)] - v[floor(pos)]) * frac(pos)
    """
    if not sorted_values:
        raise ValueError("quantile of an empty list")
    pos = (len(sorted_values) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _stats(prices: list[float]) -> ChannelStats:
    s = sorted(prices)
    return ChannelStats(
        n=len(s),
        p25=round(quantile(s, 0.25), 2),
        p50=round(quantile(s, 0.50), 2),
        p75=round(quantile(s, 0.75), 2),
    )


def price_ladder(listings: list[Listing], currency: str) -> PriceLadder | None:
    """P25/P50/P75 overall and by channel, from priced listings in ``currency``. None if < 3 prices.

    Outliers (price > 5 x raw median or < raw median / 5) are dropped before the percentiles.
    """
    cur = currency.upper()
    priced = [
        (lst.price, lst)
        for lst in listings
        if lst.price is not None and lst.price > 0 and (lst.currency or "").upper() == cur
    ]
    if not priced:
        return None
    median = statistics.median(p for p, _ in priced)
    kept = [(p, lst) for p, lst in priced if median / OUTLIER_FACTOR <= p <= median * OUTLIER_FACTOR]
    if len(kept) < MIN_PRICES:
        return None

    overall = sorted(p for p, _ in kept)
    by_channel: dict[str, list[float]] = {}
    for p, lst in kept:
        by_channel.setdefault(lst.channel, []).append(p)
    stats = _stats(overall)
    return PriceLadder(
        currency=cur,
        n=stats.n,
        min=round(overall[0], 2),
        p25=stats.p25,
        p50=stats.p50,
        p75=stats.p75,
        max=round(overall[-1], 2),
        by_channel={ch: _stats(p) for ch, p in by_channel.items()},
        evidence_ids=list(dict.fromkeys(lst.evidence_id for _, lst in kept)),
    )


def assumptions_for(market: dict[str, Any], overrides: dict[str, float] | None = None) -> dict[str, float]:
    """Pricing assumptions from the market config, with any user ``overrides`` applied."""
    base = {
        "vat_rate": float(market["vat_rate"]),
        "retailer_markup": float(market["markups"]["retailer"]),
        "importer_markup": float(market["markups"]["importer"]),
        "freight_ins_pct": float(market["freight_ins_pct"]),
        "duty_pct": float(market["duty_india"]),
    }
    for key, value in (overrides or {}).items():
        if key in ASSUMPTION_KEYS and value is not None:
            base[key] = float(value)
    return base


def verdict_for(margin: float | None) -> Verdict:
    """"go" if margin >= 25%, "tight" if >= 10%, "no_go" below, "unknown" if there is no margin."""
    if margin is None:
        return "unknown"
    if margin >= GO_MARGIN:
        return "go"
    if margin >= TIGHT_MARGIN:
        return "tight"
    return "no_go"


def _margin(fob_inr: float | None, cost: float | None) -> float | None:
    if fob_inr is None or cost is None or fob_inr <= 0:
        return None
    return round((fob_inr - cost) / fob_inr, 3)


def fob_quote(
    ladder: PriceLadder,
    market: dict[str, Any],
    *,
    fx_rate: float | None,
    unit_cost_inr: float | None,
    extra_costs_inr: float = 0.0,
    overrides: dict[str, float] | None = None,
) -> QuoteRange:
    """Work back from median retail to the FOB range and compare with the owner's cost.

    retail_ex_vat = median / (1 + vat)
    fob_retailer  = retail_ex_vat / retailer_markup / (1 + freight_ins_pct + duty_pct)
    fob_importer  = retail_ex_vat / retailer_markup / importer_markup / (1 + freight_ins_pct + duty_pct)
    margin        = (fob_inr - (unit_cost + extra)) / fob_inr
    """
    a = assumptions_for(market, overrides)
    retail_ex_vat = ladder.p50 / (1 + a["vat_rate"])
    fob_retailer = retail_ex_vat / a["retailer_markup"] / (1 + a["freight_ins_pct"] + a["duty_pct"])
    fob_importer = fob_retailer / a["importer_markup"]

    fx = fx_rate if fx_rate and fx_rate > 0 else None
    retailer_inr = round(fob_retailer * fx, 2) if fx else None
    importer_inr = round(fob_importer * fx, 2) if fx else None
    total_cost = round(unit_cost_inr + (extra_costs_inr or 0.0), 2) if unit_cost_inr is not None else None
    margin_retailer = _margin(retailer_inr, total_cost)

    return QuoteRange(
        currency=ladder.currency,
        retail_median=round(ladder.p50, 2),
        retail_ex_vat=round(retail_ex_vat, 2),
        fob_importer=round(fob_importer, 2),
        fob_retailer=round(fob_retailer, 2),
        fx_rate=fx,
        fob_importer_inr=importer_inr,
        fob_retailer_inr=retailer_inr,
        unit_cost_inr=unit_cost_inr,
        total_cost_inr=total_cost,
        margin_retailer=margin_retailer,
        margin_importer=_margin(importer_inr, total_cost),
        verdict=verdict_for(margin_retailer),
        assumptions=a,
    )
