"""Write tests/fixtures/sample_brief.json: a fictional Brief for UI development and the app smoke test.

Every name, URL and number here is made up. Nothing comes from SerpApi.

    python scripts/make_sample_brief.py
"""
from __future__ import annotations

import hashlib
import math
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from exportscout.models import (  # noqa: E402
    Brief,
    BuyerCandidate,
    BuyerSignal,
    ChannelStats,
    DemandCard,
    Evidence,
    JobPosting,
    Listing,
    MarketRow,
    MarketScore,
    OriginShare,
    Pitch,
    PriceLadder,
    ProductIdentity,
    QuoteRange,
    ReviewTheme,
    RunInputs,
    ScoreComponent,
    StepEvent,
)

OUT = ROOT / "tests" / "fixtures" / "sample_brief.json"
FETCHED = "2026-09-30T09:15:00+00:00"
SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "AUD": "A$", "AED": "AED "}
COST_INR = 650 + 60  # unit cost + extra costs, as in the sample inputs

# Market Compare: a quick Amazon-only scan per market. Prices, rates and scores are made up.
# code, label, short label, currency, Amazon domain, keyword, sample FX (INR per unit), VAT, freight, duty,
# duty detail, duty note, Trends interest, reviews on the top results, bought last month, prices
OTHER_MARKETS = [
    ("us", "United States", "US", "USD", "amazon.com", "brass hurricane lantern", 88.20, 0.0, 0.18, 0.157,
     "10% Section 301 + 5.7% normal duty",
     "Normal US duty for the HS line plus the 10% Section 301 duty on Indian goods (sample note).",
     55, 9840, 3100,
     [18.99, 19.99, 21.99, 22.99, 23.49, 24.99, 25.99, 26.99, 27.99, 27.99, 29.99, 31.99, 34.99, 36.99, 39.99, 44.99]),
    ("de", "Germany", "DE", "EUR", "amazon.de", "Windlicht Messing", 97.50, 0.19, 0.15, 0.027,
     "2.7% EU third-country duty",
     "The India–EU FTA is concluded but not in force yet (sample note).",
     18, 1310, 400,
     [22.99, 24.99, 27.99, 29.99, 31.99, 33.99, 34.99, 35.99, 37.99, 39.99, 42.99, 46.99, 49.99, 54.99]),
    ("ae", "United Arab Emirates", "UAE", "AED", "amazon.ae", "brass hurricane lantern", 24.10, 0.05, 0.10, 0.0,
     "0% under the India–UAE CEPA",
     "Needs a valid certificate of origin; the standard duty is 5% (sample note).",
     24, 140, None,
     [89.00, 119.00, 145.00, 199.00]),
    ("au", "Australia", "AU", "AUD", "amazon.com.au", "brass hurricane lantern", 57.80, 0.10, 0.18, 0.0,
     "0% under the India–Australia ECTA",
     "Every tariff line is duty-free for Indian goods from 1 Jan 2026, with a certificate of origin (sample note).",
     62, 2480, 900,
     [49.95, 55.00, 59.99, 64.95, 69.99, 74.95, 76.99, 79.95, 84.99, 89.95, 99.00, 119.95]),
]

evidence: dict[str, Evidence] = {}


def ev(engine: str, query: str, index: int, title: str, path: str, snippet: str | None = None) -> str:
    key = hashlib.sha256(f"{engine}|{query}".encode()).hexdigest()[:8]
    eid = f"{engine}:{key}:{index}"
    evidence[eid] = Evidence(
        id=eid,
        engine=engine,
        fetched_at=FETCHED,
        title=title,
        url=f"https://example.com/sample/{path}",
        snippet=snippet,
        query=f'{engine} {query}',
    )
    return eid


def percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return round(s[lo] + (s[hi] - s[lo]) * (pos - lo), 2)


def stats(values: list[float]) -> ChannelStats:
    return ChannelStats(n=len(values), p25=percentile(values, 0.25), p50=percentile(values, 0.5), p75=percentile(values, 0.75))


def make_listings() -> list[Listing]:
    amazon_q = 'amazon_domain="amazon.co.uk" k="brass hurricane lantern"'
    shop_q = 'q="brass hurricane lantern" gl="uk"'
    amazon = [
        ("Sample antique brass hurricane lantern, 30 cm", 34.99, "Sample Lantern House", 412, 200),
        ("Example gold metal lantern with glass, large", 42.50, "Example Homeware", 128, 100),
        ("Demo brass candle lantern, set of 2", 55.00, "Demo Interiors", 87, 50),
        ("Placeholder hammered brass lantern", 27.99, None, 1540, 500),
        ("Sample Moroccan-style brass lantern", 39.95, "Fictional Brass & Bloom", 64, None),
        ("Example hurricane lamp, antique gold finish", 24.99, None, 2210, 1000),
        ("Demo brass garden lantern, 45 cm", 64.99, "Demo Interiors", 33, None),
        ("Sample tealight lantern, brushed brass", 29.50, None, 356, 100),
        ("Placeholder vintage brass storm lantern", 48.00, "Placeholder Home Store", 19, None),
        ("Example brass lantern with handle, small", 22.99, None, 980, 300),
        ("Sample handmade brass lantern (made in India)", 44.99, "Sample Lantern House", 71, 50),
        ("Demo pillar candle hurricane, gold", 36.00, None, 245, None),
    ]
    shopping = [
        ("Sample brass hurricane lantern", 45.00, "Sample Lantern House"),
        ("Example antique lantern, gold", 38.00, "Example Homeware"),
        ("Fictional brass storm lantern, large", 89.00, "Fictional Brass & Bloom"),
        ("Demo hurricane candle holder, brass", 32.00, "Demo Interiors"),
        ("Placeholder metal lantern, gold", 28.00, "Placeholder Home Store"),
        ("Sample brass lantern, hand-finished", 58.00, "Sample Lantern House"),
        ("Example hurricane lantern with glass chimney", 41.00, "Example Homeware"),
        ("Fictional hammered lantern", 72.00, "Fictional Brass & Bloom"),
        ("Demo brass lantern, 35 cm", 47.50, "Demo Interiors"),
        ("Placeholder brass tealight lantern", 26.00, "Placeholder Home Store"),
        ("Sample Moroccan brass lantern", 65.00, "Imaginary Imports"),
        ("Example brass candle lantern, pair", 52.00, "Imaginary Imports"),
        ("Demo brass lantern, antique finish", 43.00, "Demo Interiors"),
    ]
    out = []
    for i, (title, price, brand, reviews, bought) in enumerate(amazon):
        eid = ev("amazon", amazon_q, i, title, f"amazon/{i}")
        out.append(Listing(
            channel="amazon", title=title, price=price, currency="GBP", url=evidence[eid].url, brand=brand,
            rating=round(3.9 + (i % 5) * 0.2, 1), reviews=reviews, bought_last_month=bought,
            asin=f"B0SAMPLE{i:02d}", sponsored=i in (1, 6), evidence_id=eid,
        ))
    for i, (title, price, merchant) in enumerate(shopping):
        eid = ev("google_shopping", shop_q, i, title, f"shopping/{i}")
        out.append(Listing(
            channel="google_shopping", title=title, price=price, currency="GBP", url=evidence[eid].url,
            merchant=merchant, rating=round(4.1 + (i % 4) * 0.2, 1), reviews=10 + i * 7, evidence_id=eid,
        ))
    return out


def make_timeline() -> list[tuple[str, int]]:
    start = date(2021, 10, 3)
    points = []
    for w in range(260):
        d = start + timedelta(weeks=w)
        season = math.cos((d.timetuple().tm_yday - 340) / 365 * 2 * math.pi)
        value = 38 + w * 0.06 + 26 * max(season, 0) ** 2 + 6 * season + (w * 7919 % 11) - 5
        points.append((d.isoformat(), max(0, min(100, round(value)))))
    return points


def main() -> None:
    ev("google_lens", 'country="gb" type="all"', 0, "Sample brass lantern look-alike", "lens/0")
    listings = make_listings()
    prices = [x.price for x in listings if x.price is not None]
    ladder = PriceLadder(
        currency="GBP", n=len(prices), min=min(prices), max=max(prices),
        p25=percentile(prices, 0.25), p50=percentile(prices, 0.5), p75=percentile(prices, 0.75),
        by_channel={
            ch: stats([x.price for x in listings if x.channel == ch and x.price is not None])
            for ch in ("amazon", "google_shopping")
        },
        evidence_ids=[x.evidence_id for x in listings],
    )

    fx_eid = ev("google_finance", 'q="GBP-INR"', 0, "GBP / INR (sample rate)", "finance/gbp-inr")
    assumptions = {"vat_rate": 0.20, "retailer_markup": 2.2, "importer_markup": 1.8, "freight_ins_pct": 0.15, "duty_pct": 0.0}
    fx = 112.40
    ex_vat = round(ladder.p50 / (1 + assumptions["vat_rate"]), 2)
    fob_r = round(ex_vat / assumptions["retailer_markup"] / (1 + assumptions["freight_ins_pct"]), 2)
    fob_i = round(fob_r / assumptions["importer_markup"], 2)
    cost = 650 + 60
    quote = QuoteRange(
        currency="GBP", retail_median=ladder.p50, retail_ex_vat=ex_vat, fob_importer=fob_i, fob_retailer=fob_r,
        fx_rate=fx, fob_importer_inr=round(fob_i * fx, 1), fob_retailer_inr=round(fob_r * fx, 1),
        unit_cost_inr=650, total_cost_inr=cost,
        margin_retailer=round((fob_r * fx - cost) / (fob_r * fx), 3),
        margin_importer=round((fob_i * fx - cost) / (fob_i * fx), 3),
        verdict="go", assumptions=assumptions,
    )

    trends_eid = ev("google_trends", 'q="brass hurricane lantern" geo="GB" date="today 5-y"', 0, "Interest over time: brass hurricane lantern (UK)", "trends/timeseries")
    regions_eid = ev("google_trends", 'q="brass hurricane lantern" geo="GB" data_type="GEO_MAP_0"', 0, "Interest by region (UK)", "trends/regions")
    auto_eid = ev("google_autocomplete", 'q="brass lantern" gl="uk"', 0, "Autocomplete: brass lantern", "autocomplete/0")
    timeline = make_timeline()
    last, prev = [v for _, v in timeline[-52:]], [v for _, v in timeline[-104:-52]]
    demand = DemandCard(
        term="brass hurricane lantern",
        mean_12m=round(sum(last) / len(last), 1),
        yoy_change=round(sum(last) / sum(prev) - 1, 3),
        peak_months=[12, 11, 10],
        top_regions=["London", "South East", "North West"],
        related_queries=["gold lantern", "large hurricane lantern", "outdoor lantern brass"],
        autocomplete=["brass lantern large", "brass lantern outdoor", "brass lantern with glass"],
        months_to_window=1,
        buying_window_note="UK buyers pick October–December ranges around April–June. Pitch now for the 2027 season.",
        timeline=timeline,
        evidence_ids=[trends_eid, regions_eid, auto_eid],
    )

    prod = [
        ev("amazon_product", 'asin="B0SAMPLE10"', 0, "Sample handmade brass lantern: country of origin India", "amazon_product/B0SAMPLE10"),
        ev("amazon_product", 'asin="B0SAMPLE00"', 0, "Sample antique brass hurricane lantern: country of origin China", "amazon_product/B0SAMPLE00"),
    ]
    ebay_eid = ev("ebay", 'ebay_domain="ebay.co.uk" _nkw="brass hurricane lantern"', 3, "Sample brass lantern, located in India", "ebay/3")
    origin = OriginShare(
        checked=6, india=2, china=3, other=0, unknown=1, ebay_total=40, ebay_from_india=7,
        india_examples=[prod[0]], evidence_ids=[*prod, ebay_eid],
    )

    themes = [
        ReviewTheme(label="Tarnishing / discolouration", kind="complaint", count=9, share=0.31,
                    quotes=["Lovely at first but went dull within a month."],
                    fix="Offer anti-tarnish lacquer coating and include care instructions.", evidence_ids=[prod[1]]),
        ReviewTheme(label="Broken or dented in transit", kind="complaint", count=6, share=0.22,
                    quotes=["Glass arrived cracked, box was far too thin."],
                    fix="Use double-wall cartons with moulded pulp inserts; drop-test to ISTA 1A.", evidence_ids=[prod[1]]),
        ReviewTheme(label="Smaller than expected", kind="complaint", count=4, share=0.12,
                    quotes=["Much smaller than the photos suggest."],
                    fix="Put exact dimensions (cm) and a scale photo on the spec sheet.", evidence_ids=[prod[0]]),
        ReviewTheme(label="Looks premium / beautiful", kind="praise", count=14, share=0.48,
                    quotes=["Looks far more expensive than it was."], evidence_ids=[prod[0]]),
    ]

    jobs = make_jobs()
    buyers = make_buyers(listings, jobs)
    markets = make_markets(listings, fx, fx_eid)
    pitches = [
        Pitch(
            buyer_name=b.name,
            subject=f"Handmade brass hurricane lanterns for {b.name}, duty-free from India",
            body=(
                f"Dear {b.name} buying team,\n\n"
                "We are a 30-person brassware workshop in Moradabad, India. We noticed your lantern range and "
                "think our antique-gold hurricane lantern would sit well alongside it.\n\n"
                "Since 15 July 2026 our products enter the UK at 0% duty under the India–UK CETA. We can offer:\n"
                "- anti-tarnish lacquer as standard\n- double-wall export cartons (drop-tested)\n"
                "- exact dimensions and a scale photo on every spec sheet\n\n"
                f"MOQ is 200 pieces, FOB Moradabad from £{quote.fob_importer:.2f}. "
                "May I send photos and a sample?\n\nKind regards,\n[Your name]\n[Company], Moradabad"
            ),
            evidence_ids=[s.evidence_id for s in b.signals[:3]],
        )
        for b in buyers[:3]
    ]

    components = {
        "demand": ScoreComponent(points=17, max_points=25, detail=f"12-month mean interest {demand.mean_12m:.0f}/100, {demand.yoy_change:+.0%} year on year; strong Amazon sales", evidence_ids=[trends_eid]),
        "price_headroom": ScoreComponent(points=22, max_points=25, detail=f"Margin {quote.margin_retailer:.0%} at the direct-to-retailer FOB", evidence_ids=[fx_eid]),
        "duty_advantage": ScoreComponent(points=12, max_points=15, detail="0% duty from India under CETA vs 2.7% for non-FTA origins (assumption)"),
        "proven_india": ScoreComponent(points=7, max_points=10, detail="2 of 5 products with a known origin are made in India", evidence_ids=prod),
        "buyer_depth": ScoreComponent(points=9, max_points=15, detail="3 buyers with fit ≥ 60", evidence_ids=[b.signals[0].evidence_id for b in buyers[:3]]),
        "timing": ScoreComponent(points=7, max_points=10, detail="Next buying window opens in about 1 month", evidence_ids=[trends_eid]),
    }
    score = MarketScore(total=sum(c.points for c in components.values()), components=components)

    news_eid = next(e.id for e in evidence.values() if e.engine == "google_news")
    best = next(m for m in markets if not m.is_home)
    summary = (
        f"**Brass hurricane lanterns sell for £{ladder.min:.0f}–£{ladder.max:.0f} in the UK**, with a median of "
        f"£{ladder.p50:.2f} across {ladder.n} listings [ev:{listings[0].evidence_id}] [ev:{listings[13].evidence_id}]. "
        f"Working back through VAT, markups and freight gives a quote range of **£{quote.fob_importer:.2f}–£{quote.fob_retailer:.2f} FOB**, "
        f"at GBP-INR {fx:.2f} [ev:{fx_eid}].\n\n"
        f"UK interest peaks October–December and is up year on year [ev:{trends_eid}]. "
        f"Two of the six top Amazon products checked are made in India [ev:{prod[0]}], so Indian lanterns already sell here. "
        f"The most common complaint is tarnishing [ev:{prod[1]}]; offering an anti-tarnish lacquer is an easy edge.\n\n"
        f"The top buyer, {buyers[0].name}, runs UK ads and has a trade-account page [ev:{buyers[0].signals[1].evidence_id}]. "
        f"Homeware news mentions new independent stores opening [ev:{news_eid}], and {buyers[0].name} is hiring a "
        f"lighting buyer [ev:{jobs[0].evidence_id}].\n\n"
        f"Outside the UK, a quick Amazon scan ranks {best.label} next: about {best.quote.margin_retailer:.0%} margin "
        f"at the retailer FOB, and duty is {best.duty_detail} [ev:{best.evidence_ids[0]}].\n\n"
        "_Sample data for UI development: every name and number here is fictional._"
    )

    steps = [
        StepEvent(step=1, name="Identify", message="Found 14 UK look-alikes (£22–£89). Keywords: brass hurricane lantern, gold metal lantern", credits_used=1),
        StepEvent(step=2, name="Demand", message="Interest peaks Oct–Dec; up year on year; strongest in London & South East", credits_used=5),
        StepEvent(step=3, name="Prices", message=f"Median £{ladder.p50:.2f} across {ladder.n} listings", credits_used=10),
        StepEvent(step=4, name="Origin", message="Of 6 products checked, 2 made in India, 3 in China, 1 unknown", credits_used=16),
        StepEvent(step=5, name="Reviews", message="Top complaint: tarnishing (31%), then broken in transit (22%)", credits_used=16),
        StepEvent(step=6, name="Buyers", message=f"11 candidates → 5 after dedupe; {len(jobs)} buying-role job ads",
                  credits_used=23),
        StepEvent(step=7, name="Enrich", message="Enriched top 5 buyers (Search, Ads Transparency, News)", credits_used=33),
        StepEvent(step=8, name="Markets", message=(
            f"Quick Amazon scan of {len(OTHER_MARKETS)} more markets: {best.label} next "
            f"({best.quote.margin_retailer:.0%} margin); US duty 15.7%"), credits_used=42),
        StepEvent(step=9, name="Write", message="Brief ready · 42 credits used", credits_used=42),
    ]

    brief = Brief(
        inputs=RunInputs(
            image_path="demo_cache/images/lantern.jpg", description="brass hurricane lantern",
            unit_cost_inr=650, extra_costs_inr=60, moq=200, material="brass", finish="antique gold",
        ),
        market="uk",
        market_label="United Kingdom",
        product=ProductIdentity(
            product_type="hurricane lantern",
            keywords=["brass hurricane lantern", "gold metal lantern", "antique brass lantern"],
            broad_term="lantern", material="brass", style_tags=["antique", "gold"],
        ),
        listings=listings,
        ladder=ladder,
        quote=quote,
        demand=demand,
        origin=origin,
        themes=themes,
        buyers=buyers,
        jobs=jobs,
        market_score=score,
        markets=markets,
        headline=f"Brass hurricane lantern → United Kingdom: GO ({score.total:.0f}/100) · sample data",
        summary_md=summary,
        spec_improvements=[t.fix for t in themes if t.fix],
        pitches=pitches,
        evidence=list(evidence.values()),
        steps=steps,
        warnings=[
            "SAMPLE DATA: this brief is fictional and exists only for UI development.",
            "Only 6 Amazon products were checked for country of origin; treat the India share as a rough signal.",
        ],
        credits_used=42,
        created_at=FETCHED,
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(brief.model_dump_json(indent=1), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(listings)} listings, {len(buyers)} buyers, {len(markets)} markets, "
          f"{len(jobs)} jobs, {len(brief.evidence)} evidence")


def make_buyers(listings: list[Listing], jobs: list[JobPosting]) -> list[BuyerCandidate]:
    specs = [
        ("Sample Lantern House", "lanternhouse.example", "online_brand", "London", 92, [34.99, 44.99, 45.0, 58.0]),
        ("Fictional Brass & Bloom", "brassandbloom.example", "retailer", "Manchester", 81, [39.95, 72.0, 89.0]),
        ("Demo Interiors", "demointeriors.example", "retailer", "Birmingham", 68, [55.0, 64.99, 32.0, 47.5]),
        ("Imaginary Imports", "imaginaryimports.example", "importer", "Leeds", 57, [65.0, 52.0]),
        ("Placeholder Home Store", "placeholderhome.example", "wholesaler", "London", 44, [48.0, 28.0, 26.0]),
    ]
    buyers = []
    for rank, (name, domain, kind, city, target, prices) in enumerate(specs):
        site = f"https://{domain}/"
        web = ev("google", f'q="{name} brass lantern" gl="uk"', rank, f"{name}: lanterns and candle holders", f"google/{rank}", "Handmade homeware, trade accounts welcome.")
        ads = ev("google_ads_transparency_center", f'text="{domain}" region="2826"', 0, f"{name}: ad creatives in the UK", f"ads/{rank}")
        signals = [
            BuyerSignal(kind="category_match", detail="Sells brass lanterns and candle holders", evidence_id=web),
            BuyerSignal(kind="ads_active", detail=f"{12 - rank * 2} UK ad creatives, last shown Sep 2026", evidence_id=ads),
            BuyerSignal(kind="trade_page", detail="Has a trade / wholesale account page", evidence_id=web),
        ]
        if rank < 2:
            maps = ev("google_maps", f'q="homeware shop" ll="{city}"', rank, f"{name}, {city}", f"maps/{rank}")
            signals.append(BuyerSignal(kind="phone", detail="Phone number on Google Maps", evidence_id=maps))
        if rank == 0:
            signals.insert(1, BuyerSignal(kind="india_sourcing", detail="Lists a lantern made in India on Amazon", evidence_id=next(
                x.evidence_id for x in listings if "made in India" in x.title)))
        if rank == 1:
            news = ev("google_news", 'q="independent homeware store opening UK"', 0, "Sample: independent homeware chain opens two new stores", "news/0")
            signals.append(BuyerSignal(kind="news", detail="News: opening two new stores", evidence_id=news))
        for job in jobs:
            if job.company == name and not job.is_recruiter:
                signals.append(BuyerSignal(kind="hiring", detail=f"Hiring a {job.title} ({job.posted})", evidence_id=job.evidence_id))
        fit = split_fit(target, signals)
        buyers.append(BuyerCandidate(
            name=name, domain=domain, kind=kind, city=city, website=site,
            phone=f"+44 20 7946 0{rank:03d}" if rank < 2 else None,
            sources=sorted({evidence[s.evidence_id].engine for s in signals}),
            listing_prices=prices, review_count=120 + rank * 40, locations_count=1 + (rank == 1),
            ad_creatives=12 - rank * 2, enriched=True, signals=signals,
            fit_score=round(sum(c.points for c in fit.values()), 1), fit=fit,
            why=f"{kind.replace('_', ' ').capitalize()} in {city} already selling lanterns at £{min(prices):.0f}–£{max(prices):.0f}",
        ))
    return buyers


def split_fit(target: float, signals: list[BuyerSignal]) -> dict[str, ScoreComponent]:
    by_kind = {s.kind: s.evidence_id for s in signals}
    weights = {"category": 25, "india": 20, "price_tier": 15, "activity": 15, "size": 15, "reachability": 10}
    details = {
        "category": ("Lantern range matches this product", "category_match"),
        "india": ("Stocks made-in-India products" if "india_sourcing" in by_kind else "No India sourcing seen", "india_sourcing"),
        "price_tier": ("Their median price supports our FOB range", "category_match"),
        "activity": ("UK ads live in the last 30 days" + ("; hiring a buyer now" if "hiring" in by_kind else ""), "ads_active"),
        "size": ("Independent size: a 30-worker unit can serve them", "trade_page"),
        "reachability": ("Website and trade page" + (", phone" if "phone" in by_kind else ""), "trade_page"),
    }
    ratio = target / 100
    out = {}
    for name, max_pts in weights.items():
        detail, kind = details[name]
        pts = max_pts * ratio if name != "india" or "india_sourcing" in by_kind else 0.0
        ids = [by_kind[kind]] if kind in by_kind else []
        if name == "activity" and "hiring" in by_kind:
            ids.append(by_kind["hiring"])
        out[name] = ScoreComponent(points=round(pts, 1), max_points=max_pts, detail=detail, evidence_ids=ids)
    shortfall = target - sum(c.points for c in out.values())
    out["category"].points = round(min(25, out["category"].points + shortfall), 1)
    return out


def make_jobs() -> list[JobPosting]:
    """Fictional Google Jobs ads for buying roles: company-level data only."""
    specs = [
        # company, title, location, via, posted, days, query, India, overseas, agency
        ("Sample Lantern House", "Lighting Buyer", "London, UK", "LinkedIn", "5 days ago", 5.0, "lighting buyer",
         False, True, False),
        ("Example Home Group", "Assistant Homeware Buyer", "Manchester, UK", "Indeed", "2 days ago", 2.0, "homeware buyer",
         True, True, False),
        ("Imaginary Recruitment Partners", "Senior Buyer, Home Accessories", "London, UK", "Reed", "1 day ago", 1.0,
         "homeware buyer", False, False, True),
        ("Placeholder Living", "Product Developer, Home Décor", "Leeds, UK", "Totaljobs", "3 weeks ago", 21.0,
         "homeware buyer", False, True, False),
    ]
    snippets = {
        (False, True): "…regular sourcing trips to supplier factories overseas…",
        (True, True): "…experience working with suppliers in India and the Far East…",
    }
    jobs = []
    for i, (company, title, where, via, posted, days, query, india, overseas, agency) in enumerate(specs):
        eid = ev("google_jobs", f'q="{query}" location="United Kingdom"', i, f"{company} is hiring: {title}", f"jobs/{i}",
                 f"{where} · {posted} · via {via}")
        jobs.append(JobPosting(
            company=company, title=title, location=where, via=via, posted=posted, posted_days=days,
            link=evidence[eid].url, query=query, mentions_india=india, mentions_overseas=overseas, is_recruiter=agency,
            snippet=snippets.get((india, overseas)), evidence_id=eid,
        ))
    return jobs


def sample_money(value: float, currency: str) -> str:
    return f"{SYMBOLS.get(currency, currency + ' ')}{value:,.2f}"


def work_back(prices: list[float], currency: str, fx: float, assumptions: dict[str, float],
              eids: list[str]) -> tuple[PriceLadder, QuoteRange]:
    """Amazon-only ladder and FOB quote for one market (same formula as the pipeline)."""
    ladder = PriceLadder(
        currency=currency, n=len(prices), min=min(prices), max=max(prices),
        p25=percentile(prices, 0.25), p50=percentile(prices, 0.5), p75=percentile(prices, 0.75),
        by_channel={"amazon": stats(prices)}, evidence_ids=eids,
    )
    a = assumptions
    ex_vat = round(ladder.p50 / (1 + a["vat_rate"]), 2)
    fob_r = round(ex_vat / a["retailer_markup"] / (1 + a["freight_ins_pct"] + a["duty_pct"]), 2)
    fob_i = round(fob_r / a["importer_markup"], 2)
    r_inr, i_inr = round(fob_r * fx, 1), round(fob_i * fx, 1)
    margin_r, margin_i = round((r_inr - COST_INR) / r_inr, 3), round((i_inr - COST_INR) / i_inr, 3)
    verdict = "go" if margin_r >= 0.25 else "tight" if margin_r >= 0.10 else "no_go"
    quote = QuoteRange(
        currency=currency, retail_median=ladder.p50, retail_ex_vat=ex_vat, fob_importer=fob_i, fob_retailer=fob_r,
        fx_rate=fx, fob_importer_inr=i_inr, fob_retailer_inr=r_inr, unit_cost_inr=650, total_cost_inr=COST_INR,
        margin_retailer=margin_r, margin_importer=margin_i, verdict=verdict, assumptions=a,
    )
    return ladder, quote


def market_fit(quote: QuoteRange, trends: int, reviews: int, duty_detail: str, duty: float, n: int, thin: bool,
               fx_eid: str, trends_eid: str, eids: list[str]) -> MarketScore:
    """Market Fit: margin 40, demand 30, trade access 20, data depth 10 (sample weights)."""
    margin = quote.margin_retailer or 0.0
    fob = sample_money(quote.fob_retailer, quote.currency)
    components = {
        "margin": ScoreComponent(
            points=round(40 * min(max(margin / 0.6, 0.0), 1.0), 1), max_points=40,
            detail=f"{margin:.0%} margin at the retailer FOB ({fob} ≈ ₹{quote.fob_retailer_inr:,.0f}) against your ₹{COST_INR} cost",
            evidence_ids=[fx_eid, *eids[:2]]),
        "demand": ScoreComponent(
            points=round(30 * (0.5 * trends / 100 + 0.5 * min(reviews / 8000, 1.0)), 1), max_points=30,
            detail=f"Google Trends interest {trends}/100; {reviews:,} reviews on the top Amazon results",
            evidence_ids=[trends_eid, *eids[:2]]),
        "trade_access": ScoreComponent(
            points=round(20 * (1 - min(duty / 0.2, 1.0)), 1), max_points=20, detail=f"{duty_detail} (HS 9405.50)"),
        "data_depth": ScoreComponent(
            points=round(10 * min(n / 16, 1.0), 1), max_points=10,
            detail=f"{n} priced Amazon listings" + (": thin data, treat the median as rough" if thin else ""),
            evidence_ids=eids[:3]),
    }
    return MarketScore(total=round(sum(c.points for c in components.values()), 1), components=components)


def make_markets(listings: list[Listing], uk_fx: float, uk_fx_eid: str) -> list[MarketRow]:
    """The UK row (from the Amazon UK listings) plus fictional quick scans of four more markets, best first."""
    trends_eid = ev("google_trends", 'q="brass hurricane lantern" data_type="GEO_MAP_0" date="today 12-m"', 0,
                    "Interest by country: brass hurricane lantern (sample)", "trends/countries")
    uk_amazon = [x for x in listings if x.channel == "amazon" and x.price is not None]
    home = ("uk", "United Kingdom", "UK", "GBP", "amazon.co.uk", "brass hurricane lantern", uk_fx, 0.20, 0.15, 0.0,
            "0% under the India–UK CETA", "In force since 15 Jul 2026; needs a valid proof of origin (sample note).",
            100, sum(x.reviews or 0 for x in uk_amazon), sum(x.bought_last_month or 0 for x in uk_amazon),
            [x.price for x in uk_amazon])
    rows = []
    for spec in [home, *OTHER_MARKETS]:
        code, label, short, cur, domain, keyword, fx, vat, freight, duty, detail, note, trends, reviews, bought, prices = spec
        if code == "uk":
            eids, fx_eid = [x.evidence_id for x in uk_amazon], uk_fx_eid
        else:
            q = f'amazon_domain="{domain}" k="{keyword}"'
            eids = [ev("amazon", q, i, f"Sample brass lantern listing {i + 1} ({domain})", f"amazon-{code}/{i}")
                    for i in range(len(prices))]
            fx_eid = ev("google_finance", f'q="{cur}-INR"', 0, f"{cur} / INR (sample rate)", f"finance/{cur.lower()}-inr")
        assumptions = {"vat_rate": vat, "retailer_markup": 2.2, "importer_markup": 1.8, "freight_ins_pct": freight,
                       "duty_pct": duty}
        ladder, quote = work_back(prices, cur, fx, assumptions, eids)
        thin = len(prices) < 5
        rows.append(MarketRow(
            code=code, label=label, short_label=short, currency=cur, keyword=keyword, is_home=code == "uk",
            listings_n=len(prices), ladder=ladder, quote=quote, fx_rate=fx, hs_code="9405.50", duty_pct=duty,
            duty_detail=detail, duty_note=note, duty_sources=[f"https://example.com/sample/duty/{code}"],
            last_verified="2026-09-30" if code == "uk" else "2026-10-01", trends_interest=trends,
            review_depth=reviews, bought_last_month=bought,
            score=market_fit(quote, trends, reviews, detail, duty, len(prices), thin, fx_eid, trends_eid, eids),
            thin_data=thin, evidence_ids=[*eids, fx_eid, trends_eid],
        ))
    return sorted(rows, key=lambda r: -r.score.total)


if __name__ == "__main__":
    main()
