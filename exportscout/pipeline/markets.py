"""Market Compare: where to sell next. Deterministic; no I/O.

Each other market gets a quick scan (one Amazon search and its FX rate, plus one worldwide
Trends "interest by country" request shared by all markets); the home market reuses data
already fetched. Every market is worked back to a FOB range with its own VAT, markups,
freight and duty (prices.fob_quote), then ranked by a transparent Market Fit score (0-100).

    duty on Indian goods = duty_india
                         + normal duty: the mfn_by_hs entry whose key is a prefix of the HS hint
                           (longest first); if there is none, mfn_default; else 0
                         + sum of extra_duty rate x copper_share (entries whose hs_prefix is a prefix of the HS hint)

    margin        40 x clamp(margin_retailer / 0.5, 0, 1)            0 without a quote or margin
    demand        30: if any market has Trends interest > 0:
                        20 x trends / max trends + 10 x review depth / max review depth
                      else 30 x review depth / max review depth      (missing values count 0)
    trade_access  20 x max(0, 1 - duty / 0.25)
    data_depth    10 with >= 30 priced listings, 5 with >= 10, else 0

Review depth = reviews summed over the first 20 Amazon results that show a review count.
Demand is relative to the best market in the comparison, so it only ranks markets against each other.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from exportscout.models import Listing, MarketRow, MarketScore, ProductIdentity, RegionInterest, ScoreComponent
from exportscout.pipeline.prices import fob_quote, price_ladder

THIN_LISTINGS = 10  # fewer priced listings than this: the median is shaky ("thin data")
DEEP_LISTINGS = 30  # full data-depth points from this many priced listings
REVIEW_DEPTH_LISTINGS = 20  # review depth sums the reviews of this many Amazon results
FULL_MARGIN = 0.5  # margin at which the margin component is full
BLOCKING_DUTY = 0.25  # duty at which trade access scores 0

MARGIN_POINTS = 40.0
DEMAND_POINTS = 30.0
TRENDS_POINTS = 20.0  # share of DEMAND_POINTS for Trends, when any market has Trends data
TRADE_POINTS = 20.0
DEPTH_POINTS = 10.0

_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "AUD": "A$"}


def _pct(value: float) -> str:
    """0.157 -> "15.7%", 0.1 -> "10%", 0.0 -> "0%"."""
    return f"{value * 100:.1f}".rstrip("0").rstrip(".") + "%"


def _money(value: float, currency: str | None) -> str:
    """18.2 AUD -> "A$18.20"; 1020.4 INR -> "₹1,020"; codes without a symbol -> "AED 45.00"."""
    cur = (currency or "").upper()
    if cur == "INR":
        return f"₹{value:,.0f}"
    sym = _SYMBOLS.get(cur)
    return f"{sym}{value:,.2f}" if sym else f"{cur} {value:,.2f}".strip()


def _component(points: float, max_points: float, detail: str, evidence: Iterable[str] = ()) -> ScoreComponent:
    """Clamp points to [0, max_points] and round to 1 dp."""
    return ScoreComponent(
        points=round(min(max(points, 0.0), max_points), 1),
        max_points=max_points,
        detail=detail,
        evidence_ids=list(dict.fromkeys(evidence)),
    )


# --------------------------------------------------------------------------- inputs


def world_market(category: dict[str, Any]) -> dict[str, Any]:
    """Pseudo-market for the one worldwide Trends request (interest by country) shared by all rows."""
    return {"label": "Worldwide", "trends_geo": None, "trends_cat": category.get("trends_cat")}


def hs_hint(product: ProductIdentity, category: dict[str, Any]) -> str | None:
    """HS prefix for the duty lookup: the first ``hs_hints`` word (config order) found in the
    product type, else in the retail keywords (most specific first). None if nothing matches."""
    # Imported here: identify imports llm.tasks, which imports this module.
    from exportscout.pipeline.identify import tokens

    hints = [(" ".join(tokens(str(word))), str(hs)) for word, hs in (category.get("hs_hints") or {}).items()]
    for text in [product.product_type, *product.keywords]:
        padded = f" {' '.join(tokens(text))} "
        for word, hs in hints:
            if word and f" {word} " in padded:
                return hs
    return None


def effective_duty(mkt: dict[str, Any], hs: str | None) -> tuple[float, str]:
    """(duty on Indian goods as a fraction, how it was built in plain words).

    duty = duty_india + normal duty + sum(extra_duty rate x copper_share)
    normal duty = the mfn_by_hs entry whose key is a prefix of ``hs`` (longest key first); if
    ``hs`` is None or no key matches, mfn_default if set, else 0. An extra_duty entry applies
    when its hs_prefix is a prefix of ``hs``.
    """
    base = float(mkt.get("duty_india") or 0.0)
    parts = [f"{_pct(base)} base duty on Indian goods"] if base else []

    table = {str(k): float(v) for k, v in (mkt.get("mfn_by_hs") or {}).items()}
    key = next((k for k in sorted(table, key=len, reverse=True) if hs and hs.startswith(k)), None)
    if key is not None:
        normal = table[key]
        parts.append(f"{_pct(normal)} normal duty (HS {hs})")
    else:
        normal = float(mkt.get("mfn_default") or 0.0)
        if normal:
            parts.append(f"{_pct(normal)} normal duty (assumed" + (f" for HS {hs})" if hs else "; HS line unknown)"))

    extra = 0.0
    for item in mkt.get("extra_duty") or []:
        if not hs or not hs.startswith(str(item["hs_prefix"])):
            continue
        share = float(item.get("copper_share", 1.0))
        rate = float(item["rate"]) * share
        extra += rate
        part = f"{_pct(rate)} {item.get('label') or 'extra duty'}"
        if "copper_share" in item:
            part += f" (assumes {_pct(share)} copper)"
        parts.append(part)

    duty = round(base + normal + extra, 4)
    return duty, (" + ".join(parts) if parts else f"{_pct(duty)} duty on Indian goods")


def translate_keyword(keyword: str, language: str, category: dict[str, Any]) -> str:
    """Word-map translation of a search phrase (the fallback when the LLM is unavailable).

    Phrases from ``category["translations"][language]`` are replaced longest first, as whole
    words, ignoring case, in one pass (a translation is never translated again); unknown words
    are kept. Returns ``keyword`` unchanged if there is no word map for ``language``.
    """
    table = {str(k).lower(): str(v) for k, v in ((category.get("translations") or {}).get(language) or {}).items()}
    if not table:
        return keyword
    pattern = r"\b(?:" + "|".join(re.escape(p) for p in sorted(table, key=len, reverse=True)) + r")\b"
    text = re.sub(r"\s+", " ", keyword).strip()
    out = re.sub(pattern, lambda m: table.get(m.group(0).lower(), m.group(0)), text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", out).strip()


# --------------------------------------------------------------------------- rows


def build_row(
    code: str,
    mkt: dict[str, Any],
    listings: list[Listing],
    *,
    keyword: str,
    fx: float | None,
    unit_cost_inr: float | None,
    extra_costs_inr: float = 0.0,
    hs: str | None = None,
    trends: RegionInterest | None = None,
    is_home: bool = False,
    overrides: dict[str, float] | None = None,
    evidence_ids: Iterable[str] = (),
) -> MarketRow:
    """One unscored Market Compare row (rank_markets adds the score).

    ladder = price_ladder(listings, market currency); duty = effective_duty(mkt, hs), unless
    ``overrides`` sets a different duty_pct (the user's own figure wins on the home row);
    quote = fob_quote(ladder, mkt, overrides={**overrides, "duty_pct": duty}).
    review_depth = reviews summed over the first 20 Amazon listings (in the given order) that
    show a review count; bought_last_month = Amazon "bought in past month" figures summed.
    thin_data = no ladder or fewer than 10 priced listings.
    """
    ladder = price_ladder(listings, mkt["currency"])
    user = {k: float(v) for k, v in (overrides or {}).items() if v is not None}
    duty, detail = effective_duty(mkt, hs)
    if "duty_pct" in user and abs(user["duty_pct"] - duty) > 1e-9:
        duty = user["duty_pct"]
        detail = f"{_pct(duty)} duty on Indian goods (your setting)"
    quote = None
    if ladder is not None:
        quote = fob_quote(
            ladder,
            mkt,
            fx_rate=fx,
            unit_cost_inr=unit_cost_inr,
            extra_costs_inr=extra_costs_inr,
            overrides={**user, "duty_pct": duty},
        )

    amazon = [lst for lst in listings if lst.channel == "amazon"]
    reviews = [lst.reviews for lst in amazon if lst.reviews][:REVIEW_DEPTH_LISTINGS]
    bought = [lst.bought_last_month for lst in amazon if lst.bought_last_month is not None]
    ids = [*(ladder.evidence_ids if ladder else []), *evidence_ids, *([trends.evidence_id] if trends else [])]
    verified = mkt.get("last_verified")
    return MarketRow(
        code=code,
        label=mkt["label"],
        short_label=mkt.get("short_label"),
        currency=mkt["currency"],
        keyword=keyword,
        is_home=is_home,
        listings_n=ladder.n if ladder else 0,
        ladder=ladder,
        quote=quote,
        fx_rate=fx if fx and fx > 0 else None,
        hs_code=hs,
        duty_pct=duty,
        duty_detail=detail,
        duty_note=mkt.get("duty_note"),
        duty_sources=list(mkt.get("duty_sources") or []),
        last_verified=str(verified) if verified is not None else None,
        trends_interest=trends.value if trends else None,
        review_depth=sum(reviews) if reviews else None,
        bought_last_month=sum(bought) if bought else None,
        thin_data=ladder is None or ladder.n < THIN_LISTINGS,
        evidence_ids=list(dict.fromkeys(ids)),
    )


# --------------------------------------------------------------------------- Market Fit


def _margin(row: MarketRow) -> float | None:
    return row.quote.margin_retailer if row.quote is not None else None


def _score_margin(row: MarketRow) -> ScoreComponent:
    """40 x clamp(margin_retailer / 0.5, 0, 1): 0 at <= 0% margin, 40 at >= 50%; 0 without a margin."""
    q = row.quote
    ev = row.ladder.evidence_ids if row.ladder else []
    if q is None or q.margin_retailer is None:
        if q is not None and q.fx_rate and q.unit_cost_inr is None:
            return _component(0, MARGIN_POINTS, "No margin: add your unit cost", ev)
        return _component(0, MARGIN_POINTS, "No quote: too few prices or no exchange rate", ev)
    detail = f"Margin {q.margin_retailer:.0%} at {_money(q.fob_retailer, q.currency)} FOB"
    if q.fob_retailer_inr is not None:
        detail += f" ({_money(q.fob_retailer_inr, 'INR')})"
    return _component(MARGIN_POINTS * q.margin_retailer / FULL_MARGIN, MARGIN_POINTS, detail, ev)


def _score_demand(row: MarketRow, max_trends: int, max_reviews: int, term: str | None) -> ScoreComponent:
    """With Trends for any market: 20 x trends / max trends + 10 x reviews / max reviews;
    else 30 x reviews / max reviews. Missing values count 0."""
    reviews_share = (row.review_depth or 0) / max_reviews if max_reviews else 0.0
    if row.review_depth:
        reviews_text = f"{row.review_depth:,} reviews on the top Amazon results"
    else:
        reviews_text = "no review counts on the top Amazon results"
    ev = [i for i in row.evidence_ids if i.startswith("google_trends:")]
    if max_trends > 0:
        points = TRENDS_POINTS * (row.trends_interest or 0) / max_trends + (DEMAND_POINTS - TRENDS_POINTS) * reviews_share
        where = f" for “{term}”" if term else ""
        trends_text = f"Trends interest {row.trends_interest}/100{where}" if row.trends_interest else f"No Trends interest recorded{where}"
        return _component(points, DEMAND_POINTS, f"{trends_text}; {reviews_text}", ev)
    return _component(DEMAND_POINTS * reviews_share, DEMAND_POINTS, f"No Trends data by country; {reviews_text}", ev)


def _score_trade(row: MarketRow) -> ScoreComponent:
    """20 x max(0, 1 - duty / 0.25): 20 at 0% duty, 0 at 25% or more."""
    detail = row.duty_detail or f"{_pct(row.duty_pct)} duty on Indian goods"
    return _component(TRADE_POINTS * max(0.0, 1 - row.duty_pct / BLOCKING_DUTY), TRADE_POINTS, detail)


def _score_depth(row: MarketRow) -> ScoreComponent:
    """10 with >= 30 priced listings, 5 with >= 10, else 0."""
    n = row.listings_n
    points = DEPTH_POINTS if n >= DEEP_LISTINGS else DEPTH_POINTS / 2 if n >= THIN_LISTINGS else 0.0
    channels = set(row.ladder.by_channel) if row.ladder else set()
    what = "Amazon listings" if channels <= {"amazon"} else "listings"
    return _component(points, DEPTH_POINTS, f"{n} priced {what}" + (" — thin data" if row.thin_data else ""))


def rank_markets(rows: list[MarketRow], *, trends_term: str | None = None) -> list[MarketRow]:
    """Copies of ``rows`` with the Market Fit score (margin 40, demand 30, trade access 20,
    data depth 10; total = sum), best first (ties: higher margin first).
    ``trends_term`` (the Trends search term) only labels the demand detail."""
    max_trends = max((r.trends_interest or 0 for r in rows), default=0)
    max_reviews = max((r.review_depth or 0 for r in rows), default=0)
    scored = []
    for row in rows:
        components = {
            "margin": _score_margin(row),
            "demand": _score_demand(row, max_trends, max_reviews, trends_term),
            "trade_access": _score_trade(row),
            "data_depth": _score_depth(row),
        }
        total = round(sum(c.points for c in components.values()), 1)
        scored.append(row.model_copy(update={"score": MarketScore(total=total, components=components)}))

    def key(row: MarketRow) -> tuple[float, float]:
        margin = _margin(row)
        return row.score.total if row.score else 0.0, margin if margin is not None else float("-inf")

    return sorted(scored, key=key, reverse=True)


def best_alternative(rows: list[MarketRow]) -> MarketRow | None:
    """The best-ranked market other than the home market that has a quote with a margin.
    ``rows`` must already be ranked (rank_markets)."""
    return next((r for r in rows if not r.is_home and _margin(r) is not None), None)
