import pytest

from exportscout.config import market
from exportscout.models import (
    BuyerCandidate,
    BuyerSignal,
    DemandCard,
    Listing,
    OriginShare,
    PriceLadder,
    ProductIdentity,
    QuoteRange,
)
from exportscout.pipeline.scoring import GOOD_FIT, buyer_fit, market_score

UK = market("uk")
PRODUCT = ProductIdentity(
    product_type="hurricane lantern",
    keywords=["brass hurricane lantern", "gold metal lantern", "moroccan lanterns"],
    broad_term="lantern",
)
LADDER = PriceLadder(currency="GBP", n=10, min=15, p25=30, p50=42, p75=60, max=90)
QUOTE = QuoteRange(
    currency="GBP",
    retail_median=42,
    retail_ex_vat=35,
    fob_importer=7.69,
    fob_retailer=13.83,
    margin_retailer=0.38,
    verdict="go",
)
FIT_MAX = {"category": 25, "india": 20, "price_tier": 15, "activity": 15, "size": 15, "reachability": 10}


def sig(kind, i=0, detail=None):
    return BuyerSignal(kind=kind, detail=detail or f"{kind} seen", evidence_id=f"google:cccc3333:{kind}{i}")


def fit(**kw):
    c = BuyerCandidate(name=kw.pop("name", "Lantern & Co"), **kw)
    return buyer_fit(c, PRODUCT, ladder=LADDER, quote=QUOTE)


def points(candidate, component):
    return candidate.fit[component].points


# --------------------------------------------------------------------------- buyer fit


def test_ideal_buyer_scores_100_with_evidence():
    c = fit(
        domain="lanternco.co.uk",
        website="https://lanternco.co.uk",
        phone="+44 20 0000 0000",
        listing_prices=[45.0, 50.0],
        review_count=400,
        locations_count=2,
        signals=[sig(k) for k in ("category_match", "india_sourcing", "ads_active", "news", "trade_page", "size")],
    )
    assert c.fit_score == 100
    assert {k: v.points for k, v in c.fit.items()} == FIT_MAX
    assert c.fit["category"].evidence_ids == ["google:cccc3333:category_match0"]
    assert c.fit["india"].evidence_ids == ["google:cccc3333:india_sourcing0"]
    assert set(c.fit["activity"].evidence_ids) == {"google:cccc3333:ads_active0", "google:cccc3333:news0"}
    assert c.fit["reachability"].evidence_ids == ["google:cccc3333:trade_page0"]
    assert "£13.83" in c.fit["price_tier"].detail


def test_empty_buyer_gets_neutral_scores_only():
    original = BuyerCandidate(name="Unknown Ltd")
    c = buyer_fit(original, PRODUCT, ladder=LADDER, quote=None)
    assert {k: v.points for k, v in c.fit.items()} == {
        "category": 0,
        "india": 0,
        "price_tier": 6,
        "activity": 0,
        "size": 11,
        "reachability": 0,
    }
    assert c.fit_score == 17
    assert original.fit_score is None and original.fit == {}  # input not mutated
    assert all(comp.detail for comp in c.fit.values())


@pytest.mark.parametrize(
    "texts, expected",
    [
        (["Antique Brass Lanterns", "Moroccan lantern, gold"], 25),
        (["Hurricane Lamp in brass"], 12),
        (["Hurricane Lamp in brass", "Garden furniture"], 12),
        (["Home decor set", "Big sale on cushions"], 0),
        ([], 0),
    ],
)
def test_category_from_seen_texts(texts, expected):
    assert points(fit(seen_texts=texts), "category") == expected


@pytest.mark.parametrize(
    "prices, expected",
    [([42.0], 15), ([45.0, 80.0], 15), ([30.0, 35.0], 10), ([10.0, 20.0], 4), ([], 6), ([0.0], 6)],
)
def test_price_tier(prices, expected):
    assert points(fit(listing_prices=prices), "price_tier") == expected


def test_price_tier_without_ladder_is_neutral():
    c = buyer_fit(BuyerCandidate(name="X", listing_prices=[50.0]), PRODUCT, ladder=None, quote=None)
    assert points(c, "price_tier") == 6
    assert c.fit["price_tier"].detail == "No price data; neutral"


@pytest.mark.parametrize(
    "signals, creatives, expected",
    [
        (["ads_active"], None, 10),
        (["news"], None, 5),
        ([], 5, 4),
        (["news"], 5, 9),
        (["ads_active"], 12, 10),
        (["ads_active", "news"], 12, 15),
        (["ads_active", "ads_active", "news", "news"], 12, 15),
        ([], 0, 0),
    ],
)
def test_activity(signals, creatives, expected):
    c = fit(signals=[sig(k, i) for i, k in enumerate(signals)], ad_creatives=creatives)
    assert points(c, "activity") == expected


@pytest.mark.parametrize(
    "kw, expected",
    [
        ({"is_giant": True, "review_count": 10, "locations_count": 1}, 0),
        ({"kind": "marketplace"}, 0),
        ({}, 11),
        ({"locations_count": 2}, 15),
        ({"review_count": 120}, 15),
        ({"locations_count": 3, "review_count": 2999}, 15),
        ({"locations_count": 3, "review_count": 3000}, 9),
        ({"review_count": 5000}, 9),
        ({"locations_count": 10}, 9),
        ({"locations_count": 25, "review_count": 100}, 3),
    ],
)
def test_size(kw, expected):
    assert points(fit(**kw), "size") == expected


@pytest.mark.parametrize(
    "kw, expected",
    [
        ({}, 0),
        ({"domain": "x.co.uk"}, 4),
        ({"website": "https://x.co.uk"}, 4),
        ({"phone": "0161 000 0000"}, 3),
        ({"signals": [sig("trade_page")]}, 3),
        ({"domain": "x.co.uk", "phone": "0161", "signals": [sig("trade_page")]}, 10),
    ],
)
def test_reachability(kw, expected):
    assert points(fit(**kw), "reachability") == expected


def test_fit_components_within_bounds():
    cases = [fit(), fit(is_giant=True, listing_prices=[1.0]), fit(signals=[sig("ads_active"), sig("news")], ad_creatives=99)]
    for c in cases:
        for name, comp in c.fit.items():
            assert comp.max_points == FIT_MAX[name]
            assert 0 <= comp.points <= comp.max_points
        assert c.fit_score == pytest.approx(sum(comp.points for comp in c.fit.values()))
        assert 0 <= c.fit_score <= 100


# --------------------------------------------------------------------------- market score


def demand(mean=50.0, yoy=0.5, months=1, proxy=False):
    return DemandCard(
        term="brass lantern",
        is_proxy=proxy,
        mean_12m=mean,
        yoy_change=yoy,
        months_to_window=months,
        evidence_ids=["google_trends:aaaa1111:0"],
    )


def amazon(bought, i=0):
    return Listing(
        channel="amazon",
        title="Brass lantern",
        price=40.0,
        currency="GBP",
        bought_last_month=bought,
        evidence_id=f"amazon:dddd4444:{i}",
    )


def score(**kw):
    args = {"demand": None, "quote": None, "origin": None, "buyers": [], "listings": [], "market": UK}
    args.update(kw)
    return market_score(**args)


def comp(ms, name):
    return ms.components[name].points


@pytest.mark.parametrize(
    "d, listings, expected",
    [
        (demand(50, 0.5), [], 25.0),
        (demand(100, 2.0), [], 25.0),  # level and growth capped at 1
        (demand(25, 0.0), [], 12.5),  # 0.6 * 0.5 + 0.4 * 0.5
        (demand(25, None), [], 12.5),  # unknown growth counts as 0.5
        (demand(25, -0.8), [], 7.5),  # growth floored at 0
        (demand(25, 0.0), [amazon(600, 0), amazon(400, 1)], 15.0),  # +0.1 at >= 1000 bought
        (demand(25, 0.0), [amazon(600, 0), amazon(None, 1)], 12.5),
        (demand(50, 0.5), [amazon(5000)], 25.0),  # capped at 1
        (demand(25, 0.0, proxy=True), [amazon(1000)], 12.0),  # 15 x 0.8
    ],
)
def test_demand_component(d, listings, expected):
    ms = score(demand=d, listings=listings)
    assert comp(ms, "demand") == pytest.approx(expected)


def test_demand_component_evidence_and_detail():
    ms = score(demand=demand(25, 0.18, proxy=True), listings=[amazon(1200, 7)])
    c = ms.components["demand"]
    assert c.evidence_ids == ["google_trends:aaaa1111:0", "amazon:dddd4444:7"]
    assert "+18%" in c.detail and "1,200+" in c.detail and "proxy" in c.detail


@pytest.mark.parametrize(
    "margin, expected", [(0.38, 19.0), (0.5, 25.0), (0.9, 25.0), (0.0, 0.0), (-0.3, 0.0), (0.1, 5.0)]
)
def test_price_headroom(margin, expected):
    q = QUOTE.model_copy(update={"margin_retailer": margin})
    ms = score(quote=q, listings=[amazon(None, 1)])
    assert comp(ms, "price_headroom") == pytest.approx(expected)
    assert ms.components["price_headroom"].evidence_ids == ["amazon:dddd4444:1"]


def test_price_headroom_detail():
    ms = score(quote=QUOTE)
    assert ms.components["price_headroom"].detail == "Margin 38% at £13.83 FOB"


def test_price_headroom_missing():
    no_margin = score(quote=QUOTE.model_copy(update={"margin_retailer": None}))
    assert comp(no_margin, "price_headroom") == 0
    assert "unknown" in no_margin.components["price_headroom"].detail
    assert comp(score(), "price_headroom") == 0


def test_duty_advantage():
    uk = score()
    # 7.5 for zero duty + 7.5 x 0.027 / 0.04
    assert comp(uk, "duty_advantage") == pytest.approx(12.6)
    assert "0% duty on Indian goods vs 2.7%" in uk.components["duty_advantage"].detail
    wide = score(market={**UK, "duty_competitor": 0.06})
    assert comp(wide, "duty_advantage") == 15
    none = score(market={**UK, "duty_india": 0.03, "duty_competitor": 0.03})
    assert comp(none, "duty_advantage") == 0
    overridden = score(quote=QUOTE.model_copy(update={"assumptions": {"duty_pct": 0.027}}))
    assert comp(overridden, "duty_advantage") == 0


@pytest.mark.parametrize(
    "india, china, other, expected",
    [(3, 5, 2, 10.0), (3, 17, 0, 5.0), (0, 5, 0, 0.0), (5, 0, 0, 4.0), (13, 7, 0, 5.0), (1, 1, 0, 7.1)],
)
def test_proven_india_amazon_share(india, china, other, expected):
    o = OriginShare(
        checked=india + china + other + 1,
        india=india,
        china=china,
        other=other,
        unknown=1,
        evidence_ids=["amazon_product:eeee5555:0"],
    )
    ms = score(origin=o)
    assert comp(ms, "proven_india") == pytest.approx(expected)
    assert ms.components["proven_india"].evidence_ids == ["amazon_product:eeee5555:0"]


def test_proven_india_ebay_fallback_and_missing():
    assert comp(score(origin=OriginShare(unknown=4, ebay_total=10, ebay_from_india=3)), "proven_india") == 10
    assert comp(score(origin=OriginShare(unknown=4)), "proven_india") == 0
    missing = score(origin=None)
    assert comp(missing, "proven_india") == 0
    assert missing.components["proven_india"].detail == "No origin data"


def test_buyer_depth():
    def buyer(s, i):
        return BuyerCandidate(name=f"B{i}", fit_score=s, signals=[sig("website", i)])

    four_good = [buyer(s, i) for i, s in enumerate([90, 75, GOOD_FIT, 61, 59.9, None])]
    ms = score(buyers=four_good)
    assert comp(ms, "buyer_depth") == 7.5
    assert ms.components["buyer_depth"].detail.startswith("4 of 6 buyers")
    assert len(ms.components["buyer_depth"].evidence_ids) == 4
    assert comp(score(buyers=[buyer(80, i) for i in range(12)]), "buyer_depth") == 15
    assert comp(score(buyers=[]), "buyer_depth") == 0


@pytest.mark.parametrize(
    "months, expected", [(0, 10), (2, 10), (3, 7), (6, 7), (7, 4), (9, 4), (10, 2), (11, 2), (None, 0)]
)
def test_timing(months, expected):
    assert comp(score(demand=demand(months=months)), "timing") == expected


def test_all_inputs_missing():
    ms = score()
    assert set(ms.components) == {"demand", "price_headroom", "duty_advantage", "proven_india", "buyer_depth", "timing"}
    assert comp(ms, "demand") == 0 and comp(ms, "timing") == 0
    assert ms.total == pytest.approx(12.6)  # only the config-driven duty advantage
    assert all(c.detail for c in ms.components.values())


def test_total_is_sum_and_within_0_100():
    buyers = [BuyerCandidate(name=f"B{i}", fit_score=80) for i in range(8)]
    best = score(
        demand=demand(80, 1.0, months=0),
        quote=QUOTE.model_copy(update={"margin_retailer": 0.6}),
        origin=OriginShare(india=3, china=7),
        buyers=buyers,
        listings=[amazon(2000)],
        market={**UK, "duty_competitor": 0.05},
    )
    assert best.total == 100
    typical = score(
        demand=demand(30, 0.1, months=8), quote=QUOTE, origin=OriginShare(india=2, china=3, other=1), buyers=buyers[:3]
    )
    assert typical.total == pytest.approx(sum(c.points for c in typical.components.values()))
    assert 0 <= typical.total <= 100
    maxes = {k: c.max_points for k, c in typical.components.items()}
    assert maxes == {
        "demand": 25,
        "price_headroom": 25,
        "duty_advantage": 15,
        "proven_india": 10,
        "buyer_depth": 15,
        "timing": 10,
    }
