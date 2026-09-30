import pytest

from exportscout.config import category, market
from exportscout.models import Listing, MarketRow, PriceLadder, ProductIdentity, QuoteRange, RegionInterest
from exportscout.pipeline.markets import (
    best_alternative,
    build_row,
    effective_duty,
    hs_hint,
    rank_markets,
    translate_keyword,
    world_market,
)
from exportscout.pipeline.prices import fob_quote, price_ladder, verdict_for

CAT = category("metal_handicrafts")
UK, US, DE, AE, AU = (market(code) for code in ("uk", "us", "de", "ae", "au"))
MAX_POINTS = {"margin": 40, "demand": 30, "trade_access": 20, "data_depth": 10}


def listing(price, i=0, currency="AUD", channel="amazon", reviews=None, bought=None):
    return Listing(
        channel=channel,
        title=f"Brass lantern {i}",
        price=price,
        currency=currency,
        reviews=reviews,
        bought_last_month=bought,
        evidence_id=f"{channel}:{currency.lower()}00000:{i}",
    )


def listings(prices, currency="AUD", **kw):
    return [listing(p, i=i, currency=currency, **kw) for i, p in enumerate(prices)]


def product(product_type, *keywords):
    return ProductIdentity(product_type=product_type, keywords=list(keywords) or [product_type], broad_term="lantern")


# --------------------------------------------------------------------------- inputs


def test_world_market():
    assert world_market(CAT) == {"label": "Worldwide", "trends_geo": None, "trends_cat": 11}


@pytest.mark.parametrize(
    ("mkt", "hs", "duty"),
    [
        (US, "9405.50", 0.157),  # 10% Section 301 + 5.7% normal duty
        (US, "7419.80", 0.425),  # 10% + 0% normal + 50% copper duty x 65% copper
        (US, None, 0.13),  # 10% + 3% assumed normal duty
        (DE, "9405.50", 0.027),
        (DE, None, 0.03),  # unknown HS line -> mfn_default
        (DE, "8306.29", 0.03),  # no table entry -> mfn_default
        (AE, "9405.50", 0.0),
        (AU, "7419.80", 0.0),
        (UK, "9405.50", 0.0),
        (UK, None, 0.0),
    ],
)
def test_effective_duty(mkt, hs, duty):
    assert effective_duty(mkt, hs)[0] == duty


def test_effective_duty_details():
    assert effective_duty(US, "9405.50")[1] == "10% base duty on Indian goods + 5.7% normal duty (HS 9405.50)"
    planter = effective_duty(US, "7419.80")[1]
    assert planter.startswith("10% base duty on Indian goods + 0% normal duty (HS 7419.80)")
    assert planter.endswith("+ 32.5% Section 232 copper duty (50% of the copper content) (assumes 65% copper)")
    assert effective_duty(DE, "9405.50")[1] == "2.7% normal duty (HS 9405.50)"
    assert effective_duty(DE, None)[1] == "3% normal duty (assumed; HS line unknown)"
    for mkt in (AE, AU, UK):
        assert effective_duty(mkt, "7419.80") == (0.0, "0% duty on Indian goods")


def test_effective_duty_longest_prefix_wins_and_extra_duty_needs_its_prefix():
    mkt = {
        "duty_india": 0.0,
        "mfn_default": 0.05,
        "mfn_by_hs": {"9405": 0.02, "9405.50": 0.04},
        "extra_duty": [{"hs_prefix": "7419", "rate": 0.5, "copper_share": 0.65, "label": "copper duty"}],
    }
    assert effective_duty(mkt, "9405.50.30")[0] == 0.04
    assert effective_duty(mkt, "9405.10")[0] == 0.02
    assert effective_duty(mkt, "7419.80")[0] == 0.375  # 5% default + 32.5% copper duty
    assert effective_duty(mkt, None)[0] == 0.05  # no HS: no extra duty


def test_hs_hint():
    assert hs_hint(product("hurricane lantern", "brass hurricane lantern"), CAT) == "9405.50"
    assert hs_hint(product("hammered brass planter", "brass planters"), CAT) == "7419.80"
    assert hs_hint(product("brass bowl", "brass candle bowl"), CAT) == "7419.80"  # the product type comes first
    assert hs_hint(product("brass ornament", "brass ornament", "brass plant pots"), CAT) == "7419.80"  # from a keyword
    assert hs_hint(product("brass ornament"), CAT) is None
    assert hs_hint(product("brass teapot"), CAT) is None  # whole words only
    assert hs_hint(product("hurricane lantern"), {}) is None


@pytest.mark.parametrize(
    ("english", "german"),
    [
        ("brass hurricane lantern", "Messing Windlicht"),
        ("hammered brass planter", "gehämmert Messing Pflanzkübel"),
        ("Brass  Lantern", "Messing Laterne"),  # case-insensitive, spaces collapsed
        ("brass plant pot", "Messing Blumentopf"),  # longest phrase first, not "plant Topf"
        ("brass teapot stand", "Messing teapot stand"),  # whole words only; unknown words kept
    ],
)
def test_translate_keyword_de(english, german):
    assert translate_keyword(english, "de", CAT) == german


def test_translate_keyword_without_a_word_map_is_unchanged():
    assert translate_keyword("brass  lantern", "fr", CAT) == "brass  lantern"
    assert translate_keyword("brass lantern", "de", {}) == "brass lantern"


# --------------------------------------------------------------------------- rows


def test_build_row_works_back_to_fob_and_margin():
    row = build_row(
        "au", AU, listings([30, 40, 44, 50, 60]), keyword="brass lantern", fx=55.0, unit_cost_inr=500, extra_costs_inr=50, hs="9405.50"
    )
    q = row.quote
    # 44 / 1.10 GST = 40.00 ex-VAT; / 2.2 retailer markup / (1 + 18% freight + 0% duty) = A$15.41 FOB
    assert (q.retail_median, q.retail_ex_vat, q.fob_retailer, q.fob_importer) == (44.0, 40.0, 15.41, 8.56)
    assert q.fob_retailer_inr == 847.46 and q.total_cost_inr == 550.0  # 15.4083 x 55
    assert q.margin_retailer == 0.351 and q.verdict == "go"  # (847.46 - 550) / 847.46
    assert q.assumptions == {"vat_rate": 0.1, "retailer_markup": 2.2, "importer_markup": 1.8, "freight_ins_pct": 0.18, "duty_pct": 0.0}
    assert (row.code, row.label, row.short_label, row.currency, row.keyword) == ("au", "Australia", "AU", "AUD", "brass lantern")
    assert (row.duty_pct, row.duty_detail, row.hs_code, row.fx_rate) == (0.0, "0% duty on Indian goods", "9405.50", 55.0)
    assert row.duty_note and row.duty_sources and row.last_verified == "2026-10-01"
    assert row.listings_n == 5 and row.thin_data and not row.is_home and row.score is None


def test_build_row_puts_the_duty_in_the_fob_formula():
    row = build_row("us", US, listings([20, 25, 30, 35, 40], currency="USD"), keyword="k", fx=88.0, unit_cost_inr=None, hs="7419.80")
    assert row.duty_pct == 0.425 and row.quote.assumptions["duty_pct"] == 0.425
    assert row.quote.fob_retailer == round(30 / 2.2 / (1 + 0.18 + 0.425), 2)  # no VAT in US list prices
    assert row.quote.margin_retailer is None and row.quote.fob_retailer_inr is not None


def test_build_row_demand_proxies_and_evidence():
    amazon = [listing(40, i=i, reviews=i + 1, bought=50 if i < 2 else None) for i in range(30)]
    shopping = Listing(channel="google_shopping", title="Lantern", price=40, currency="AUD", reviews=9999, evidence_id="google_shopping:ssss0000:0")
    trends = RegionInterest(region="Australia", value=64, evidence_id="google_trends:tttt0000:3")
    row = build_row(
        "au", AU, [shopping, *amazon], keyword="brass lantern", fx=55.0, unit_cost_inr=None, trends=trends,
        evidence_ids=["google_finance:ffff0000:0", "amazon:aud00000:0"],
    )  # fmt: skip
    assert row.review_depth == sum(range(1, 21))  # the first 20 Amazon results with reviews; Shopping is not Amazon
    assert row.bought_last_month == 100 and row.trends_interest == 64
    assert row.listings_n == 31 and not row.thin_data
    assert row.evidence_ids[0] == "google_shopping:ssss0000:0"  # ladder first
    assert row.evidence_ids[-2:] == ["google_finance:ffff0000:0", "google_trends:tttt0000:3"]
    assert len(row.evidence_ids) == len(set(row.evidence_ids)) == 33  # 31 listings + FX + Trends, deduped


def test_build_row_thin_data_and_missing_ladder():
    few = build_row("ae", AE, listings([100, 120], currency="AED"), keyword="k", fx=24.0, unit_cost_inr=500)
    assert few.ladder is None and few.quote is None and few.listings_n == 0 and few.thin_data
    assert few.review_depth is None and few.bought_last_month is None and few.trends_interest is None
    wrong_currency = build_row("ae", AE, listings([100] * 12, currency="USD"), keyword="k", fx=24.0, unit_cost_inr=500)
    assert wrong_currency.ladder is None
    nine = build_row("ae", AE, listings([100] * 9, currency="AED"), keyword="k", fx=24.0, unit_cost_inr=500)
    ten = build_row("ae", AE, listings([100] * 10, currency="AED"), keyword="k", fx=24.0, unit_cost_inr=500)
    assert nine.thin_data and not ten.thin_data and ten.listings_n == 10


def test_home_row_user_duty_override_wins():
    ls = listings([30, 40, 42, 50, 60], currency="GBP")
    sidebar = {"vat_rate": 0.2, "retailer_markup": 2.5, "importer_markup": 1.8, "freight_ins_pct": 0.15, "duty_pct": 0.05}
    row = build_row("uk", UK, ls, keyword="brass lantern", fx=None, unit_cost_inr=None, hs="9405.50", is_home=True, overrides=sidebar)
    assert row.is_home and row.duty_pct == 0.05 and row.duty_detail == "5% duty on Indian goods (your setting)"
    assert row.quote.assumptions["duty_pct"] == 0.05 and row.quote.assumptions["retailer_markup"] == 2.5
    assert row.quote == fob_quote(price_ladder(ls, "GBP"), UK, fx_rate=None, unit_cost_inr=None, overrides=sidebar)
    # The app always sends every sidebar value: an unchanged duty keeps the config wording.
    same = build_row("uk", UK, ls, keyword="k", fx=None, unit_cost_inr=None, is_home=True, overrides={**sidebar, "duty_pct": 0.0})
    assert (same.duty_pct, same.duty_detail) == (0.0, "0% duty on Indian goods")


# --------------------------------------------------------------------------- ranking


def mrow(code, *, margin=None, quoted=True, trends=None, reviews=None, duty=0.0, n=30, is_home=False):
    """A Market Compare row with a quote (unit cost set only when there is a margin)."""
    quote = ladder = None
    if quoted:
        ladder = PriceLadder(currency="AUD", n=n, min=30, p25=40, p50=44, p75=50, max=60, evidence_ids=[f"amazon:{code}000000:0"])
        quote = QuoteRange(
            currency="AUD", retail_median=44.0, retail_ex_vat=40.0, fob_importer=8.56, fob_retailer=15.41, fx_rate=55.0,
            fob_retailer_inr=847.46, unit_cost_inr=None if margin is None else 500.0, margin_retailer=margin,
            verdict=verdict_for(margin),
        )  # fmt: skip
    return MarketRow(
        code=code, label=code.upper(), currency="AUD", keyword="brass lantern", is_home=is_home, listings_n=n if quoted else 0,
        ladder=ladder, quote=quote, duty_pct=duty, duty_detail=f"{duty:.1%} duty", trends_interest=trends,
        review_depth=reviews, thin_data=not quoted or n < 10,
        evidence_ids=[f"amazon:{code}000000:0", *([f"google_trends:tttt0000:{code}"] if trends else [])],
    )  # fmt: skip


def points(row):
    return {k: c.points for k, c in row.score.components.items()}


def test_rank_markets_components_and_order():
    rows = [
        mrow("uk", margin=0.3, trends=80, reviews=1000, is_home=True),
        mrow("us", margin=0.25, trends=40, reviews=2000, duty=0.157, n=12),
        mrow("de", quoted=False, duty=0.027),
        mrow("au", margin=0.6, trends=80, reviews=4000, n=40),
    ]
    ranked = rank_markets(rows, trends_term="candle lantern")
    assert [r.code for r in ranked] == ["au", "uk", "us", "de"]
    assert all(r.score is None for r in rows)  # inputs are not mutated

    au, uk, us, de = ranked
    assert {k: c.max_points for k, c in au.score.components.items()} == MAX_POINTS
    assert points(au) == MAX_POINTS and au.score.total == 100
    assert points(uk) == {"margin": 24, "demand": 22.5, "trade_access": 20, "data_depth": 10} and uk.score.total == 76.5
    assert points(us) == {"margin": 20, "demand": 15, "trade_access": 7.4, "data_depth": 5} and us.score.total == 47.4
    assert points(de) == {"margin": 0, "demand": 0, "trade_access": 17.8, "data_depth": 0} and de.score.total == 17.8

    c = au.score.components
    assert c["margin"].detail == "Margin 60% at A$15.41 FOB (₹847)" and c["margin"].evidence_ids == ["amazon:au000000:0"]
    assert c["demand"].detail == "Trends interest 80/100 for “candle lantern”; 4,000 reviews on the top Amazon results"
    assert c["demand"].evidence_ids == ["google_trends:tttt0000:au"]
    assert c["trade_access"].detail == "0.0% duty" and c["data_depth"].detail == "40 priced Amazon listings"
    d = de.score.components
    assert d["margin"].detail == "No quote: too few prices or no exchange rate"
    assert d["demand"].detail.startswith("No Trends interest recorded")
    assert d["data_depth"].detail == "0 priced Amazon listings — thin data"


def test_rank_markets_demand_from_reviews_without_trends():
    ranked = rank_markets([mrow("q", margin=0.5, reviews=1000), mrow("p", margin=0.5, reviews=3000), mrow("r", margin=0.5)])
    assert [r.code for r in ranked] == ["p", "q", "r"]
    assert [r.score.components["demand"].points for r in ranked] == [30, 10, 0]
    assert ranked[0].score.components["demand"].detail == "No Trends data by country; 3,000 reviews on the top Amazon results"


def test_rank_markets_ties_go_to_the_higher_margin():
    rows = [mrow("c", quoted=False, duty=0.3), mrow("b", margin=-0.2, duty=0.25, n=5), mrow("a", margin=0.0, duty=0.25, n=5)]
    ranked = rank_markets(rows)
    assert [r.score.total for r in ranked] == [0, 0, 0]
    assert [r.code for r in ranked] == ["a", "b", "c"]  # margin 0% > -20% > none
    assert rank_markets([]) == []


def test_best_alternative_skips_the_home_market_and_rows_without_a_margin():
    ranked = rank_markets(
        [
            mrow("uk", margin=0.6, trends=80, reviews=4000, is_home=True),  # ranked first, but it is the home market
            mrow("de", quoted=False, trends=80, reviews=4000),  # no quote
            mrow("ae", trends=80, reviews=4000),  # a quote but no margin (no unit cost)
            mrow("us", margin=0.1, duty=0.157),
        ]
    )
    assert [r.code for r in ranked] == ["uk", "ae", "de", "us"]
    assert ranked[1].score.components["margin"].detail == "No margin: add your unit cost"
    assert best_alternative(ranked).code == "us"
    assert best_alternative([r for r in ranked if r.code != "us"]) is None
    assert best_alternative([]) is None
