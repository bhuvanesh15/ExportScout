import pytest

from exportscout.config import market
from exportscout.models import Listing, PriceLadder
from exportscout.pipeline.prices import GO_MARGIN, TIGHT_MARGIN, fob_quote, price_ladder, quantile

UK = market("uk")


def listing(price, channel="amazon", currency="GBP", i=0):
    return Listing(
        channel=channel, title=f"Brass lantern {i}", price=price, currency=currency, evidence_id=f"{channel}:abcd1234:{i}"
    )


def ladder_at(p50, currency="GBP"):
    return PriceLadder(currency=currency, n=3, min=p50, p25=p50, p50=p50, p75=p50, max=p50)


# --------------------------------------------------------------------------- ladder


def test_quantile_matches_numpy_linear():
    vals = [10.0, 20.0, 30.0, 40.0]
    assert quantile(vals, 0.25) == pytest.approx(17.5)
    assert quantile(vals, 0.5) == pytest.approx(25.0)
    assert quantile(vals, 0.75) == pytest.approx(32.5)
    assert quantile([7.0], 0.9) == 7.0


def test_ladder_percentiles_and_evidence():
    ls = [listing(p, i=i) for i, p in enumerate([30, 40, 42, 50, 60])]
    lad = price_ladder(ls, "GBP")
    assert lad is not None
    assert (lad.n, lad.min, lad.p25, lad.p50, lad.p75, lad.max) == (5, 30, 40, 42, 50, 60)
    assert lad.currency == "GBP"
    assert lad.evidence_ids == [f"amazon:abcd1234:{i}" for i in range(5)]


def test_ladder_trims_outliers():
    # raw median 40 -> keep 8..200
    ls = [listing(p, i=i) for i, p in enumerate([5, 30, 40, 45, 50, 250])]
    lad = price_ladder(ls, "GBP")
    assert lad.n == 4
    assert (lad.min, lad.max) == (30, 50)
    assert "amazon:abcd1234:0" not in lad.evidence_ids
    assert "amazon:abcd1234:5" not in lad.evidence_ids


def test_ladder_keeps_boundary_prices():
    # raw median 40 -> exactly 8 and 200 survive
    lad = price_ladder([listing(p, i=i) for i, p in enumerate([8, 40, 40, 200])], "GBP")
    assert lad.n == 4


def test_ladder_filters_currency_and_missing_prices():
    ls = [
        listing(20, i=0),
        listing(30, i=1),
        listing(None, i=2),
        listing(0, i=3),
        listing(-5, i=4),
        listing(35, currency="USD", i=5),
        listing(40, currency=None, i=6),
    ]
    assert price_ladder(ls, "GBP") is None  # only 2 usable prices
    ls.append(listing(25, i=7))
    lad = price_ladder(ls, "gbp")
    assert lad.n == 3 and lad.p50 == 25


def test_ladder_none_when_too_few_after_trim():
    assert price_ladder([listing(40, i=0), listing(41, i=1), listing(1000, i=2)], "GBP") is None
    assert price_ladder([], "GBP") is None


def test_ladder_channel_stats():
    ls = [
        listing(20, "amazon", i=0),
        listing(30, "amazon", i=1),
        listing(40, "amazon", i=2),
        listing(50, "ebay", i=3),
        listing(45, "google_shopping", i=4),
        listing(55, "google_shopping", i=5),
    ]
    lad = price_ladder(ls, "GBP")
    assert set(lad.by_channel) == {"amazon", "ebay", "google_shopping"}
    amz = lad.by_channel["amazon"]
    assert (amz.n, amz.p25, amz.p50, amz.p75) == (3, 25, 30, 35)
    eb = lad.by_channel["ebay"]
    assert (eb.n, eb.p25, eb.p50, eb.p75) == (1, 50, 50, 50)
    gs = lad.by_channel["google_shopping"]
    assert (gs.n, gs.p25, gs.p50, gs.p75) == (2, 47.5, 50, 52.5)


def test_ladder_rounds_to_pence():
    lad = price_ladder([listing(p, i=i) for i, p in enumerate([10.0, 10.01, 10.02, 10.04])], "GBP")
    assert lad.p25 == 10.01  # 10.0075 -> 10.01


# --------------------------------------------------------------------------- quote


def test_worked_example_plan_6_1():
    q = fob_quote(ladder_at(42.0), UK, fx_rate=None, unit_cost_inr=None)
    assert q.retail_median == 42.0
    assert q.retail_ex_vat == 35.00
    assert q.fob_retailer == pytest.approx(13.83, abs=0.005)
    assert q.fob_importer == pytest.approx(7.69, abs=0.005)
    assert q.assumptions == {
        "vat_rate": 0.2,
        "retailer_markup": 2.2,
        "importer_markup": 1.8,
        "freight_ins_pct": 0.15,
        "duty_pct": 0.0,
    }
    assert q.verdict == "unknown"
    assert q.currency == "GBP"


def test_inr_values_and_margins():
    q = fob_quote(ladder_at(42.0), UK, fx_rate=110.0, unit_cost_inr=800.0, extra_costs_inr=100.0)
    fob_r = 35 / 2.2 / 1.15
    assert q.fob_retailer_inr == round(fob_r * 110, 2)
    assert q.fob_importer_inr == round(fob_r / 1.8 * 110, 2)
    assert q.total_cost_inr == 900.0
    assert q.margin_retailer == round((q.fob_retailer_inr - 900) / q.fob_retailer_inr, 3)
    assert q.margin_importer == round((q.fob_importer_inr - 900) / q.fob_importer_inr, 3)
    assert q.margin_retailer == 0.409
    assert q.margin_importer < 0
    assert q.verdict == "go"


@pytest.mark.parametrize(
    "margin, verdict",
    [(0.40, "go"), (GO_MARGIN, "go"), (0.20, "tight"), (TIGHT_MARGIN, "tight"), (0.05, "no_go"), (-0.5, "no_go")],
)
def test_verdicts(margin, verdict):
    fob_inr = 35 / 2.2 / 1.15 * 100.0
    cost = round(fob_inr * (1 - margin), 2)
    q = fob_quote(ladder_at(42.0), UK, fx_rate=100.0, unit_cost_inr=cost)
    assert q.margin_retailer == pytest.approx(margin, abs=0.001)
    assert q.verdict == verdict


def test_unknown_verdict_without_cost_or_fx():
    lad = ladder_at(42.0)
    no_cost = fob_quote(lad, UK, fx_rate=110.0, unit_cost_inr=None)
    assert no_cost.verdict == "unknown" and no_cost.margin_retailer is None
    assert no_cost.fob_retailer_inr is not None  # ₹ values still shown
    no_fx = fob_quote(lad, UK, fx_rate=None, unit_cost_inr=500.0)
    assert no_fx.verdict == "unknown" and no_fx.fob_retailer_inr is None and no_fx.margin_importer is None
    assert no_fx.total_cost_inr == 500.0
    zero_fx = fob_quote(lad, UK, fx_rate=0.0, unit_cost_inr=500.0)
    assert zero_fx.verdict == "unknown" and zero_fx.fx_rate is None


def test_overrides_replace_assumptions():
    q = fob_quote(
        ladder_at(42.0),
        UK,
        fx_rate=None,
        unit_cost_inr=None,
        overrides={"retailer_markup": 2.5, "duty_pct": 0.027, "freight_ins_pct": 0.10, "unrelated": 9.0},
    )
    assert q.assumptions["retailer_markup"] == 2.5
    assert q.assumptions["duty_pct"] == 0.027
    assert "unrelated" not in q.assumptions
    assert q.fob_retailer == round(35 / 2.5 / (1 + 0.10 + 0.027), 2)
    assert q.fob_importer == round(35 / 2.5 / (1 + 0.10 + 0.027) / 1.8, 2)


def test_vat_override():
    q = fob_quote(ladder_at(42.0), UK, fx_rate=None, unit_cost_inr=None, overrides={"vat_rate": 0.0})
    assert q.retail_ex_vat == 42.0
