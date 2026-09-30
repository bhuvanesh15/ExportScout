"""The ExportScout agent: plan §5 steps 1-8 with follow-up rules and a credit budget.

The app calls ``make_clients`` and ``run_scout``. Searches inside a step run in parallel
threads; step-log events are always emitted from the calling thread (Streamlit needs that).
Any step can fail or hit the budget without killing the run: the brief is built from
whatever was found, with a warning.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

import yaml

from exportscout.config import category, market
from exportscout.llm import tasks
from exportscout.llm.client import LLM
from exportscout.models import (
    Brief,
    BuyerCandidate,
    EvidenceStore,
    Listing,
    ProductDetail,
    ProductIdentity,
    RunInputs,
    StepEvent,
)
from exportscout.pipeline import buyers as buyer_pipeline
from exportscout.pipeline import demand as demand_pipeline
from exportscout.pipeline import identify, origin, prices, reviews, scoring
from exportscout.serp import engines
from exportscout.serp.client import BudgetExceeded, CacheMiss, SerpApiError, SerpClient, env_api_key

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "demo_cache"
CACHE_DIR = REPO_ROOT / ".cache"
DEFAULT_BUDGET = 35

MIN_PRICED_LOOKALIKES = 5
STEADY_SHARE = 0.6  # share of weeks with non-zero Trends interest
ASINS_TO_CHECK = 6
EXTRA_ASINS_IF_NO_ORIGIN = 2
DISCOVERY_QUERIES = 3
MAPS_SWEEPS = 2
MAX_ENRICH = 6
CREDITS_PER_ENRICH = 2
MAX_BUYERS = 15
MAX_TAGGED = 25
NOT_BUYERS = ("marketplace", "not_a_buyer")
MAX_PITCHES = 5

T = TypeVar("T")


def load_demo_products() -> list[dict[str, Any]]:
    """Demo products from demo_cache/demo_products.yaml: [{id, label, inputs: RunInputs}]."""
    path = DEMO_DIR / "demo_products.yaml"
    if not path.exists():
        return []
    items = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    out = []
    for item in items:
        inputs = dict(item["inputs"])
        if inputs.get("image_path"):
            inputs["image_path"] = str(DEMO_DIR / inputs["image_path"])
        out.append({"id": item["id"], "label": item["label"], "inputs": RunInputs(**inputs)})
    return out


def make_clients(
    *,
    demo_mode: bool,
    record: bool = False,
    budget: int | None = DEFAULT_BUDGET,
    listener: Callable[..., None] | None = None,
) -> tuple[SerpClient, LLM]:
    """SerpApi + LLM clients for one run. Demo Mode replays demo_cache/ and needs no keys."""
    if demo_mode:
        mode = "replay"
    elif record:
        mode = "record"
    else:
        mode = "live"
    serp = SerpClient(
        mode=mode,
        cache_path=CACHE_DIR / "serp.sqlite",
        fixture_dir=DEMO_DIR / "serp",
        budget=budget,
        listener=listener,
        prefer_fixtures=True,  # never pay twice for a search that is already recorded
    )
    llm = LLM(mode=mode, record_dir=DEMO_DIR / "llm", cache_path=CACHE_DIR / "llm.sqlite")
    return serp, llm


def keys_available() -> dict[str, bool]:
    return {"SERPAPI_API_KEY": bool(env_api_key()), "ANTHROPIC_API_KEY": bool(os.environ.get("ANTHROPIC_API_KEY"))}


# --------------------------------------------------------------------------- run state


class _Run:
    """Step log, warnings and budget state for one run."""

    def __init__(self, serp: SerpClient, on_event: Callable[[StepEvent], None] | None):
        self.serp = serp
        self.on_event = on_event
        self.events: list[StepEvent] = []
        self.warnings: list[str] = []
        self.out_of_budget = False
        self.searched = False  # at least one search succeeded

    def log(self, step: int, name: str, message: str, status: str = "done") -> None:
        event = StepEvent(step=step, name=name, message=message, status=status, credits_used=self.serp.credits_used)
        self.events.append(event)
        if self.on_event is not None:
            self.on_event(event)

    def warn(self, step: int, name: str, message: str) -> None:
        self.warnings.append(message)
        self.log(step, name, message, status="warning")

    def remaining(self) -> int | None:
        if self.serp.budget is None or self.serp.mode == "replay":
            return None
        return max(0, self.serp.budget - self.serp.credits_used)

    def can_spend(self, credits: int = 1) -> bool:
        remaining = self.remaining()
        return not self.out_of_budget and (remaining is None or remaining >= credits)

    def handle(self, exc: BaseException, step: int, name: str, what: str) -> None:
        """Turn a search failure into a warning; re-raise only if nothing has worked yet."""
        if isinstance(exc, BudgetExceeded):
            if not self.out_of_budget:
                self.out_of_budget = True
                self.warn(step, name, "Credit budget reached; the brief uses what was found so far.")
            return
        if isinstance(exc, (CacheMiss, SerpApiError)) and not self.searched:
            raise exc
        self.warn(step, name, f"{what} failed: {exc}")

    def call(self, step: int, name: str, what: str, fn: Callable[[], T], default: T) -> T:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - a failed search must not end the run
            self.handle(exc, step, name, what)
            return default
        self.searched = True
        return result

    def parallel(self, step: int, name: str, jobs: dict[str, Callable[[], Any]], default: Any) -> dict[str, Any]:
        """Run searches in threads; failures become warnings and ``default``."""
        if not jobs:
            return {}
        with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as pool:
            futures = {key: pool.submit(fn) for key, fn in jobs.items()}
            out: dict[str, Any] = {}
            for key, future in futures.items():
                try:
                    out[key] = future.result()
                    self.searched = True
                except Exception as exc:  # noqa: BLE001
                    self.handle(exc, step, name, key)
                    out[key] = default
        return out


# --------------------------------------------------------------------------- formatting


def _money(value: float | None, currency: str) -> str:
    if value is None:
        return "n/a"
    symbol = {"GBP": "£", "USD": "$", "EUR": "€", "INR": "₹"}.get(currency, currency + " ")
    return f"{symbol}{value:,.0f}" if currency == "INR" else f"{symbol}{value:,.2f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


# --------------------------------------------------------------------------- the agent


def run_scout(
    inputs: RunInputs,
    *,
    serp: SerpClient,
    llm: LLM | None,
    on_event: Callable[[StepEvent], None] | None = None,
) -> Brief:
    """Run the full pipeline and return the Brief. ``on_event`` is called from the calling
    thread for every step-log line (safe for Streamlit)."""
    if not (inputs.image_path or inputs.image_url or inputs.description):
        raise ValueError("Upload a product photo or describe the product.")
    cat = category(inputs.category)
    mkt = {**market(inputs.market), "trends_cat": cat.get("trends_cat")}
    cur = mkt["currency"]
    ev = EvidenceStore()
    run = _Run(serp, on_event)

    product, lens = _identify(run, serp, llm, ev, mkt, cat, inputs)
    demand = _demand(run, serp, ev, mkt, cat, product)
    listings, amazon, ebay, fx = _prices(run, serp, ev, mkt, product, lens)

    ladder = prices.price_ladder(listings, cur)
    quote = None
    if ladder is None:
        run.warn(3, "Prices", f"Fewer than 3 {cur} prices found; no quote range.")
    else:
        quote = prices.fob_quote(
            ladder,
            mkt,
            fx_rate=fx,
            unit_cost_inr=inputs.unit_cost_inr,
            extra_costs_inr=inputs.extra_costs_inr,
            overrides=inputs.assumption_overrides or None,
        )
        by_channel = ", ".join(f"{ch} {s.n}" for ch, s in ladder.by_channel.items())
        msg = (
            f"Median {_money(ladder.p50, cur)} across {ladder.n} listings ({by_channel}) → quote "
            f"{_money(quote.fob_importer, cur)}–{_money(quote.fob_retailer, cur)}"
        )
        if quote.fob_retailer_inr is not None:
            msg += f" (≈{_money(quote.fob_importer_inr, 'INR')}–{_money(quote.fob_retailer_inr, 'INR')})"
        if quote.margin_retailer is not None:
            msg += f"; margin {_pct(quote.margin_retailer)} → {quote.verdict.replace('_', '-').upper()}"
        run.log(3, "Prices", msg)

    products = _origin(run, serp, ev, mkt, amazon, ebay)
    origin_share = origin.origin_shares(products, ebay)

    snippets = [r for p in products for r in p.reviews]
    themes = reviews.review_themes(llm, snippets, cat) if snippets else []
    complaints = [t for t in themes if t.kind == "complaint"]
    if complaints:
        top = ", ".join(f"{t.label.lower()} ({_pct(t.share)})" for t in complaints[:3])
        run.log(5, "Reviews", f"{len(snippets)} reviews read. Top complaints: {top}")
    else:
        run.log(5, "Reviews", f"{len(snippets)} reviews read; no clear complaint themes.", status="skipped" if not snippets else "done")

    candidates = _discover(run, serp, ev, mkt, cat, product, listings, products, demand)
    # Rank on discovery data, then let the LLM weed out marketplaces and non-buyers
    # before any enrichment credits are spent on them.
    shortlist = tasks.tag_buyers(llm, _rank(candidates, product, ladder, quote)[:MAX_TAGGED], product)
    shortlist = _drop_non_buyers(run, shortlist)
    price_below_cost = quote is not None and quote.verdict == "no_go"
    if price_below_cost:
        run.warn(
            7,
            "Enrich",
            "The FOB ceiling is below your cost, so buyer enrichment was skipped to save credits. "
            "Consider premium positioning (handmade / antique finish at the upper price band) or another market.",
        )
    else:
        shortlist = _merge_by_domain(_enrich(run, serp, ev, mkt, product, shortlist))

    ranked = _rank(shortlist, product, ladder, quote)[:MAX_BUYERS]
    if any(c.enriched for c in ranked):
        # Re-tag so each "why" line can use what enrichment found.
        ranked = _drop_non_buyers(run, tasks.tag_buyers(llm, ranked, product))

    score = scoring.market_score(
        demand=demand, quote=quote, origin=origin_share, buyers=ranked, listings=listings, market=mkt
    )
    verdict = (quote.verdict if quote else "unknown").replace("_", "-").upper()
    brief = Brief(
        inputs=inputs,
        market=inputs.market,
        market_label=mkt["label"],
        product=product,
        listings=listings,
        ladder=ladder,
        quote=quote,
        demand=demand,
        origin=origin_share,
        themes=themes,
        buyers=ranked,
        market_score=score,
        headline=f"{product.product_type[:1].upper()}{product.product_type[1:]} → {mkt['label']}: {verdict} ({score.total:.0f}/100)",
        evidence=ev.all(),
        warnings=run.warnings,
        credits_used=serp.credits_used,
    )

    text = tasks.write_brief(llm, brief)
    pitches = tasks.write_pitches(llm, brief, ranked[:MAX_PITCHES])
    good = sum(1 for b in ranked if (b.fit_score or 0) >= scoring.GOOD_FIT)
    run.log(8, "Write", f"Brief ready · {len(ranked)} buyers ranked ({good} strong fits) · {serp.credits_used} credits used")
    return brief.model_copy(
        update={
            "summary_md": text.summary_md,
            "spec_improvements": text.spec_improvements,
            "pitches": pitches,
            "evidence": ev.all(),
            "steps": run.events,
            "warnings": run.warnings,
            "credits_used": serp.credits_used,
        }
    )


# --------------------------------------------------------------------------- steps


def _identify(
    run: _Run, serp: SerpClient, llm: LLM | None, ev: EvidenceStore, mkt: dict, cat: dict, inputs: RunInputs
) -> tuple[ProductIdentity, list[Listing]]:
    """Step 1: Lens look-alikes -> product type and UK retail keywords."""
    lens: list[Listing] = []
    has_image = bool(inputs.image_path or inputs.image_url)
    if has_image:
        run.log(1, "Identify", "Searching Google Lens for UK look-alikes…", status="running")
        image = dict(image=inputs.image_path) if inputs.image_path else dict(url=inputs.image_url)
        lens = run.call(1, "Identify", "Google Lens", lambda: engines.lens_matches(serp, ev, mkt, **image), [])

    product = identify.identify_product(llm, lens, hint=inputs.description, material=inputs.material, category=cat)

    priced = _priced(lens, mkt["currency"])
    if has_image and len(priced) < MIN_PRICED_LOOKALIKES and run.can_spend():
        # Follow-up rule: few priced look-alikes -> retry Lens focused on the keyword.
        run.log(1, "Identify", f"Only {len(priced)} priced look-alikes; retrying Lens with “{product.keywords[0]}”.", status="running")
        retry = run.call(
            1,
            "Identify",
            "Google Lens retry",
            lambda: engines.lens_matches(serp, ev, mkt, **image, q=product.keywords[0], type="visual_matches"),
            [],
        )
        lens = _dedupe_listings(lens + retry)
        priced = _priced(lens, mkt["currency"])

    kws = ", ".join(f"“{k}”" for k in product.keywords[:3])
    if lens:
        low = min((l.price for l in priced), default=None)
        high = max((l.price for l in priced), default=None)
        span = f" ({_money(low, mkt['currency'])}–{_money(high, mkt['currency'])})" if priced else ""
        run.log(1, "Identify", f"Found {len(lens)} UK look-alikes, {len(priced)} priced{span}. Keywords: {kws}")
    else:
        run.log(1, "Identify", f"No photo matches; using text search. Keywords: {kws}")
    return product, lens


def _demand(run: _Run, serp: SerpClient, ev: EvidenceStore, mkt: dict, cat: dict, product: ProductIdentity):
    """Step 2: Trends seasonality for the most specific term with steady volume, related
    queries, and Autocomplete phrasing."""
    if not run.can_spend():
        run.log(2, "Demand", "Skipped (budget).", status="skipped")
        return None
    specific = product.keywords[0]
    terms, generic = _trend_terms(product, cat)
    run.log(2, "Demand", f"Comparing UK Google Trends interest for {', '.join(f'“{t}”' for t in terms)}…", status="running")
    series = run.call(2, "Demand", "Google Trends", lambda: engines.trends_timeseries(serp, ev, mkt, terms), None)
    term = _pick_term(series, terms)
    if term is None and generic and run.can_spend():
        # Follow-up rule: every product phrase is too thin -> fall back to the generic word.
        run.log(2, "Demand", f"No product phrase has steady UK volume; trying “{generic}”.", status="running")
        series = run.call(2, "Demand", "Google Trends", lambda: engines.trends_timeseries(serp, ev, mkt, [generic]), None)
        term = generic if series is not None else None
    if term is None:
        term = specific
    is_proxy = term != specific
    if is_proxy:
        run.log(2, "Demand", f"“{specific}” has too little UK search volume; using “{term}” as a proxy.", status="running")

    jobs: dict[str, Callable[[], Any]] = {}
    if run.can_spend(2):
        jobs = {
            "Trends related": lambda: engines.trends_related(serp, ev, mkt, term),
            "Autocomplete": lambda: engines.autocomplete(serp, ev, mkt, specific),
        }
    got = run.parallel(2, "Demand", jobs, [])
    # Timing is "as of the data", so Demo Mode replays give the same window on any date.
    fetched = ev.get(series.evidence_id) if series is not None else None
    as_of = datetime.fromisoformat(fetched.fetched_at).date() if fetched else None
    card = demand_pipeline.demand_card(
        term,
        series,
        [],
        got.get("Trends related", []),
        got.get("Autocomplete", []),
        mkt,
        today=as_of,
        is_proxy=is_proxy,
    )
    parts = []
    if card.peak_months:
        parts.append("peaks " + ", ".join(_month(m) for m in card.peak_months))
    if card.yoy_change is not None:
        parts.append(f"{card.yoy_change * 100:+.0f}% year on year")
    if card.months_to_window is not None:
        parts.append("buying window " + ("now" if card.months_to_window == 0 else f"in {card.months_to_window} months"))
    run.log(2, "Demand", (f"“{term}” interest " + "; ".join(parts)) if parts else "Not enough Trends data for this term.")
    return card


def _trend_terms(product: ProductIdentity, cat: dict) -> tuple[list[str], str | None]:
    """Up to 5 phrases to compare in one Trends request, most specific first, plus the generic
    one-word term held back for a fallback. A one-word term like "lantern" is left out of the
    comparison: Trends scales every term to the biggest one, which would flatten niche phrases to 0."""
    head = identify.tokens(product.product_type)
    broad: list[str] = []
    for key, phrases in cat.get("broad_terms", {}).items():
        if key in head:
            broad = phrases
            break
    terms = list(dict.fromkeys(t.strip().lower() for t in [*product.keywords[:3], product.broad_term, *broad] if t))
    multi = [t for t in terms if len(t.split()) > 1]
    single = [t for t in terms if len(t.split()) == 1]
    if len(multi) >= 2:
        return multi[:5], (single[0] if single else None)
    return terms[:5], None


def _pick_term(series, terms: list[str]) -> str | None:
    """Most specific term with steady volume: non-zero in most weeks of the last 2 years."""
    if series is None:
        return None
    for term in terms:
        recent = series.values.get(term, [])[-104:]
        steady = recent and sum(1 for v in recent if v > 0) / len(recent) >= STEADY_SHARE
        if steady and not demand_pipeline.is_low_volume(series, term):
            return term
    return None


def _prices(
    run: _Run, serp: SerpClient, ev: EvidenceStore, mkt: dict, product: ProductIdentity, lens: list[Listing]
) -> tuple[list[Listing], list[Listing], list[Listing], float | None]:
    """Step 3: retail price ladder from Shopping, Amazon, eBay (+ priced Lens matches) and FX."""
    kws = product.keywords[:2]
    # In priority order, so a tight budget drops the second keyword first.
    jobs: dict[str, Callable[[], Any]] = {
        "Shopping 0": lambda: engines.shopping_listings(serp, ev, mkt, kws[0]),
        "Amazon 0": lambda: engines.amazon_listings(serp, ev, mkt, kws[0]),
        "eBay": lambda: engines.ebay_listings(serp, ev, mkt, kws[0]),
    }
    if len(kws) > 1:
        jobs["Shopping 1"] = lambda: engines.shopping_listings(serp, ev, mkt, kws[1])
        jobs["Amazon 1"] = lambda: engines.amazon_listings(serp, ev, mkt, kws[1])
    remaining = run.remaining()
    if remaining is not None and remaining < len(jobs) + 1:
        jobs = dict(list(jobs.items())[: max(0, remaining - 1)])
    run.log(3, "Prices", f"Checking UK prices on Google Shopping, Amazon UK and eBay UK for {', '.join(f'“{k}”' for k in kws)}…", status="running")
    got = run.parallel(3, "Prices", jobs, [])
    fx = None
    if run.can_spend():
        fx = run.call(3, "Prices", "Google Finance", lambda: engines.fx_rate(serp, ev, mkt["fx_pair"]), None)
    if fx is None:
        run.warn(3, "Prices", f"No live {mkt['fx_pair']} rate; ₹ figures unavailable.")

    amazon = _dedupe_listings([l for k, v in got.items() if k.startswith("Amazon") for l in v])
    ebay = _dedupe_listings(got.get("eBay", []))
    shopping = _dedupe_listings([l for k, v in got.items() if k.startswith("Shopping") for l in v])
    listings = _dedupe_listings(_priced(lens, mkt["currency"]) + shopping + amazon + ebay)
    return listings, amazon, ebay, fx


def _origin(
    run: _Run, serp: SerpClient, ev: EvidenceStore, mkt: dict, amazon: list[Listing], ebay: list[Listing]
) -> list[ProductDetail]:
    """Step 4: country of origin on the top Amazon products (+ eBay seller location)."""
    asins = [l.asin for l in sorted(amazon, key=lambda l: l.reviews or 0, reverse=True) if l.asin and not l.sponsored]
    asins = list(dict.fromkeys(asins))
    first = asins[:ASINS_TO_CHECK]
    budget_left = run.remaining()
    if budget_left is not None:
        first = first[: max(0, budget_left - 12)]  # keep credits for buyer discovery + enrichment
    if not first:
        run.log(4, "Origin", "No Amazon products to check for origin.", status="skipped")
        return []
    run.log(4, "Origin", f"Opening {len(first)} top Amazon UK product pages for origin and reviews…", status="running")
    got = run.parallel(4, "Origin", {a: (lambda a=a: engines.amazon_product(serp, ev, mkt, a)) for a in first}, None)
    products = [p for p in got.values() if p is not None]

    if products and all(origin.classify_origin(p) == "unknown" for p in products):
        extra = asins[len(first) : len(first) + EXTRA_ASINS_IF_NO_ORIGIN]
        if extra and run.can_spend(len(extra) + 12):
            # Follow-up rule: no origin on the top products -> check a few more.
            run.log(4, "Origin", f"No origin shown on the top {len(products)}; checking {len(extra)} more.", status="running")
            more = run.parallel(4, "Origin", {a: (lambda a=a: engines.amazon_product(serp, ev, mkt, a)) for a in extra}, None)
            products += [p for p in more.values() if p is not None]

    counts: dict[str, int] = {}
    for p in products:
        counts[origin.classify_origin(p)] = counts.get(origin.classify_origin(p), 0) + 1
    india_ebay = sum(1 for l in ebay if l.location and "india" in l.location.lower())
    parts = [f"{counts.get(k, 0)} {label}" for k, label in (("india", "made in India"), ("china", "China"), ("other", "other"), ("unknown", "unknown"))]
    msg = f"Of {len(products)} Amazon products checked: " + ", ".join(parts)
    if ebay:
        msg += f". eBay: {india_ebay} of {len(ebay)} listings ship from India"
    run.log(4, "Origin", msg)
    return products


def _discover(
    run: _Run,
    serp: SerpClient,
    ev: EvidenceStore,
    mkt: dict,
    cat: dict,
    product: ProductIdentity,
    listings: list[Listing],
    products: list[ProductDetail],
    demand,
) -> list[BuyerCandidate]:
    """Step 6: buyer candidates from merchants/brands already seen + Google + Maps sweeps."""
    queries = buyer_pipeline.discovery_queries(product, mkt, cat)[:DISCOVERY_QUERIES]
    cities = _maps_cities(mkt, demand)
    jobs: dict[str, Callable[[], Any]] = {}
    for i, q in enumerate(queries):
        jobs[f"web {i}"] = lambda q=q: engines.web_results(serp, ev, mkt, q)
    maps_q = cat["maps_queries"][0]
    for city in cities:
        jobs[f"maps {city['name']}"] = lambda city=city: engines.maps_places(
            serp, ev, mkt, maps_q, ll=city["ll"], city=city["name"]
        )
    remaining = run.remaining()
    if remaining is not None:
        # Keep credits for enrichment of at least two buyers.
        jobs = dict(list(jobs.items())[: max(0, remaining - 2 * CREDITS_PER_ENRICH)])
    where = ", ".join(c["name"] for c in cities)
    run.log(6, "Buyers", f"Finding UK buyers: {len(queries)} Google searches + Maps sweeps in {where}…", status="running")
    got = run.parallel(6, "Buyers", jobs, [])
    web = [r for k, v in got.items() if k.startswith("web") for r in v]
    places = [p for k, v in got.items() if k.startswith("maps") for p in v]
    candidates = buyer_pipeline.discover_candidates(
        listings=listings,
        web=web,
        places=places,
        category=cat,
        products=products,
        product=product,
        currency=mkt["currency"],
    )
    multi = sum(1 for c in candidates if len(c.sources) >= 2)
    run.log(6, "Buyers", f"{len(candidates)} candidate buyers after dedupe ({multi} seen in 2+ engines)")
    return candidates


def _enrich(
    run: _Run, serp: SerpClient, ev: EvidenceStore, mkt: dict, product: ProductIdentity, candidates: list[BuyerCandidate]
) -> list[BuyerCandidate]:
    """Step 7: spend the remaining budget on the most promising buyers."""
    remaining = run.remaining()
    k = MAX_ENRICH if remaining is None else max(0, min(MAX_ENRICH, (remaining - 1) // CREDITS_PER_ENRICH))
    picked = buyer_pipeline.rank_for_enrichment(candidates, k) if k else []
    if not picked:
        run.log(7, "Enrich", "No budget or candidates left for enrichment.", status="skipped")
        return candidates
    with_news = remaining is None or remaining >= k * CREDITS_PER_ENRICH + 1
    run.log(7, "Enrich", f"Checking {len(picked)} buyers: websites, UK ads, news…", status="running")
    jobs = {
        c.name: (lambda c=c, i=i: buyer_pipeline.enrich(serp, ev, mkt, c, product, with_news=with_news and i == 0))
        for i, c in enumerate(picked)
    }
    got = run.parallel(7, "Enrich", jobs, None)
    enriched = {name: c for name, c in got.items() if c is not None}
    out = [enriched.get(c.name, c) for c in candidates]
    highlights = []
    for c in enriched.values():
        ads = next((s.detail for s in c.signals if s.kind == "ads_active"), None)
        if ads:
            highlights.append(f"{c.name}: {ads}")
    msg = f"Enriched {len(enriched)} buyers"
    if highlights:
        msg += " · " + " · ".join(highlights[:2])
    run.log(7, "Enrich", msg)
    return out


# --------------------------------------------------------------------------- helpers


def _rank(
    candidates: list[BuyerCandidate], product: ProductIdentity, ladder, quote
) -> list[BuyerCandidate]:
    scored = [scoring.buyer_fit(c, product, ladder=ladder, quote=quote) for c in candidates]
    return sorted(scored, key=lambda c: c.fit_score or 0, reverse=True)


def _drop_non_buyers(run: _Run, candidates: list[BuyerCandidate]) -> list[BuyerCandidate]:
    dropped = [c for c in candidates if c.kind in NOT_BUYERS]
    if dropped:
        names = ", ".join(c.name for c in dropped[:4]) + (" …" if len(dropped) > 4 else "")
        run.log(6, "Buyers", f"Set aside {len(dropped)} marketplaces / non-buyers: {names}")
    return [c for c in candidates if c.kind not in NOT_BUYERS]


def _merge_by_domain(candidates: list[BuyerCandidate]) -> list[BuyerCandidate]:
    """Enrichment can reveal that a Shopping merchant and a web result are the same business."""
    by_domain: dict[str, int] = {}
    out: list[BuyerCandidate] = []
    for c in candidates:
        i = by_domain.get(c.domain) if c.domain else None
        if i is None:
            if c.domain:
                by_domain[c.domain] = len(out)
            out.append(c)
            continue
        a = out[i]
        seen = {(s.kind, s.evidence_id) for s in a.signals}
        out[i] = a.model_copy(
            update={
                "sources": list(dict.fromkeys(a.sources + c.sources)),
                "signals": a.signals + [s for s in c.signals if (s.kind, s.evidence_id) not in seen],
                "seen_texts": list(dict.fromkeys(a.seen_texts + c.seen_texts)),
                "listing_prices": a.listing_prices + c.listing_prices,
                "phone": a.phone or c.phone,
                "city": a.city or c.city,
                "website": a.website or c.website,
                "enriched": a.enriched or c.enriched,
                "ad_creatives": a.ad_creatives if a.ad_creatives is not None else c.ad_creatives,
            }
        )
    return out


def _priced(listings: list[Listing], currency: str) -> list[Listing]:
    return [l for l in listings if l.price and l.price > 0 and l.currency == currency]


def _dedupe_listings(listings: list[Listing]) -> list[Listing]:
    seen: set[str] = set()
    out = []
    for l in listings:
        key = l.asin or l.url or f"{l.channel}|{l.title}|{l.price}"
        if key not in seen:
            seen.add(key)
            out.append(l)
    return out


def _maps_cities(mkt: dict, demand) -> list[dict]:
    """Follow-up rule: sweep Maps in the regions where Trends shows demand, else the defaults."""
    cities = mkt["maps_cities"]
    if demand is not None and demand.top_regions:
        hot = [c for c in cities if any(c["name"].lower() in r.lower() for r in demand.top_regions)]
        rest = [c for c in cities if c not in hot]
        cities = hot + rest
    return cities[:MAPS_SWEEPS]


def _month(m: int) -> str:
    return ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][m - 1]
