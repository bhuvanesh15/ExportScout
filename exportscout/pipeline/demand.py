"""Demand card from Trends + Autocomplete (plan §5 step 2). Deterministic; no I/O.

OWNER: agent B.
"""
from __future__ import annotations

import statistics
from datetime import date
from typing import Any

from exportscout.models import DemandCard, RegionInterest, TrendsSeries

# Below this 12-month mean interest the specific term is treated as too thin to use.
LOW_VOLUME_MEAN = 5.0

# A calendar month counts as a peak if its average is at least this multiple of the overall mean.
PEAK_FACTOR = 1.1
MAX_PEAKS = 3
MAX_REGIONS = 3
MAX_QUERIES = 8

MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

Month = tuple[int, int]  # (year, month)


def _monthly(series: TrendsSeries | None, term: str) -> dict[Month, float]:
    """Mean interest per (year, month), bucketing weekly or monthly points by their start date."""
    if series is None or term not in series.values:
        return {}
    buckets: dict[Month, list[int]] = {}
    for iso, value in zip(series.dates, series.values[term]):
        try:
            d = date.fromisoformat(iso[:10])
        except ValueError:
            continue
        buckets.setdefault((d.year, d.month), []).append(value)
    return {k: statistics.fmean(v) for k, v in sorted(buckets.items())}


def _index(m: Month) -> int:
    return m[0] * 12 + m[1] - 1


def _window_mean(monthly: dict[Month, float], start: int, end: int) -> float | None:
    """Mean of the monthly values whose month index is in [start, end]."""
    vals = [v for m, v in monthly.items() if start <= _index(m) <= end]
    return statistics.fmean(vals) if vals else None


def _last_two_years(monthly: dict[Month, float]) -> tuple[float | None, float | None]:
    """(mean of the last 12 months, mean of the 12 before), counted back from the latest month."""
    if not monthly:
        return None, None
    last = _index(max(monthly))
    return _window_mean(monthly, last - 11, last), _window_mean(monthly, last - 23, last - 12)


def is_low_volume(series: TrendsSeries | None, term: str) -> bool:
    """True if ``term`` has too little Trends volume to rely on (orchestrator then broadens).

    low = no series, term missing, or mean(last 12 months) < LOW_VOLUME_MEAN
    """
    mean_12m, _ = _last_two_years(_monthly(series, term))
    return mean_12m is None or mean_12m < LOW_VOLUME_MEAN


def peak_months(monthly: dict[Month, float]) -> list[int]:
    """Up to 3 calendar months, strongest first, whose average across years is
    >= 1.1 x the mean of the 12 calendar-month averages; else the single top month."""
    by_month: dict[int, list[float]] = {}
    for (_, month), v in monthly.items():
        by_month.setdefault(month, []).append(v)
    avg = {m: statistics.fmean(v) for m, v in by_month.items()}
    if not avg or max(avg.values()) <= 0:
        return []
    overall = statistics.fmean(avg.values())
    ranked = sorted(avg, key=lambda m: (-avg[m], m))
    peaks = [m for m in ranked if avg[m] >= PEAK_FACTOR * overall][:MAX_PEAKS]
    return peaks or ranked[:1]


def _shift(month: int, by: int) -> int:
    return (month - 1 + by) % 12 + 1


def month_span(months: list[int]) -> str:
    """Readable calendar span, e.g. [12, 11] -> "Nov–Dec", [1, 12] -> "Dec–Jan", [6, 12] -> "Jun and Dec"."""
    chosen = set(months)
    if not chosen:
        return ""
    if len(chosen) == 12:
        return "all year"
    start = min(m for m in chosen if _shift(m, -1) not in chosen)
    runs: list[list[int]] = []
    for step in range(12):
        m = _shift(start, step)
        if m not in chosen:
            continue
        if runs and _shift(runs[-1][-1], 1) == m:
            runs[-1].append(m)
        else:
            runs.append([m])
    parts = [MONTH_ABBR[r[0] - 1] if len(r) == 1 else f"{MONTH_ABBR[r[0] - 1]}–{MONTH_ABBR[r[-1] - 1]}" for r in runs]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def buying_window(peaks: list[int], lead_months: int, today: date) -> tuple[list[int], int | None]:
    """Window months (peak - lead, wrapping the year) and months from ``today`` to the nearest one.

    months_to_window = min((window - today.month) mod 12); 0 means the window is now.
    """
    windows = list(dict.fromkeys(_shift(p, -lead_months) for p in peaks))
    if not windows:
        return [], None
    return windows, min((w - today.month) % 12 for w in windows)


def _window_note(label: str, peaks: list[int], windows: list[int], months_to: int) -> str:
    head = f"{label} interest peaks {month_span(peaks)}; buyers choose these ranges around {month_span(windows)}"
    if months_to == 0:
        return f"{head}: pitch now."
    if months_to == 1:
        return f"{head}, next month."
    return f"{head}, {months_to} months from now."


def demand_card(
    term: str,
    series: TrendsSeries | None,
    regions: list[RegionInterest],
    related: list[str],
    suggestions: list[str],
    market: dict[str, Any],
    *,
    today: date | None = None,
    is_proxy: bool = False,
) -> DemandCard:
    """12-month mean, year-on-year change, peak months, top regions, and the next buying
    window (peak month minus market["buying_lead_months"]).

    yoy_change = mean(last 12 months) / mean(12 months before) - 1   (None if the earlier mean is 0 or missing)
    """
    today = today or date.today()
    monthly = _monthly(series, term)
    mean_12m, mean_prev = _last_two_years(monthly)
    yoy = round(mean_12m / mean_prev - 1, 3) if mean_12m is not None and mean_prev else None

    peaks = peak_months(monthly)
    windows, months_to = buying_window(peaks, int(market.get("buying_lead_months", 0)), today)
    label = market.get("short_label") or market.get("label") or "Market"
    note = _window_note(label, peaks, windows, months_to) if months_to is not None else None

    ranked_regions = sorted((r for r in regions if r.value > 0), key=lambda r: -r.value)
    timeline: list[tuple[str, int]] = []
    evidence: list[str] = []
    if series is not None and term in series.values:
        timeline = list(zip(series.dates, series.values[term]))
        evidence.append(series.evidence_id)
    evidence.extend(r.evidence_id for r in regions)

    return DemandCard(
        term=term,
        is_proxy=is_proxy,
        mean_12m=round(mean_12m, 2) if mean_12m is not None else None,
        yoy_change=yoy,
        peak_months=peaks,
        top_regions=[r.region for r in ranked_regions[:MAX_REGIONS]],
        related_queries=list(related[:MAX_QUERIES]),
        autocomplete=list(suggestions[:MAX_QUERIES]),
        months_to_window=months_to,
        buying_window_note=note,
        timeline=timeline,
        evidence_ids=list(dict.fromkeys(evidence)),
    )
