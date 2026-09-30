from datetime import date, timedelta

import pytest

from exportscout.config import market
from exportscout.models import RegionInterest, TrendsSeries
from exportscout.pipeline.demand import LOW_VOLUME_MEAN, buying_window, demand_card, is_low_volume, month_span

UK = market("uk")  # buying_lead_months: 6
TERM = "brass lantern"
SEASON = {10: 30, 11: 60, 12: 80}  # other months 20; Oct is below the 1.1x peak threshold


def seasonal(month, recent=False, season=SEASON, base=20, growth=1.2):
    v = season.get(month, base)
    return int(round(v * growth)) if recent else v


def monthly_series(values_for=seasonal, start=date(2021, 10, 1), n=60, term=TERM):
    """Monthly points from ``start``; the last 12 are "recent"."""
    dates, values = [], []
    y, m = start.year, start.month
    for i in range(n):
        dates.append(date(y, m, 1).isoformat())
        values.append(values_for(m, recent=i >= n - 12))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return TrendsSeries(terms=[term], dates=dates, values={term: values}, evidence_id="google_trends:aaaa1111:0")


def weekly_series(values_for=seasonal, start=date(2021, 10, 3), end=date(2026, 9, 27), term=TERM):
    """Weekly points (Sunday starts); weeks starting in the last 12 months are "recent"."""
    dates, values = [], []
    d = start
    while d <= end:
        dates.append(d.isoformat())
        values.append(values_for(d.month, recent=d >= date(2025, 10, 1)))
        d += timedelta(days=7)
    return TrendsSeries(terms=[term], dates=dates, values={term: values}, evidence_id="google_trends:bbbb2222:0")


def card(series, today=date(2026, 9, 30), regions=(), related=(), suggestions=(), is_proxy=False):
    return demand_card(TERM, series, list(regions), list(related), list(suggestions), UK, today=today, is_proxy=is_proxy)


# --------------------------------------------------------------------------- stats


@pytest.mark.parametrize("make", [monthly_series, weekly_series], ids=["monthly", "weekly"])
def test_mean_yoy_and_peaks(make):
    c = card(make())
    # last 12 months: 9 x 24 + 36 + 72 + 96 = 420 -> 35.0; the year before: 350 / 12
    assert c.mean_12m == pytest.approx(35.0, abs=0.01)
    assert c.yoy_change == pytest.approx(0.2, abs=0.001)
    assert c.peak_months == [12, 11]  # Oct (30 vs 1.1 x ~29.6) is not a peak


def test_three_peaks_strongest_first():
    season = {10: 40, 11: 60, 12: 80, 3: 50}
    c = card(monthly_series(lambda m, recent: seasonal(m, recent, season=season)))
    assert c.peak_months == [12, 11, 3]


def test_flat_series_gives_single_top_month():
    c = card(monthly_series(lambda m, recent: 30))
    assert c.peak_months == [1]
    assert c.yoy_change == 0.0


def test_yoy_none_when_previous_year_is_zero_or_missing():
    zero_before = monthly_series(lambda m, recent: 40 if recent else 0)
    assert card(zero_before).yoy_change is None
    one_year = monthly_series(n=12)
    c = card(one_year)
    assert c.yoy_change is None
    assert c.mean_12m is not None


def test_last_12_months_by_date_not_list_order():
    s = monthly_series()
    shuffled = TrendsSeries(
        terms=s.terms,
        dates=list(reversed(s.dates)),
        values={TERM: list(reversed(s.values[TERM]))},
        evidence_id=s.evidence_id,
    )
    assert card(shuffled).mean_12m == card(s).mean_12m


# --------------------------------------------------------------------------- timing


def test_buying_window_note_months_ahead():
    c = card(monthly_series(), today=date(2026, 9, 30))
    # peaks Nov-Dec, lead 6 -> May-Jun; from Sep that is 8 months (May next year)
    assert c.months_to_window == 8
    assert c.buying_window_note == (
        "UK interest peaks Nov–Dec; buyers choose these ranges around May–Jun, 8 months from now."
    )


def test_buying_window_now():
    c = card(monthly_series(), today=date(2026, 6, 15))
    assert c.months_to_window == 0
    assert c.buying_window_note.endswith("around May–Jun: pitch now.")
    assert card(monthly_series(), today=date(2026, 4, 1)).buying_window_note.endswith(", next month.")


def test_buying_window_wraps_year():
    # peak Mar - 6 months -> Sep (previous year); peak Jan -> Jul
    assert buying_window([3], 6, date(2026, 9, 1)) == ([9], 0)
    assert buying_window([1], 6, date(2026, 9, 1)) == ([7], 10)  # Jul has passed: next year
    assert buying_window([12, 11], 6, date(2026, 7, 1)) == ([6, 5], 10)
    assert buying_window([12, 11], 6, date(2026, 1, 1)) == ([6, 5], 4)
    assert buying_window([], 6, date(2026, 1, 1)) == ([], None)


def test_month_span():
    assert month_span([12, 11]) == "Nov–Dec"
    assert month_span([1, 12]) == "Dec–Jan"
    assert month_span([12, 1, 2]) == "Dec–Feb"
    assert month_span([6, 12]) == "Jun and Dec"
    assert month_span([9, 1, 5]) == "Jan, May and Sep"
    assert month_span([3]) == "Mar"
    assert month_span([]) == ""


# --------------------------------------------------------------------------- low volume


def test_low_volume_rule():
    assert is_low_volume(None, TERM)
    assert is_low_volume(monthly_series(), "other term")
    thin = monthly_series(lambda m, recent: 3 if recent else 50)
    assert is_low_volume(thin, TERM)
    edge = monthly_series(lambda m, recent: int(LOW_VOLUME_MEAN))
    assert not is_low_volume(edge, TERM)
    assert not is_low_volume(weekly_series(), TERM)


# --------------------------------------------------------------------------- card fields


def test_regions_queries_timeline_evidence():
    s = monthly_series()
    regions = [
        RegionInterest(region="Wales", value=40, evidence_id="google_trends:cccc3333:0"),
        RegionInterest(region="England", value=100, evidence_id="google_trends:cccc3333:1"),
        RegionInterest(region="Scotland", value=55, evidence_id="google_trends:cccc3333:2"),
        RegionInterest(region="Northern Ireland", value=0, evidence_id="google_trends:cccc3333:3"),
    ]
    related = [f"related {i}" for i in range(12)]
    sugg = [f"brass lantern {i}" for i in range(10)]
    c = card(s, regions=regions, related=related, suggestions=sugg, is_proxy=True)
    assert c.top_regions == ["England", "Scotland", "Wales"]
    assert c.related_queries == related[:8]
    assert c.autocomplete == sugg[:8]
    assert c.is_proxy and c.term == TERM
    assert len(c.timeline) == 60
    assert c.timeline[0] == ("2021-10-01", 30)
    assert c.evidence_ids[0] == s.evidence_id
    assert set(c.evidence_ids) == {s.evidence_id} | {r.evidence_id for r in regions}


def test_card_without_series():
    c = demand_card(TERM, None, [], ["x"], ["y"], UK, today=date(2026, 9, 30))
    assert c.mean_12m is None and c.yoy_change is None
    assert c.peak_months == [] and c.months_to_window is None and c.buying_window_note is None
    assert c.timeline == [] and c.evidence_ids == []
    assert c.related_queries == ["x"] and c.autocomplete == ["y"]


def test_card_term_missing_from_series():
    c = demand_card("lantern", monthly_series(), [], [], [], UK, today=date(2026, 9, 30))
    assert c.mean_12m is None and c.timeline == [] and c.peak_months == []
