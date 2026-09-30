"""Market Opportunity Score (plan §6.2) and Buyer Fit Score (plan §6.3).
Deterministic; every component carries the evidence IDs behind it.

OWNER: agent B.
"""
from __future__ import annotations

import re
import statistics
from typing import Any

from exportscout.models import (
    BuyerCandidate,
    DemandCard,
    Listing,
    MarketScore,
    OriginShare,
    PriceLadder,
    ProductIdentity,
    QuoteRange,
    ScoreComponent,
)

# A buyer counts towards "buyer depth" at or above this fit score.
GOOD_FIT = 60.0

# Buyer depth is full marks at this many good-fit buyers.
DEPTH_TARGET = 8
# Amazon "bought in past month" total that earns the demand bonus.
BOUGHT_BONUS_AT = 1000
# Proven-for-India share where the score peaks.
INDIA_SWEET_SPOT = 0.3

_STOPWORDS = frozenset(
    "a an and the for with of in on to by at from set pack piece pcs uk buy sale best cheap new "
    "large small mini big home decor decorative style".split()
)
_WORD = re.compile(r"[a-z]+")
_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "INR": "₹"}


def _money(value: float, currency: str | None) -> str:
    sym = _SYMBOLS.get((currency or "").upper())
    return f"{sym}{value:,.2f}" if sym else f"{value:,.2f} {currency or ''}".rstrip()


def _pct(value: float) -> str:
    """0.027 -> "2.7%", 0.0 -> "0%"."""
    return f"{value * 100:.1f}".rstrip("0").rstrip(".") + "%"


def _component(points: float, max_points: float, detail: str, evidence: list[str] | None = None) -> ScoreComponent:
    """Clamp points to [0, max_points] and round to 1 dp."""
    return ScoreComponent(
        points=round(min(max(points, 0.0), max_points), 1),
        max_points=max_points,
        detail=detail,
        evidence_ids=list(dict.fromkeys(evidence or [])),
    )


# --------------------------------------------------------------------------- buyer fit


def _stem(word: str) -> str:
    if word.endswith(("xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _words(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(text.lower()) if len(w) >= 3 and w not in _STOPWORDS}


def product_words(product: ProductIdentity) -> set[str]:
    """Content words from the product type and retail keywords, stopwords removed, crudely singularised."""
    return _words(" ".join([product.product_type, *product.keywords]))


def _signal_ids(candidate: BuyerCandidate, kind: str) -> list[str]:
    return [s.evidence_id for s in candidate.signals if s.kind == kind]


def _first_detail(candidate: BuyerCandidate, kind: str) -> str | None:
    return next((s.detail for s in candidate.signals if s.kind == kind), None)


def _fit_category(c: BuyerCandidate, product: ProductIdentity) -> ScoreComponent:
    """25 with a category_match signal or >= 2 seen texts using product words; 12 with 1 text; else 0."""
    ev = _signal_ids(c, "category_match")
    if ev:
        return _component(25, 25, _first_detail(c, "category_match") or "Sells this product type", ev)
    wanted = product_words(product)
    hits = [wanted & _words(t) for t in c.seen_texts]
    hits = [h for h in hits if h]
    shown = ", ".join(sorted(set().union(*hits))[:3]) if hits else ""
    if len(hits) >= 2:
        return _component(25, 25, f"{len(hits)} of their listings mention {shown}")
    if len(hits) == 1:
        return _component(12, 25, f"1 listing mentions {shown}")
    return _component(0, 25, f'No listings matching "{product.product_type}" seen')


def _fit_india(c: BuyerCandidate) -> ScoreComponent:
    """20 with an india_sourcing signal, else 0."""
    ev = _signal_ids(c, "india_sourcing")
    if ev:
        return _component(20, 20, _first_detail(c, "india_sourcing") or "Already sources from India", ev)
    return _component(0, 20, "No sign of sourcing from India yet")


def _fit_price_tier(c: BuyerCandidate, ladder: PriceLadder | None, quote: QuoteRange | None) -> ScoreComponent:
    """Their median price >= market P50 -> 15; >= P25 -> 10; below -> 4; no data -> 6."""
    prices = [p for p in c.listing_prices if p and p > 0]
    ev = _signal_ids(c, "price_tier")
    if not prices or ladder is None:
        return _component(6, 15, "No price data; neutral", ev)
    cur = ladder.currency
    med = statistics.median(prices)
    theirs = f"Their median {_money(med, cur)}"
    supports = ""
    if quote:
        supports = f"; supports our {_money(quote.fob_importer, cur)}–{_money(quote.fob_retailer, cur)} FOB"
    if med >= ladder.p50:
        return _component(15, 15, f"{theirs} is at or above the market median {_money(ladder.p50, cur)}{supports}", ev)
    if med >= ladder.p25:
        return _component(10, 15, f"{theirs} is mid-market (P25 {_money(ladder.p25, cur)}){supports}", ev)
    return _component(4, 15, f"{theirs} is below the market P25 {_money(ladder.p25, cur)}: budget tier", ev)


def _fit_activity(c: BuyerCandidate) -> ScoreComponent:
    """min(15, ads_active signal 10 (else any ad creatives 4) + news signal 5 + hiring signal 5)."""
    points, parts, ev = 0.0, [], []
    if ads := _signal_ids(c, "ads_active"):
        points += 10
        parts.append(_first_detail(c, "ads_active") or "Ads running in the last 30 days")
        ev += ads
    elif (c.ad_creatives or 0) > 0:
        points += 4
        parts.append(f"{c.ad_creatives} ads on record, none recent")
    if news := _signal_ids(c, "news"):
        points += 5
        parts.append(_first_detail(c, "news") or "Recent news")
        ev += news
    if hiring := _signal_ids(c, "hiring"):
        points += 5
        parts.append(_first_detail(c, "hiring") or "Hiring a buyer")
        ev += hiring
    return _component(points, 15, "; ".join(parts) or "No recent ads, news or buying jobs found", ev)


def _fit_size(c: BuyerCandidate) -> ScoreComponent:
    """Giant/marketplace 0; unknown size 11; <= 3 locations and < 3000 reviews 15; <= 10 locations 9; else 3."""
    ev = _signal_ids(c, "size")
    if c.is_giant or c.kind == "marketplace":
        what = "a marketplace, not a buyer" if c.kind == "marketplace" else "too big for a small unit to serve"
        return _component(0, 15, f"{c.name} is {what}", ev)
    reviews, locs = c.review_count, c.locations_count
    if reviews is None and locs == 0:
        return _component(11, 15, "Size unknown; assumed small-to-mid", ev)
    parts = [f"{locs} location{'s' if locs != 1 else ''}"] if locs else []
    if reviews is not None:
        parts.append(f"{reviews:,} reviews")
    facts = ", ".join(parts)
    if locs <= 3 and (reviews is None or reviews < 3000):
        return _component(15, 15, f"Right size ({facts})", ev)
    if locs <= 10:
        return _component(9, 15, f"Mid-size ({facts})", ev)
    return _component(3, 15, f"Large chain ({facts})", ev)


def _fit_reachability(c: BuyerCandidate) -> ScoreComponent:
    """Website or domain 4 + phone 3 + trade_page signal 3."""
    points, parts = 0.0, []
    ev = _signal_ids(c, "website") + _signal_ids(c, "phone") + _signal_ids(c, "trade_page")
    if c.website or c.domain:
        points += 4
        parts.append("website")
    if c.phone:
        points += 3
        parts.append("phone")
    if _signal_ids(c, "trade_page"):
        points += 3
        parts.append("trade page")
    detail = ("Has " + ", ".join(parts)) if parts else "No website, phone or trade page found"
    return _component(points, 10, detail, ev)


def buyer_fit(
    candidate: BuyerCandidate,
    product: ProductIdentity,
    *,
    ladder: PriceLadder | None,
    quote: QuoteRange | None,
) -> BuyerCandidate:
    """Return a copy with ``fit`` components (category 25, india 20, price_tier 15,
    activity 15, size 15, reachability 10) and ``fit_score`` filled in (fit_score = sum)."""
    fit = {
        "category": _fit_category(candidate, product),
        "india": _fit_india(candidate),
        "price_tier": _fit_price_tier(candidate, ladder, quote),
        "activity": _fit_activity(candidate),
        "size": _fit_size(candidate),
        "reachability": _fit_reachability(candidate),
    }
    total = round(sum(c.points for c in fit.values()), 1)
    return candidate.model_copy(update={"fit": fit, "fit_score": total})


# --------------------------------------------------------------------------- market score


def _score_demand(demand: DemandCard | None, listings: list[Listing]) -> ScoreComponent:
    """base = 0.6 * min(mean_12m / 50, 1) + 0.4 * clamp(yoy + 0.5, 0, 1)   (growth 0.5 if yoy unknown)
    base += 0.1 (cap 1) if Amazon bought_last_month sums to >= 1000; points = 25 * base (x 0.8 for a proxy term)."""
    if demand is None or demand.mean_12m is None:
        term = f' for "{demand.term}"' if demand else ""
        return _component(0, 25, f"No Trends data{term}", demand.evidence_ids if demand else [])
    level = min(demand.mean_12m / 50, 1.0)
    growth = 0.5 if demand.yoy_change is None else min(max(demand.yoy_change + 0.5, 0.0), 1.0)
    base = 0.6 * level + 0.4 * growth
    parts = [f"Interest {demand.mean_12m:.0f}/100 over 12 months"]
    parts.append("no year-on-year data" if demand.yoy_change is None else f"{demand.yoy_change:+.0%} year on year")
    ev = list(demand.evidence_ids)
    bought = [lst for lst in listings if lst.bought_last_month]
    total_bought = sum(lst.bought_last_month or 0 for lst in bought)
    if total_bought >= BOUGHT_BONUS_AT:
        base = min(base + 0.1, 1.0)
        parts.append(f"{total_bought:,}+ bought last month on Amazon")
        ev += [lst.evidence_id for lst in bought]
    points = 25 * base
    if demand.is_proxy:
        points *= 0.8
        parts.append(f'proxy term "{demand.term}" (x0.8)')
    return _component(points, 25, "; ".join(parts), ev)


def _score_headroom(quote: QuoteRange | None, listings: list[Listing]) -> ScoreComponent:
    """points = 25 * clamp(margin_retailer / 0.5, 0, 1): 0 at <= 0% margin, 25 at >= 50%."""
    if quote is None:
        return _component(0, 25, "No price ladder, so no FOB quote")
    if quote.margin_retailer is None:
        return _component(0, 25, "Margin unknown: unit cost or exchange rate missing")
    ev = [lst.evidence_id for lst in listings if lst.price]
    detail = f"Margin {quote.margin_retailer:.0%} at {_money(quote.fob_retailer, quote.currency)} FOB"
    return _component(25 * quote.margin_retailer / 0.5, 25, detail, ev)


def _score_duty(quote: QuoteRange | None, market: dict[str, Any]) -> ScoreComponent:
    """7.5 if India's duty is 0, + 7.5 * min(1, max(0, competitor - india) / 0.04).
    India's duty is the quote's duty_pct assumption (user override) if present, else market duty_india."""
    duty_india = float(market.get("duty_india", 0.0))
    if quote and "duty_pct" in quote.assumptions:
        duty_india = quote.assumptions["duty_pct"]
    duty_comp = float(market.get("duty_competitor", 0.0))
    points = (7.5 if duty_india == 0 else 0.0) + 7.5 * min(1.0, max(0.0, duty_comp - duty_india) / 0.04)
    detail = f"{_pct(duty_india)} duty on Indian goods vs {_pct(duty_comp)} for competing origins"
    if market.get("last_verified"):
        detail += f" (config, verified {market['last_verified']})"
    return _component(points, 15, detail)


def _score_proven_india(origin: OriginShare | None) -> ScoreComponent:
    """share <= 0.3: 10 * max(0, 1 - |share - 0.3| / 0.3);  share > 0.3: 10 * max(0.4, 1 - (share - 0.3) / 0.7).
    share = Amazon made-in-India share, else eBay listings located in India / eBay listings."""
    if origin is None:
        return _component(0, 10, "No origin data")
    share = origin.india_share
    if share is not None:
        known = origin.india + origin.china + origin.other
        detail = f"{origin.india} of {known} Amazon products with a known origin are made in India"
    elif origin.ebay_total:
        share = origin.ebay_from_india / origin.ebay_total
        detail = f"{origin.ebay_from_india} of {origin.ebay_total} eBay listings ship from India"
    else:
        return _component(0, 10, "No origin data", origin.evidence_ids)
    if share <= INDIA_SWEET_SPOT:
        points = 10 * max(0.0, 1 - abs(share - INDIA_SWEET_SPOT) / INDIA_SWEET_SPOT)
    else:
        points = 10 * max(0.4, 1 - (share - INDIA_SWEET_SPOT) / (1 - INDIA_SWEET_SPOT))
    if share == 0:
        note = "no Indian products yet"
    elif share < INDIA_SWEET_SPOT / 2:
        note = "few Indian products yet"
    elif share <= INDIA_SWEET_SPOT * 1.5:
        note = "proven, not crowded"
    else:
        note = "proven but getting crowded"
    return _component(points, 10, f"{detail} ({share:.0%}): {note}", origin.evidence_ids)


def _score_buyer_depth(buyers: list[BuyerCandidate]) -> ScoreComponent:
    """points = 15 * min(1, buyers with fit >= 60 / 8)."""
    good = [b for b in buyers if b.fit_score is not None and b.fit_score >= GOOD_FIT]
    ev = [s.evidence_id for b in good for s in b.signals]
    detail = f"{len(good)} of {len(buyers)} buyers fit {GOOD_FIT:.0f}+"
    return _component(15 * min(1.0, len(good) / DEPTH_TARGET), 15, detail, ev)


def _score_timing(demand: DemandCard | None) -> ScoreComponent:
    """months to the next buying window: 0-2 -> 10; 3-6 -> 7; 7-9 -> 4; > 9 -> 2; unknown -> 0."""
    months = demand.months_to_window if demand else None
    if months is None:
        return _component(0, 10, "No seasonality data, so no buying window")
    points = 10 if months <= 2 else 7 if months <= 6 else 4 if months <= 9 else 2
    when = "now" if months == 0 else "next month" if months == 1 else f"in {months} months"
    return _component(points, 10, f"Next buying window {when}", demand.evidence_ids if demand else [])


def market_score(
    *,
    demand: DemandCard | None,
    quote: QuoteRange | None,
    origin: OriginShare | None,
    buyers: list[BuyerCandidate],
    listings: list[Listing],
    market: dict[str, Any],
) -> MarketScore:
    """Demand 25, price headroom 25, duty advantage 15, proven for Indian goods 10,
    buyer depth 15, timing 10. Missing inputs score 0 for that component, with a detail saying why."""
    components = {
        "demand": _score_demand(demand, listings),
        "price_headroom": _score_headroom(quote, listings),
        "duty_advantage": _score_duty(quote, market),
        "proven_india": _score_proven_india(origin),
        "buyer_depth": _score_buyer_depth(buyers),
        "timing": _score_timing(demand),
    }
    return MarketScore(total=round(sum(c.points for c in components.values()), 1), components=components)
