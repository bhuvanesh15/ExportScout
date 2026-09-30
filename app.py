"""ExportScout: one product photo -> UK buyers, a quote range and a pitch.

Run with ``streamlit run app.py``. Demo Mode replays recorded SerpApi responses and needs no keys.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from exportscout.agent.orchestrator import (
    CACHE_DIR,
    DEFAULT_BUDGET,
    keys_available,
    load_demo_products,
    make_clients,
    run_scout,
)
from exportscout.config import category, market
from exportscout.models import (
    Brief,
    BuyerCandidate,
    DemandCard,
    Evidence,
    JobPosting,
    MarketRow,
    QuoteRange,
    RunInputs,
    StepEvent,
)
from exportscout.serp.client import BudgetExceeded, CacheMiss, SerpApiError, SerpClient, redact_text

try:
    from exportscout.llm.client import LLMCacheMiss
except ImportError:
    LLMCacheMiss = CacheMiss

log = logging.getLogger("exportscout.app")

APP_DIR = Path(__file__).resolve().parent
MARKET = "uk"
ONE_LINER = "One product photo → UK buyers, a quote range and a pitch"
DISCLAIMER = (
    "Estimates from live search data, not quotes. Verify duty rates and rules of origin before quoting. "
    "Public business information only; no automated outreach."
)
DEMO_MISS = "This input isn't in the Demo Mode recordings — use a demo product or turn off Demo Mode"
TABS = ["Brief", "Markets", "Buyers", "Pitch", "Evidence"]

WHY_NOW = [
    ("27 Aug 2025", "US tariff on Indian goods reaches **50%**",
     "Orders worth over ₹300 crore halted; about 2 lakh jobs at risk",
     "https://thefederal.com/category/business/us-tariff-impact-aromatic-brass-handicraft-industries-hit-204380"),
    ("2 Feb 2026", "US–India interim deal: **18%**", "Relief, but short-lived",
     "https://www.whitehouse.gov/fact-sheets/2026/02/fact-sheet-the-united-states-and-india-announce-historic-trade-deal/"),
    ("20 Feb 2026", "US Supreme Court strikes down IEEPA tariffs; a temporary 10% Section 122 surcharge replaces them",
     "More uncertainty", None),
    ("**15 Jul 2026**", "**India–UK CETA in force: zero duty on ~99% of tariff lines, including handicrafts**",
     "**A new zero-duty market where most units have no buyers yet**",
     "https://www.drishtiias.com/daily-updates/daily-news-analysis/india-uk-ceta-comes-into-effect"),
    ("24 Jul 2026", "Section 122 expires; a **10% Section 301** tariff applies to India",
     "US policy keeps shifting, so relying on one market is risky", "https://www.honigman.com/alert-3462"),
    ("Late 2026 / 2027", "India–EU FTA concluded (27 Jan 2026) but not yet in force", "Next market: build the EU pipeline now",
     "https://www.orfonline.org/expert-speak/the-india-eu-fta-from-political-agreement-to-ratification-and-coming-into-force"),
]

VERDICTS = {
    "go": ("GO", "green", ":material/check_circle:"),
    "tight": ("TIGHT", "orange", ":material/warning:"),
    "no_go": ("NO-GO", "red", ":material/block:"),
    "unknown": ("MARGIN UNKNOWN", "gray", ":material/help:"),
}
STATUS_ICONS = {
    "running": ":material/progress_activity:",
    "done": ":material/check_circle:",
    "skipped": ":material/skip_next:",
    "warning": ":material/warning:",
}
SCORE_LABELS = {
    "demand": "Demand",
    "price_headroom": "Price headroom",
    "duty_advantage": "Duty advantage",
    "proven_india": "Proven for Indian goods",
    "buyer_depth": "Buyer depth",
    "timing": "Timing",
}
MARKET_FIT_LABELS = {
    "margin": "Margin",
    "demand": "Demand",
    "trade_access": "Trade access",
    "data_depth": "Data depth",
}
FIT_LABELS = {
    "category": "Category & style match",
    "india": "Sourcing from India",
    "price_tier": "Price-tier fit",
    "activity": "Activity",
    "size": "Right size",
    "reachability": "Reachability",
}
KIND_LABELS = {
    "retailer": "Retailer",
    "online_brand": "Online brand",
    "wholesaler": "Wholesaler",
    "importer": "Importer",
    "marketplace": "Marketplace seller",
    "unknown": "Unknown",
}
CHANNEL_LABELS = {"lens": "Google Lens", "google_shopping": "Google Shopping", "amazon": "Amazon", "ebay": "eBay"}
ENGINE_LABELS = {
    "google_lens": "Google Lens",
    "google_shopping": "Google Shopping",
    "amazon": "Amazon Search",
    "amazon_product": "Amazon Product",
    "ebay": "eBay",
    "google_trends": "Google Trends",
    "google_autocomplete": "Google Autocomplete",
    "google_finance": "Google Finance",
    "google": "Google Search",
    "google_maps": "Google Maps",
    "google_ads_transparency_center": "Ads Transparency Center",
    "google_news": "Google News",
    "google_jobs": "Google Jobs",
}
CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "INR": "₹", "AUD": "A$", "AED": "AED "}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# --------------------------------------------------------------------------- pure helpers

_CITE_RE = re.compile(r"\[ev:\s*([^\]]+)\]")
_SECRET_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


def _split_ids(group: str) -> list[str]:
    return [p.strip().removeprefix("ev:").strip() for p in re.split(r"[,;]", group) if p.strip()]


def _safe_url(url: str) -> str:
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def md_escape(text: str) -> str:
    return re.sub(r"([\\`*_\[\]$|<>])", r"\\\1", text)


def md_link(title: str, url: str | None) -> str:
    return f"[{md_escape(title)}]({_safe_url(url)})" if url else md_escape(title)


def cited_evidence(md: str, evidence: list[Evidence]) -> list[Evidence]:
    """Evidence cited as ``[ev:ID]`` in ``md``, in order of first citation. Unknown IDs are skipped."""
    by_id = {e.id: e for e in evidence}
    out: list[Evidence] = []
    for match in _CITE_RE.finditer(md):
        for eid in _split_ids(match.group(1)):
            if eid in by_id and by_id[eid] not in out:
                out.append(by_id[eid])
    return out


def render_citations(md: str, evidence: list[Evidence]) -> str:
    """Replace ``[ev:ID]`` citations with numbered Markdown links ``[[n]](url)``, rendered as a linked "[n]".

    Numbers follow first citation, matching ``cited_evidence``. Several IDs may share one
    bracket (``[ev:a, ev:b]``). IDs not in ``evidence`` are dropped.
    """
    numbers = {e.id: i + 1 for i, e in enumerate(cited_evidence(md, evidence))}
    urls = {e.id: e.url for e in evidence}

    def link(eid: str) -> str:
        n = numbers[eid]
        return f"[[{n}]]({_safe_url(urls[eid])})" if urls[eid] else f"[{n}]"

    def replace(match: re.Match[str]) -> str:
        return " ".join(link(eid) for eid in _split_ids(match.group(1)) if eid in numbers)

    return _CITE_RE.sub(replace, md)


def safe_text(text: str, limit: int | None = 400) -> str:
    """Strip anything that looks like an API key before showing text in the UI."""
    for name in ("SERPAPI_API_KEY", "SERPAPI_KEY", "ANTHROPIC_API_KEY"):
        text = redact_text(text, os.environ.get(name, ""))
    text = _SECRET_RE.sub("REDACTED", text)
    return text[:limit] if limit else text


def friendly_error(exc: BaseException) -> str:
    if isinstance(exc, (CacheMiss, LLMCacheMiss)):
        return DEMO_MISS
    if isinstance(exc, BudgetExceeded):
        return "The credit budget ran out before the brief was finished. Raise the budget in the sidebar and try again."
    if isinstance(exc, SerpApiError):
        return (f"SerpApi returned an error: {safe_text(str(exc))}. "
                "Check your key and remaining searches, or switch to Demo Mode.")
    if isinstance(exc, NotImplementedError):
        return "Part of the ExportScout pipeline isn't wired up yet in this build. Try Demo Mode or a later version."
    return f"Something went wrong ({type(exc).__name__}: {safe_text(str(exc))}). Try again, or switch to Demo Mode."


def step_line(event: StepEvent) -> str:
    icon = STATUS_ICONS.get(event.status, "")
    return f"{icon} **{event.step}. {event.name}** · {event.message} · `{event.credits_used} credits`"


def upload_path(filename: str, data: bytes) -> Path:
    ext = Path(filename).suffix.lower().lstrip(".") or "jpg"
    ext = "jpg" if ext == "jpeg" else ext
    return CACHE_DIR / "uploads" / f"{hashlib.sha256(data).hexdigest()[:16]}.{ext}"


def export_or_none(name: str, brief: Brief) -> str | None:
    """Run ``exportscout.report.brief.<name>(brief)``; None when that export isn't available yet."""
    try:
        from exportscout.report import brief as report

        return getattr(report, name)(brief)
    except NotImplementedError:
        return None
    except Exception as exc:
        log.warning("export %s failed: %s", name, safe_text(str(exc)))
        return None


def symbol(currency: str | None) -> str:
    return CURRENCY_SYMBOLS.get(currency or "", f"{currency} " if currency else "")


def money(value: float | None, currency: str | None = "GBP", decimals: int = 2) -> str:
    return "—" if value is None else f"{symbol(currency)}{value:,.{decimals}f}"


def pct(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value:+.0%}" if signed else f"{value:.0%}"


def month_span(months: list[int]) -> str:
    """Top peak months as a calendar span, e.g. [12, 11, 10] -> "Oct–Dec"."""
    ms = sorted({m for m in months[:3] if 1 <= m <= 12})
    if not ms:
        return "—"
    for start in ms:
        run = [(start - 1 + i) % 12 + 1 for i in range(len(ms))]
        if set(run) == set(ms):
            return MONTHS[start - 1] if len(ms) == 1 else f"{MONTHS[start - 1]}–{MONTHS[run[-1] - 1]}"
    return ", ".join(MONTHS[m - 1] for m in ms)


def fmt_ts(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts).strftime("%d %b %Y %H:%M UTC")
    except ValueError:
        return ts


def why_now_table() -> str:
    rows = ["| Date | Event | Effect on Moradabad | Source |", "|---|---|---|---|"]
    for when, event, effect, url in WHY_NOW:
        rows.append(f"| {when} | {event} | {effect} | {f'[link]({url})' if url else ''} |")
    return "\n".join(rows)


def buyer_evidence_ids(buyer: BuyerCandidate) -> list[str]:
    ids = [s.evidence_id for s in buyer.signals]
    for comp in buyer.fit.values():
        ids.extend(comp.evidence_ids)
    return list(dict.fromkeys(ids))


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else many or word + 's'}"


def rate(value: float | None) -> str:
    """A duty, tax or freight rate with at most one decimal: 0.157 -> "15.7%", 0.2 -> "20%"."""
    return "—" if value is None else f"{value:.1%}".replace(".0%", "%")


def short_url(url: str, limit: int = 48) -> str:
    text = re.sub(r"^https?://(?:www\.)?", "", url).rstrip("/")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fob_range(q: QuoteRange) -> str:
    return f"{money(q.fob_importer, q.currency)}–{money(q.fob_retailer, q.currency)}"


def fob_formula(q: QuoteRange) -> str:
    """The retail → FOB working for one quote, as Markdown (money is escaped: "$" would start LaTeX)."""
    a, c = q.assumptions, q.currency

    def m(value: float | None) -> str:
        return md_escape(money(value, c))

    return (
        f"Retail median {m(q.retail_median)} → ex-VAT {m(q.retail_ex_vat)} ({rate(a.get('vat_rate', 0))} VAT) "
        f"→ ÷ retailer markup {a.get('retailer_markup', 0):g} → ÷ (1 + {rate(a.get('freight_ins_pct', 0))} freight "
        f"+ {rate(a.get('duty_pct', 0))} duty) = **{m(q.fob_retailer)}**; "
        f"÷ importer markup {a.get('importer_markup', 0):g} = **{m(q.fob_importer)}**."
    )


def market_name(row: MarketRow) -> str:
    return f"{row.label} (deep dive)" if row.is_home else row.label


def best_other_market(brief: Brief) -> MarketRow | None:
    """The best-ranked market other than the deep dive that has a quote (``brief.markets`` is ranked)."""
    return next((m for m in brief.markets if not m.is_home and m.quote is not None), None)


def market_evidence_ids(row: MarketRow) -> list[str]:
    ids = [eid for comp in (row.score.components.values() if row.score else []) for eid in comp.evidence_ids]
    return list(dict.fromkeys(ids + row.evidence_ids))


_HIRING_PREFIX = re.compile(r"^hiring an?\s+", re.I)


def hiring_role(buyer: BuyerCandidate) -> str:
    """The buyer's hiring signal without the "Hiring a " prefix, e.g. "Lighting Buyer (5 days ago)"; "" if none."""
    signal = next((s for s in buyer.signals if s.kind == "hiring"), None)
    return _HIRING_PREFIX.sub("", signal.detail) if signal else ""


def hiring_companies(jobs: list[JobPosting]) -> list[str]:
    """Distinct employers advertising buying roles, recruitment agencies excluded, in first-seen order."""
    seen: dict[str, str] = {}
    for j in jobs:
        if not j.is_recruiter:
            seen.setdefault(j.company.strip().casefold(), j.company.strip())
    return list(seen.values())


def sorted_jobs(jobs: list[JobPosting]) -> list[JobPosting]:
    """Named employers before agencies; newest first; undated ads last in each group."""
    return sorted(jobs, key=lambda j: (j.is_recruiter, j.posted_days is None, j.posted_days or 0.0))


def job_flags(job: JobPosting) -> list[str]:
    flags = [("mentions India", job.mentions_india), ("overseas sourcing", job.mentions_overseas),
             ("via agency", job.is_recruiter)]
    return [label for label, on in flags if on]


def evidence_links(ids: list[str], ev_by_id: dict[str, Evidence], limit: int = 8) -> str:
    """A numbered Markdown list of evidence links (like the summary's Sources); the rest are counted."""
    found = [ev_by_id[eid] for eid in dict.fromkeys(ids) if eid in ev_by_id]
    lines = [f"{i}. {md_link(e.title, e.url)} · {ENGINE_LABELS.get(e.engine, e.engine)}"
             for i, e in enumerate(found[:limit], 1)]
    if len(found) > limit:
        lines.append(f"\n…and {len(found) - limit} more in the Evidence tab.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- charts

def palette() -> dict[str, str]:
    try:
        dark = st.context.theme.type == "dark"
    except Exception:
        dark = False
    if dark:
        return {"accent": "#3987e5", "track": "#0d366b", "ink2": "#c3c2b7", "muted": "#898781", "surface": "#0e1117"}
    return {"accent": "#2a78d6", "track": "#cde2fb", "ink2": "#52514e", "muted": "#898781", "surface": "#ffffff"}


def score_chart(brief: Brief) -> alt.LayerChart:
    p = palette()
    rows = [
        {"component": SCORE_LABELS.get(k, k), "points": c.points, "max": c.max_points,
         "label": f"{c.points:g} / {c.max_points:g}", "detail": c.detail}
        for k, c in brief.market_score.components.items()
    ]
    df = pd.DataFrame(rows)
    top = float(df["max"].max()) * 1.35
    base = alt.Chart(df).encode(
        y=alt.Y("component:N", sort=list(df["component"]), title=None,
                axis=alt.Axis(ticks=False, domain=False, labelLimit=220)),
    )
    x = alt.X("max:Q", scale=alt.Scale(domain=[0, top]), axis=None)
    tooltip = [alt.Tooltip("component:N", title="Component"), alt.Tooltip("label:N", title="Points"),
               alt.Tooltip("detail:N", title="Why")]
    track = base.mark_bar(size=14, cornerRadiusEnd=4, color=p["track"]).encode(x=x, tooltip=tooltip)
    fill = base.mark_bar(size=14, cornerRadiusEnd=4, color=p["accent"]).encode(
        x=alt.X("points:Q", scale=alt.Scale(domain=[0, top]), axis=None), tooltip=tooltip)
    text = base.mark_text(align="left", dx=6, color=p["ink2"]).encode(x=x, text="label:N")
    return (track + fill + text).properties(height=36 * len(df))


def demand_chart(timeline: list[tuple[str, int]]) -> alt.LayerChart:
    p = palette()
    df = pd.DataFrame(timeline, columns=["date", "interest"])
    df["date"] = pd.to_datetime(df["date"])
    base = alt.Chart(df).encode(
        x=alt.X("date:T", title=None, axis=alt.Axis(format="%Y", tickCount="year", grid=False)),
        y=alt.Y("interest:Q", title="Search interest (0–100)", scale=alt.Scale(domain=[0, 100])),
    )
    hover = alt.selection_point(fields=["date"], nearest=True, on="pointerover", clear="pointerout", empty=False)
    area = base.mark_area(color=p["accent"], opacity=0.1)
    line = base.mark_line(color=p["accent"], strokeWidth=2, strokeCap="round", strokeJoin="round")
    rule = base.mark_rule(color=p["muted"]).encode(
        opacity=alt.condition(hover, alt.value(0.8), alt.value(0)),
        tooltip=[alt.Tooltip("date:T", title="Week of", format="%d %b %Y"), alt.Tooltip("interest:Q", title="Interest")],
    ).add_params(hover)
    dot = base.mark_circle(size=70, color=p["accent"], stroke=p["surface"], strokeWidth=2).encode(
        opacity=alt.condition(hover, alt.value(1), alt.value(0)))
    return (area + line + rule + dot).properties(height=240)


def price_chart(brief: Brief) -> alt.LayerChart:
    p = palette()
    ladder = brief.ladder
    sym = symbol(ladder.currency)
    rows = [
        {"channel": CHANNEL_LABELS.get(x.channel, x.channel), "price": x.price, "title": x.title,
         "seller": x.merchant or x.brand or "", "jitter": (i * 0.618) % 1 - 0.5}
        for i, x in enumerate(brief.listings)
        if x.price is not None and x.currency == ladder.currency
    ]
    df = pd.DataFrame(rows)
    stats = pd.DataFrame([{"p25": ladder.p25, "p50": ladder.p50, "p75": ladder.p75}])
    x_axis = alt.Axis(labelExpr=f"'{sym}' + datum.label", grid=True)
    band = alt.Chart(stats).mark_rect(color=p["accent"], opacity=0.12).encode(
        x=alt.X("p25:Q", axis=x_axis, title=f"Retail price ({ladder.currency})"), x2="p75:Q",
        tooltip=[alt.Tooltip("p25:Q", title="P25", format=",.2f"), alt.Tooltip("p75:Q", title="P75", format=",.2f")],
    )
    median = alt.Chart(stats).mark_rule(color=p["accent"], strokeWidth=2).encode(
        x="p50:Q", tooltip=[alt.Tooltip("p50:Q", title="Median", format=",.2f")])
    dots = alt.Chart(df).mark_circle(size=80, color=p["accent"], opacity=0.85, stroke=p["surface"], strokeWidth=1.5).encode(
        x=alt.X("price:Q", axis=x_axis, title=f"Retail price ({ladder.currency})", scale=alt.Scale(zero=False)),
        y=alt.Y("channel:N", title=None, axis=alt.Axis(ticks=False, domain=False)),
        yOffset=alt.YOffset("jitter:Q", scale=alt.Scale(domain=[-0.7, 0.7])),
        tooltip=[alt.Tooltip("title:N", title="Listing"), alt.Tooltip("seller:N", title="Seller"),
                 alt.Tooltip("price:Q", title="Price", format=",.2f"), alt.Tooltip("channel:N", title="Channel")],
    )
    return (band + median + dots).properties(height=70 * max(df["channel"].nunique(), 1) + 40)


def engine_chart(evidence: list[Evidence]) -> alt.LayerChart:
    p = palette()
    counts = (
        pd.Series([ENGINE_LABELS.get(e.engine, e.engine) for e in evidence], name="engine")
        .value_counts().rename_axis("engine").reset_index(name="results")
    )
    bars = alt.Chart(counts).mark_bar(size=16, cornerRadiusEnd=4, color=p["accent"]).encode(
        x=alt.X("results:Q", title="Evidence items", axis=alt.Axis(tickMinStep=1)),
        y=alt.Y("engine:N", sort="-x", title=None, axis=alt.Axis(ticks=False, domain=False, labelLimit=220)),
        tooltip=[alt.Tooltip("engine:N", title="Engine"), alt.Tooltip("results:Q", title="Evidence items")],
    )
    labels = bars.mark_text(align="left", dx=4, color=p["ink2"]).encode(text="results:Q")
    return (bars + labels).properties(title=f"{len(counts)} SerpApi engines used", height=30 * len(counts) + 20)


def market_margin_chart(markets: list[MarketRow]) -> alt.LayerChart:
    """Margin at the retailer FOB price by market, highest first. Rows without a margin are skipped."""
    p = palette()
    rows = [
        {"market": market_name(m), "margin": m.quote.margin_retailer,
         "verdict": VERDICTS.get(m.quote.verdict, VERDICTS["unknown"])[0], "fob": fob_range(m.quote),
         "duty": rate(m.duty_pct)}
        for m in markets
        if m.quote is not None and m.quote.margin_retailer is not None
    ]
    df = pd.DataFrame(rows).sort_values("margin", ascending=False, kind="stable")
    df["label"] = [f"{pct(v)} · {verdict}" for v, verdict in zip(df["margin"], df["verdict"])]
    df["label_at"] = df["margin"].clip(lower=0)  # labels of negative bars sit just right of zero
    scale = alt.Scale(domain=[min(0.0, float(df["margin"].min()) * 1.2), max(0.1, float(df["margin"].max()) * 1.35)])

    def x(field: str) -> alt.X:
        # Identical axis on every layer: a layer with axis=None would disable the shared axis.
        return alt.X(field, scale=scale, title="Your margin at the retailer FOB price",
                     axis=alt.Axis(format=".0%", tickCount=5))

    base = alt.Chart(df).encode(
        y=alt.Y("market:N", sort=list(df["market"]), title=None,
                axis=alt.Axis(ticks=False, domain=False, labelLimit=220)),
        tooltip=[alt.Tooltip("market:N", title="Market"), alt.Tooltip("margin:Q", title="Margin", format=".0%"),
                 alt.Tooltip("verdict:N", title="Verdict"), alt.Tooltip("fob:N", title="FOB range"),
                 alt.Tooltip("duty:N", title="Duty")],
    )
    bars = base.mark_bar(size=16, cornerRadiusEnd=4, color=p["accent"]).encode(x=x("margin:Q"))
    labels = base.mark_text(align="left", dx=4, color=p["ink2"]).encode(x=x("label_at:Q"), text="label:N")
    zero = alt.Chart(pd.DataFrame({"zero": [0.0]})).mark_rule(color=p["muted"]).encode(x=x("zero:Q"))
    return (bars + labels + zero).properties(height=36 * len(df) + 70)  # + room for the axis and its title


# --------------------------------------------------------------------------- sidebar

@dataclass
class Settings:
    demo_mode: bool
    budget: int
    overrides: dict[str, float]
    meter: Any
    compare_markets: bool = True


def draw_meter(slot: Any, *, budget: int, credits: int, searches: int | None = None, demo: bool = False) -> None:
    with slot.container():
        if demo and searches is not None:
            st.progress(min(searches / budget, 1.0), text=f"{searches} recorded searches replayed")
            st.caption("Demo Mode spends 0 credits. A live run costs 1 credit per search that isn't cached.")
        else:
            st.progress(min(credits / budget, 1.0), text=f"{credits} / {budget} credits this run")
            if searches is not None:
                st.caption(f"{searches} searches · {searches - credits} served free from cache")


def fetch_account(serp: SerpClient | None = None) -> dict[str, Any]:
    try:
        if serp is not None:
            data = serp.account() or {}
        else:
            with SerpClient(mode="live", cache_path=CACHE_DIR / "serp.sqlite") as client:
                data = client.account() or {}
    except Exception as exc:
        log.warning("account check failed: %s", safe_text(str(exc)))
        return {}
    return {k: data.get(k) for k in ("plan_searches_left", "this_month_usage", "plan_name")}


def render_account() -> None:
    refresh = st.sidebar.button("Refresh plan usage", icon=":material/refresh:", type="tertiary")
    if refresh or "account" not in st.session_state:
        st.session_state["account"] = fetch_account()
    left = st.session_state["account"].get("plan_searches_left")
    st.sidebar.caption(f"SerpApi plan searches left: **{left}**" if left is not None else "SerpApi plan usage: unavailable")


def render_assumptions() -> dict[str, float]:
    m = market(MARKET)
    with st.sidebar.expander("Assumptions", icon=":material/tune:"):
        st.caption("Editable defaults, not facts. They turn UK retail prices into your FOB quote range.")
        vat = st.number_input("UK VAT %", 0.0, 50.0, round(m["vat_rate"] * 100, 2), 0.5, format="%.1f")
        retailer = st.number_input("Retailer markup ×", 1.0, 6.0, float(m["markups"]["retailer"]), 0.1, format="%.1f")
        importer = st.number_input("Importer markup ×", 1.0, 6.0, float(m["markups"]["importer"]), 0.1, format="%.1f")
        freight = st.number_input("Freight & insurance, % of FOB", 0.0, 80.0, round(m["freight_ins_pct"] * 100, 2), 1.0,
                                  format="%.1f")
        duty = st.number_input("Import duty %", 0.0, 40.0, round(m["duty_india"] * 100, 2), 0.5, format="%.1f",
                               help="0% for India → UK under CETA, with valid proof of origin.")
        st.caption(f"Market data last verified {m.get('last_verified', 'n/a')}.")
    return {
        "vat_rate": vat / 100,
        "retailer_markup": retailer,
        "importer_markup": importer,
        "freight_ins_pct": freight / 100,
        "duty_pct": duty / 100,
    }


def render_sidebar() -> Settings:
    sb = st.sidebar
    has_key = keys_available()["SERPAPI_API_KEY"]
    sb.header("Settings")
    # On by default even with a key: live searches spend credits only when the user opts in.
    demo = sb.toggle("Demo Mode", value=True, disabled=not has_key,
                     help="Replays recorded SerpApi responses for the demo products. No keys, no credits.")
    if not has_key:
        demo = True
        sb.caption("No SERPAPI_API_KEY found, so Demo Mode is on. Add keys to `.env` for live runs.")
    elif not demo and not keys_available()["ANTHROPIC_API_KEY"]:
        sb.caption("No ANTHROPIC_API_KEY: text steps fall back to simple templates.")
    budget = sb.slider("Credit budget per run", 15, 60, min(max(DEFAULT_BUDGET, 15), 60),
                       help="The agent stops making new searches at this cap. A full UK run uses about 35, "
                            "plus about 9 to compare other markets.")
    sb.selectbox("Market", ["United Kingdom"], disabled=True)
    sb.caption("Deep dive: UK. Other markets are compared in the Markets tab.")
    compare = sb.checkbox("Compare other markets", value=True, key="compare_markets",
                          help="Quick Amazon scan of the US, Germany, UAE and Australia: about 9 credits in live mode, "
                               "free in Demo Mode")
    overrides = render_assumptions()

    sb.subheader("Credits")
    meter = sb.empty()
    state = st.session_state.get("meter")
    if state:
        draw_meter(meter, budget=budget, **state)
    else:
        brief = st.session_state.get("brief")
        draw_meter(meter, budget=budget, credits=brief.credits_used if brief else 0)
    if not demo:
        render_account()
    return Settings(demo_mode=demo, budget=budget, overrides=overrides, meter=meter, compare_markets=compare)


# --------------------------------------------------------------------------- input and run

def render_header() -> None:
    st.title("ExportScout")
    st.markdown(f"#### {ONE_LINER}")
    st.caption("For Moradabad brassware exporters: see the UK market the way a UK shopper sees it, "
               "through live SerpApi search data.")
    with st.expander("Why now", icon=":material/schedule:"):
        st.markdown(why_now_table())


def show_image(path: str | None) -> None:
    if path and Path(path).is_file():
        st.image(path, width=260)
    else:
        with st.container(border=True):
            st.caption("Demo photo not included yet. The recorded searches still replay.")


def demo_inputs() -> RunInputs | None:
    products = load_demo_products()
    if not products:
        st.warning("No demo products found in demo_cache/demo_products.yaml.")
        return None
    labels = {p["id"]: p["label"] for p in products}
    pid = st.selectbox("Demo product", list(labels), format_func=labels.get)
    base: RunInputs = next(p["inputs"] for p in products if p["id"] == pid)
    col_img, col_fields = st.columns([1, 2])
    with col_img:
        show_image(base.image_path)
    with col_fields:
        st.caption(" · ".join(x for x in (base.description, base.material, base.finish) if x))
        c1, c2, c3 = st.columns(3)
        cost = c1.number_input("Unit cost (₹)", min_value=0.0, value=float(base.unit_cost_inr or 0), step=10.0,
                               key=f"cost_{pid}")
        extra = c2.number_input("Extra costs (₹)", min_value=0.0, value=float(base.extra_costs_inr), step=5.0,
                                key=f"extra_{pid}", help="Packing + inland freight to port, per piece.")
        moq = c3.number_input("MOQ (pieces)", min_value=1, value=int(base.moq or 100), step=50, key=f"moq_{pid}")
        st.caption("Demo Mode replays recorded SerpApi responses for this product. Costs change only the maths.")
    return base.model_copy(update={"unit_cost_inr": cost, "extra_costs_inr": extra, "moq": int(moq)})


def save_upload(filename: str, data: bytes) -> Path:
    path = upload_path(filename, data)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)
    return path


def live_inputs() -> RunInputs | None:
    col_img, col_fields = st.columns([1, 2])
    with col_img:
        photo = st.file_uploader("Product photo", type=["jpg", "jpeg", "png", "webp"])
        image_path = save_upload(photo.name, photo.getvalue()) if photo else None
        if image_path:
            st.image(str(image_path), width=260)
    with col_fields:
        description = st.text_input("Description (optional)", placeholder="e.g. brass hurricane lantern")
        c1, c2, c3 = st.columns(3)
        cost = c1.number_input("Unit cost (₹)", min_value=0.0, value=None, step=10.0, placeholder="e.g. 650")
        extra = c2.number_input("Extra costs (₹)", min_value=0.0, value=0.0, step=5.0,
                                help="Packing + inland freight to port, per piece.")
        moq = c3.number_input("MOQ (pieces)", min_value=1, value=200, step=50)
        c4, c5 = st.columns(2)
        material = c4.text_input("Material", value="brass")
        finish = c5.text_input("Finish", placeholder="e.g. antique gold")
    if not image_path and not description.strip():
        return None
    return RunInputs(
        image_path=str(image_path) if image_path else None,
        description=description.strip() or None,
        unit_cost_inr=cost,
        extra_costs_inr=extra or 0.0,
        moq=int(moq),
        material=material.strip() or None,
        finish=finish.strip() or None,
    )


def render_inputs(settings: Settings) -> RunInputs | None:
    st.subheader("Your product")
    inputs = demo_inputs() if settings.demo_mode else live_inputs()
    if not st.button("Scout the UK market", type="primary", icon=":material/travel_explore:"):
        return None
    if inputs is None:
        st.warning("Upload a product photo or type a description first.")
        return None
    return inputs.model_copy(update={"market": MARKET, "assumption_overrides": settings.overrides})


def run(inputs: RunInputs, settings: Settings) -> None:
    serp = None
    error: BaseException | None = None
    brief: Brief | None = None
    with st.status("Scouting the UK market…", expanded=True) as status:
        try:
            serp, llm = make_clients(demo_mode=settings.demo_mode, budget=settings.budget)

            def on_event(event: StepEvent) -> None:
                status.markdown(step_line(event))
                draw_meter(settings.meter, budget=settings.budget, credits=serp.credits_used,
                           searches=serp.credits_used + serp.cache_hits, demo=settings.demo_mode)

            brief = run_scout(inputs, serp=serp, llm=llm, on_event=on_event, compare_markets=settings.compare_markets)
        except Exception as exc:
            error = exc
            status.update(label="Scouting stopped", state="error", expanded=True)
        else:
            status.update(label=f"Brief ready · {brief.credits_used} credits used", state="complete", expanded=False)
    if serp is not None:
        st.session_state["meter"] = {"credits": serp.credits_used, "searches": serp.credits_used + serp.cache_hits,
                                     "demo": settings.demo_mode}
        if not settings.demo_mode:
            st.session_state["account"] = fetch_account(serp)
        serp.close()
    if error is not None:
        st.error(friendly_error(error), icon=":material/error:")
        with st.expander("Technical details"):
            st.code(safe_text("".join(traceback.format_exception(error)), limit=None), language=None)
        return
    st.session_state["brief"] = brief
    st.session_state.pop("sample_brief", None)


def load_sample_brief() -> None:
    raw = os.environ.get("EXPORTSCOUT_SAMPLE_BRIEF")
    if not raw or "brief" in st.session_state:
        return
    path = Path(raw)
    if not path.is_absolute() and not path.exists():
        path = APP_DIR / raw
    try:
        st.session_state["brief"] = Brief.model_validate_json(path.read_text(encoding="utf-8"))
        st.session_state["sample_brief"] = str(path)
    except (OSError, ValueError) as exc:
        st.warning(f"Couldn't load the sample brief from {raw}: {safe_text(str(exc))}")


# --------------------------------------------------------------------------- Brief tab

def render_headline(brief: Brief) -> None:
    verdict = brief.quote.verdict if brief.quote else "unknown"
    label, color, icon = VERDICTS.get(verdict, VERDICTS["unknown"])
    st.badge(label, icon=icon, color=color)
    product = brief.product.product_type if brief.product else "Your product"
    st.markdown(f"### {md_escape(brief.headline or f'{product} → {brief.market_label}')}")
    if brief.product and brief.product.keywords:
        st.caption("UK retail keywords: " + ", ".join(brief.product.keywords))


def render_metrics(brief: Brief) -> None:
    q, score = brief.quote, brief.market_score
    c1, c2, c3, c4 = st.columns([3, 3, 2, 2])
    if q:
        inr = (f"₹{q.fob_importer_inr:,.0f}–₹{q.fob_retailer_inr:,.0f}"
               if q.fob_importer_inr is not None and q.fob_retailer_inr is not None else "—")
        fx = f"At {q.fx_rate:.2f} INR per {q.currency}." if q.fx_rate else "No exchange rate found."
        c1.metric("Quote range (FOB)", f"{money(q.fob_importer, q.currency)}–{money(q.fob_retailer, q.currency)}",
                  help="Low end: selling through an importer. High end: a retailer imports directly.", border=True)
        c2.metric("Quote range (₹)", inr, help=fx, border=True)
        cost = f"₹{q.total_cost_inr:,.0f}" if q.total_cost_inr is not None else "not given"
        c3.metric("Retailer margin", pct(q.margin_retailer),
                  help=f"Your margin at the retailer FOB price, against your cost per piece (unit + extra): {cost}. "
                       f"Via an importer: {pct(q.margin_importer)}.",
                  border=True)
    else:
        c1.metric("Quote range (FOB)", "—", border=True)
        c2.metric("Quote range (₹)", "—", border=True)
        c3.metric("Retailer margin", "—", border=True)
    c4.metric("Opportunity score", f"{score.total:.0f}/100" if score else "—", border=True,
              help="Market Opportunity Score (0–100): demand, price headroom, duty advantage, proven for Indian goods, "
                   "buyer depth and timing.")
    render_signal_metrics(brief)
    if q:
        st.caption(fob_formula(q) + " Markups and freight are editable assumptions (sidebar).")


def render_signal_metrics(brief: Brief) -> None:
    """Second metrics row: the best market after the deep dive, and companies hiring buyers right now."""
    if not (brief.markets or brief.jobs):
        return
    c1, c2 = st.columns([6, 4])  # lines up under the 3+3 and 2+2 columns above
    best = best_other_market(brief)
    if best is not None:
        bq = best.quote
        value = best.label + (f" · {pct(bq.margin_retailer)} margin" if bq.margin_retailer is not None else "")
        fit = f"Market Fit {best.score.total:.0f}/100, " if best.score else ""
        c1.metric("Best other market", value, border=True,
                  help=f"{fit}FOB {md_escape(fob_range(bq))}, duty {rate(best.duty_pct)}. "
                       "A quick Amazon-only scan: see the Markets tab.")
    else:
        c1.metric("Best other market", "—", border=True,
                  help="No other market had enough Amazon prices for a quote." if brief.markets
                  else "Turn on **Compare other markets** in the sidebar to scan the US, Germany, UAE and Australia.")
    companies = hiring_companies(brief.jobs)
    agencies = sum(j.is_recruiter for j in brief.jobs)
    if brief.jobs:
        names = ", ".join(md_escape(c) for c in companies[:5]) + (", …" if len(companies) > 5 else "")
        help_text = (f"Advertising buying or sourcing roles on Google Jobs: {names or 'none named'}"
                     + (f" (plus {plural(agencies, 'recruitment agency ad')})" if agencies else "")
                     + ". See the Buyers tab.")
        c2.metric("Companies hiring buyers", len(companies), border=True, help=help_text)
    else:
        c2.metric("Companies hiring buyers", "—", border=True, help="No buying-role job ads in this brief.")


def render_summary(brief: Brief) -> None:
    if not brief.summary_md:
        return
    st.markdown(render_citations(brief.summary_md, brief.evidence))
    sources = cited_evidence(brief.summary_md, brief.evidence)
    if sources:
        with st.expander(f"Sources ({len(sources)})"):
            for i, e in enumerate(sources, 1):
                st.markdown(f"{i}. {md_link(e.title, e.url)} · {ENGINE_LABELS.get(e.engine, e.engine)}")


def render_score(brief: Brief) -> None:
    if not brief.market_score:
        return
    st.markdown(f"**Market Opportunity Score: {brief.market_score.total:.0f}/100**")
    st.altair_chart(score_chart(brief), width="stretch")
    with st.expander("Score details"):
        st.dataframe(
            pd.DataFrame([
                {"Component": SCORE_LABELS.get(k, k), "Points": f"{c.points:g} / {c.max_points:g}", "Why": c.detail}
                for k, c in brief.market_score.components.items()
            ]),
            hide_index=True, width="stretch",
        )


def render_demand(d: DemandCard | None) -> None:
    st.subheader("Demand and timing")
    if d is None:
        st.info("No demand data in this brief.")
        return
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Interest, last 12 months", f"{d.mean_12m:.0f}/100" if d.mean_12m is not None else "—", border=True)
    c2.metric("Year on year", pct(d.yoy_change, signed=True), border=True)
    c3.metric("Peak months", month_span(d.peak_months), border=True)
    window = "—" if d.months_to_window is None else ("now" if d.months_to_window <= 0 else f"in {d.months_to_window} mo")
    c4.metric("Next buying window", window, border=True)
    if d.is_proxy:
        st.caption(f"“{d.term}” is a broader proxy term: the exact phrase had too little search volume.")
    if d.timeline:
        st.markdown(f"**Google Trends interest in the UK: “{md_escape(d.term)}”**")
        st.altair_chart(demand_chart(d.timeline), width="stretch")
    if d.buying_window_note:
        st.info(d.buying_window_note, icon=":material/event:")
    for label, items in (("Top regions", d.top_regions), ("Related searches", d.related_queries),
                         ("How UK shoppers type it", d.autocomplete)):
        if items:
            st.caption(f"{label}: " + ", ".join(items[:6]))


def render_prices(brief: Brief) -> None:
    st.subheader("UK retail prices")
    ladder = brief.ladder
    if ladder is None or not any(x.price is not None for x in brief.listings):
        st.info("Not enough priced listings for a price ladder.")
        return
    c = ladder.currency
    st.caption(f"{ladder.n} priced listings · median {money(ladder.p50, c)} · middle half "
               f"{money(ladder.p25, c)}–{money(ladder.p75, c)} · range {money(ladder.min, c)}–{money(ladder.max, c)}")
    st.altair_chart(price_chart(brief), width="stretch")
    st.caption("Each dot is one listing. Shaded band: the middle 50% of prices (P25–P75). Line: the median.")
    if ladder.by_channel:
        with st.expander("Prices by channel"):
            st.dataframe(
                pd.DataFrame([
                    {"Channel": CHANNEL_LABELS.get(k, k), "Listings": s.n, "P25": money(s.p25, c),
                     "Median": money(s.p50, c), "P75": money(s.p75, c)}
                    for k, s in ladder.by_channel.items()
                ]),
                hide_index=True, width="stretch",
            )


def render_origin(brief: Brief) -> None:
    st.subheader("Where competitors' products come from")
    o = brief.origin
    if o is None or (o.checked == 0 and o.ebay_total == 0):
        st.info("No country-of-origin data in this brief.")
        return
    r1, r2 = st.columns(2), st.columns(2)
    r1[0].metric("Made in India", o.india, border=True)
    r1[1].metric("Made in China", o.china, border=True)
    r2[0].metric("Other countries", o.other, border=True)
    r2[1].metric("Unknown", o.unknown, border=True)
    notes = [f"Amazon products checked: {o.checked}."]
    if o.india_share is not None:
        notes.append(f"India's share of known origins: {pct(o.india_share)}.")
    if o.ebay_total:
        notes.append(f"eBay listings located in India: {o.ebay_from_india} of {o.ebay_total}.")
    st.caption(" ".join(notes))


def render_themes(brief: Brief) -> None:
    st.subheader("What UK customers complain about")
    complaints = [t for t in brief.themes if t.kind == "complaint"]
    praise = [t for t in brief.themes if t.kind == "praise"]
    if not complaints:
        st.info("No review complaints found.")
    for t in complaints:
        with st.container(border=True):
            st.markdown(f"**{md_escape(t.label)}** · {t.share:.0%} of reviews ({t.count})")
            if t.quotes:
                st.caption(f"“{md_escape(t.quotes[0])}”")
            if t.fix:
                st.markdown(f":material/build: {md_escape(t.fix)}")
    if praise:
        st.caption("What they like: " + ", ".join(f"{t.label} ({t.share:.0%})" for t in praise))


def render_downloads(brief: Brief) -> None:
    md = export_or_none("brief_markdown", brief)
    st.download_button(
        "Download brief (Markdown)", data=md or "", file_name="exportscout_brief.md", mime="text/markdown",
        disabled=md is None, icon=":material/download:", on_click="ignore",
    )
    if md is None:
        st.caption("Markdown export not available yet.")


def render_brief_tab(brief: Brief) -> None:
    render_headline(brief)
    render_metrics(brief)
    left, right = st.columns([3, 2], gap="large")
    with left:
        render_summary(brief)
    with right:
        render_score(brief)
    render_demand(brief.demand)
    render_prices(brief)
    left, right = st.columns(2, gap="large")
    with left:
        render_origin(brief)
    with right:
        render_themes(brief)
    for w in brief.warnings:
        st.warning(w, icon=":material/warning:")
    render_downloads(brief)
    if brief.steps:
        with st.expander(f"Agent step log ({len(brief.steps)} steps, {brief.credits_used} credits)"):
            for event in brief.steps:
                st.markdown(step_line(event))


# --------------------------------------------------------------------------- Markets tab

def market_table(markets: list[MarketRow]) -> pd.DataFrame:
    rows = []
    for i, m in enumerate(markets, 1):
        q = m.quote
        retail = q.retail_median if q else (m.ladder.p50 if m.ladder else None)
        notes = ["Thin data" if m.thin_data else "", "No quote" if q is None else ""]
        rows.append({
            "rank": i,
            "market": market_name(m),
            "duty": round(m.duty_pct * 100, 1),
            "retail": money(retail, m.currency) if retail is not None else "n/a",
            "fob": fob_range(q) if q else "n/a",
            "fob_inr": round(q.fob_retailer_inr) if q and q.fob_retailer_inr is not None else None,
            "margin": round(q.margin_retailer * 100) if q and q.margin_retailer is not None else None,
            "verdict": VERDICTS.get(q.verdict, VERDICTS["unknown"])[0] if q else "n/a",
            "fit": m.score.total if m.score else None,
            "listings": m.listings_n,
            "trends": m.trends_interest,
            "reviews": m.review_depth,
            "note": "; ".join(n for n in notes if n),
        })
    return pd.DataFrame(rows)


def render_market_detail(m: MarketRow, ev_by_id: dict[str, Evidence]) -> None:
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown("**Market Fit breakdown**")
        if m.score and m.score.components:
            st.dataframe(
                pd.DataFrame([
                    {"Component": MARKET_FIT_LABELS.get(k, k), "Points": f"{c.points:g} / {c.max_points:g}", "Why": c.detail}
                    for k, c in m.score.components.items()
                ]),
                hide_index=True, width="stretch",
            )
        else:
            st.caption("No Market Fit breakdown.")
        if m.quote:
            st.caption(fob_formula(m.quote) + " Markups and freight are assumptions.")
        elif m.ladder:
            st.caption(f"{m.ladder.n} priced listings, median {md_escape(money(m.ladder.p50, m.ladder.currency))}; "
                       "no quote range.")
        else:
            st.caption("Quote range: n/a (too few priced listings).")
    with right:
        st.markdown("**Duty on Indian goods**")
        duty = f"**{rate(m.duty_pct)}**" + (f" · {md_escape(m.duty_detail)}" if m.duty_detail else "")
        lines = [duty + (f" · HS {md_escape(m.hs_code)}" if m.hs_code else "")]
        if m.duty_note:
            lines.append(md_escape(m.duty_note))
        if m.duty_sources:
            lines.append("Sources: " + ", ".join(md_link(short_url(u), u) for u in m.duty_sources))
        lines.append((f"Verified {md_escape(m.last_verified)}. " if m.last_verified else "") + "Verify before quoting.")
        st.markdown("  \n".join(lines))
        st.markdown("**Search**")
        facts = [f"Amazon search: “{md_escape(m.keyword)}”",
                 f"FX: 1 {m.currency} = ₹{m.fx_rate:.2f}" if m.fx_rate else "FX: no exchange rate found"]
        if m.ladder:
            c = m.ladder.currency
            facts.append(md_escape(f"{m.ladder.n} priced listings · median {money(m.ladder.p50, c)} · middle half "
                                   f"{money(m.ladder.p25, c)}–{money(m.ladder.p75, c)}"))
        else:
            facts.append(f"{m.listings_n} priced listings")
        demand = [f"Trends interest {m.trends_interest}/100" if m.trends_interest is not None else "",
                  f"{m.review_depth:,} reviews on the top results" if m.review_depth is not None else "",
                  f"{m.bought_last_month:,}+ bought last month" if m.bought_last_month else ""]
        if any(demand):
            facts.append(" · ".join(d for d in demand if d))
        st.markdown("  \n".join(facts))
        if m.thin_data:
            st.caption(":material/warning: Thin data: too few priced listings to trust the median.")
    ids = market_evidence_ids(m)
    if any(eid in ev_by_id for eid in ids):
        st.markdown("**Evidence**")
        st.markdown(evidence_links(ids, ev_by_id))


def render_market_assumptions(markets: list[MarketRow]) -> None:
    st.subheader("Assumptions by market", anchor="market-assumptions")

    def times(a: dict[str, float], key: str) -> str:
        return f"{a[key]:g}×" if key in a else "n/a"

    rows = []
    for m in markets:
        a = m.quote.assumptions if m.quote else {}
        rows.append({
            "Market": market_name(m),
            "VAT / sales tax": rate(a["vat_rate"]) if "vat_rate" in a else "n/a",
            "Retailer markup": times(a, "retailer_markup"),
            "Importer markup": times(a, "importer_markup"),
            "Freight & insurance": rate(a["freight_ins_pct"]) if "freight_ins_pct" in a else "n/a",
            "Duty": rate(m.duty_pct),
            "₹ per unit": f"{m.fx_rate:.2f}" if m.fx_rate else "n/a",
            "Verified": m.last_verified or "n/a",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("Markups and freight are assumptions, not facts. Duty comes from each market's tariff schedule "
               "(sources in each market's details). Verify duty rates and rules of origin before quoting.")


def render_markets(brief: Brief) -> None:
    st.subheader("Where to sell next")
    st.caption(
        "A quick Amazon-only scan of each market, not a deep dive: one Amazon search per market, worked back to a FOB "
        "range with that market's VAT, markups, freight and duty on Indian goods "
        "([assumptions](#market-assumptions)). The UK row is worked out the same way from the Amazon UK listings, "
        "so it can differ from the full quote on the Brief tab."
    )
    if not brief.markets:
        st.info("No market comparison in this brief. Turn on **Compare other markets** in the sidebar and scout again.",
                icon=":material/info:")
        return
    best = best_other_market(brief)
    if best is not None:
        q = best.quote
        facts = [f"{pct(q.margin_retailer)} margin" if q.margin_retailer is not None else "",
                 f"FOB {md_escape(fob_range(q))}", f"duty {rate(best.duty_pct)}"]
        st.markdown(f"**Best other market: {md_escape(best.label)}** · " + " · ".join(f for f in facts if f))
    st.dataframe(
        market_table(brief.markets), hide_index=True, width="stretch",
        column_config={
            "rank": st.column_config.NumberColumn("#", pinned=True),
            "market": st.column_config.TextColumn("Market", pinned=True),
            "duty": st.column_config.NumberColumn("Duty", format="%g%%",
                                                  help="Import duty on Indian goods used in the FOB maths"),
            "retail": st.column_config.TextColumn("Retail median", help="Median Amazon price, local currency"),
            "fob": st.column_config.TextColumn("FOB range", help="Via an importer – direct to a retailer, local currency"),
            "fob_inr": st.column_config.NumberColumn("FOB ceiling ₹", format="₹%,d",
                                                     help="The most you can quote (direct to a retailer), in rupees"),
            "margin": st.column_config.NumberColumn("Margin", format="%d%%",
                                                    help="Your margin at the retailer FOB price, against your cost"),
            "verdict": st.column_config.TextColumn("Verdict"),
            "fit": st.column_config.ProgressColumn(
                "Market Fit", min_value=0, max_value=100, format="%.0f", width="small",
                help="0–100: margin 40, demand 30, trade access 20, data depth 10"),
            "listings": st.column_config.NumberColumn("Listings", format="%d", help="Priced Amazon listings"),
            "trends": st.column_config.NumberColumn("Trends", format="%d",
                                                    help="Google Trends interest in this country (0–100)"),
            "reviews": st.column_config.NumberColumn("Amazon reviews", format="%,d",
                                                     help="Reviews on the top Amazon results: a demand proxy"),
            "note": st.column_config.TextColumn("Note", help="Thin data: too few priced listings to trust the median"),
        },
    )
    chartable = [m for m in brief.markets if m.quote is not None and m.quote.margin_retailer is not None]
    if chartable:
        st.markdown("**Your margin at the retailer FOB price, by market**")
        st.altair_chart(market_margin_chart(chartable), width="stretch")
    else:
        st.caption("Add your unit cost to compare margins across markets.")
    ev_by_id = {e.id: e for e in brief.evidence}
    for i, m in enumerate(brief.markets, 1):
        fit = f" · Market Fit {m.score.total:.0f}/100" if m.score else ""
        with st.expander(f"{i}. {market_name(m)}{fit}"):
            render_market_detail(m, ev_by_id)
    render_market_assumptions(brief.markets)


# --------------------------------------------------------------------------- other tabs

def evidence_card(e: Evidence | None, detail: str | None = None) -> None:
    if e is None:
        return
    with st.container(border=True):
        if detail:
            st.markdown(md_escape(detail))
        st.markdown(f"{md_link(e.title, e.url)}")
        st.caption(f"{ENGINE_LABELS.get(e.engine, e.engine)} · fetched {fmt_ts(e.fetched_at)}")


def render_buyer_detail(b: BuyerCandidate, ev_by_id: dict[str, Evidence]) -> None:
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown("**Fit score breakdown**")
        if b.fit:
            st.dataframe(
                pd.DataFrame([
                    {"Component": FIT_LABELS.get(k, k), "Points": f"{c.points:g} / {c.max_points:g}", "Detail": c.detail}
                    for k, c in b.fit.items()
                ]),
                hide_index=True, width="stretch",
            )
        else:
            st.caption("No fit breakdown.")
    with right:
        st.markdown("**Contact (public business information)**")
        lines = [
            f"Website: {md_link(b.website, b.website)}" if b.website else None,
            f"Phone: {b.phone}" if b.phone else None,
            f"Address: {md_escape(b.address)}" if b.address else None,
            f"Type: {KIND_LABELS.get(b.kind, b.kind)}" + (f" · {b.city}" if b.city else ""),
            "Seen in: " + ", ".join(ENGINE_LABELS.get(s, s) for s in b.sources) if b.sources else None,
        ]
        st.markdown("  \n".join(x for x in lines if x))
    st.markdown("**Evidence**")
    details = {s.evidence_id: s.detail for s in b.signals}
    ids = buyer_evidence_ids(b)
    cols = st.columns(2)
    for i, eid in enumerate(ids):
        with cols[i % 2]:
            evidence_card(ev_by_id.get(eid), details.get(eid))


def render_jobs(brief: Brief) -> None:
    """Card: companies advertising buying roles right now (company-level data only)."""
    if not brief.jobs:
        return
    companies = hiring_companies(brief.jobs)
    agency_ads = sum(j.is_recruiter for j in brief.jobs)
    summary = (f"{plural(len(brief.jobs) - agency_ads, 'buying or sourcing role')} advertised by "
               f"{plural(len(companies), 'company', 'companies')} on Google Jobs")
    if agency_ads:
        summary += f", plus {plural(agency_ads, 'recruitment agency ad')} (listed last)"
    with st.container(border=True):
        st.markdown("**UK companies hiring buyers now**")
        st.caption(f"{summary}. A company hiring a buyer is often building or refreshing its range. "
                   "Company-level data only; ExportScout never contacts anyone.")
        st.dataframe(
            pd.DataFrame([
                {"company": j.company, "role": j.title, "where": j.location, "posted": j.posted,
                 "flags": job_flags(j), "ad": j.link}
                for j in sorted_jobs(brief.jobs)
            ]),
            hide_index=True, width="stretch",
            column_config={
                "company": st.column_config.TextColumn("Company"),
                "role": st.column_config.TextColumn("Role", width="medium"),
                "where": st.column_config.TextColumn("Where"),
                "posted": st.column_config.TextColumn("Posted"),
                "flags": st.column_config.ListColumn("Signals"),
                "ad": st.column_config.LinkColumn("Ad", display_text="View ad"),
            },
        )


def render_buyers_tab(brief: Brief) -> None:
    if not brief.buyers:
        st.info("No buyers in this brief. If the FOB ceiling is below your cost, the agent skips buyer search.")
        render_jobs(brief)
        return
    df = pd.DataFrame([
        {"rank": i, "name": b.name, "kind": KIND_LABELS.get(b.kind, b.kind), "city": b.city, "fit": b.fit_score,
         "hiring": hiring_role(b), "website": b.website, "phone": b.phone, "why": b.why}
        for i, b in enumerate(brief.buyers, 1)
    ])
    st.markdown(f"**{len(brief.buyers)} UK buyers, ranked by fit**")
    st.dataframe(
        df, hide_index=True, width="stretch",
        column_config={
            "rank": st.column_config.NumberColumn("#", width="small"),
            "name": st.column_config.TextColumn("Name"),
            "kind": st.column_config.TextColumn("Type"),
            "city": st.column_config.TextColumn("City"),
            "fit": st.column_config.ProgressColumn("Fit", min_value=0, max_value=100, format="%.0f"),
            "hiring": st.column_config.TextColumn("Hiring", help="A buying role they are advertising now (Google Jobs)"),
            "website": st.column_config.LinkColumn("Website", display_text=r"https?://(?:www\.)?([^/]+)"),
            "phone": st.column_config.TextColumn("Phone"),
            "why": st.column_config.TextColumn("Why them", width="large"),
        },
    )
    csv = export_or_none("buyers_csv", brief)
    st.download_button("Download buyers (CSV)", data=csv or "", file_name="exportscout_buyers.csv", mime="text/csv",
                       disabled=csv is None, icon=":material/download:", on_click="ignore")
    if csv is None:
        st.caption("CSV export not available yet.")
    render_jobs(brief)
    ev_by_id = {e.id: e for e in brief.evidence}
    for i, b in enumerate(brief.buyers, 1):
        fit = f" · fit {b.fit_score:.0f}/100" if b.fit_score is not None else ""
        with st.expander(f"{i}. {b.name}{fit}", expanded=i == 1):
            render_buyer_detail(b, ev_by_id)


def render_pitch_tab(brief: Brief) -> None:
    left, right = st.columns([3, 2], gap="large")
    with left:
        if not brief.pitches:
            st.info("No pitch emails in this brief.")
        else:
            names = [p.buyer_name for p in brief.pitches]
            idx = st.selectbox("Buyer", range(len(names)), format_func=lambda i: names[i], key="pitch_buyer")
            pitch = brief.pitches[idx]
            subject = st.text_input("Subject", pitch.subject, key=f"pitch_subject_{idx}")
            body = st.text_area("Email", pitch.body, height=340, key=f"pitch_body_{idx}")
            st.caption("Edit it, then send it from your own email. ExportScout never contacts anyone for you.")
            with st.expander("Copy as plain text"):
                st.code(f"Subject: {subject}\n\n{body}", language=None, wrap_lines=True)
            ev_by_id = {e.id: e for e in brief.evidence}
            cited = [ev_by_id[eid] for eid in pitch.evidence_ids if eid in ev_by_id]
            if cited:
                st.markdown("**Based on**")
                for e in cited:
                    st.markdown(f"- {md_link(e.title, e.url)} · {ENGINE_LABELS.get(e.engine, e.engine)}")
    with right:
        st.markdown("**Spec improvements to offer**")
        specs = brief.spec_improvements or [t.fix for t in brief.themes if t.fix]
        st.markdown("\n".join(f"- {md_escape(s)}" for s in specs) if specs else "No spec changes suggested.")
        st.markdown("**Compliance notes**")
        try:
            notes = list(category(brief.inputs.category).get("compliance_notes", []))
        except KeyError:
            notes = []
        if not any("proof of origin" in n for n in notes):
            notes.append("Duty is 0% from India to the UK under CETA only with valid proof of origin. "
                         "Verify duty rates and rules of origin before quoting.")
        st.markdown("\n".join(f"- {md_escape(n)}" for n in notes))


def render_evidence_tab(brief: Brief) -> None:
    if not brief.evidence:
        st.info("No evidence recorded.")
        return
    st.altair_chart(engine_chart(brief.evidence), width="stretch")
    df = pd.DataFrame([e.model_dump(include={"id", "engine", "title", "url", "fetched_at", "query"}) for e in brief.evidence])
    df = df[["id", "engine", "title", "url", "fetched_at", "query"]]
    engines = sorted(df["engine"].unique())
    chosen = st.multiselect("Filter by engine", engines, format_func=lambda e: ENGINE_LABELS.get(e, e),
                            placeholder="All engines", key="evidence_engines")
    shown = df[df["engine"].isin(chosen)] if chosen else df
    st.dataframe(
        shown, hide_index=True, width="stretch",
        column_config={
            "id": st.column_config.TextColumn("ID"),
            "engine": st.column_config.TextColumn("Engine"),
            "title": st.column_config.TextColumn("Title", width="large"),
            "url": st.column_config.LinkColumn("URL"),
            "fetched_at": st.column_config.TextColumn("Fetched at"),
            "query": st.column_config.TextColumn("Query"),
        },
    )
    st.caption(f"{len(shown)} of {len(df)} evidence items. Every number in the brief links back to one of these.")


def render_tabs(brief: Brief) -> None:
    brief_tab, markets_tab, buyers_tab, pitch_tab, evidence_tab = st.tabs(TABS)
    with brief_tab:
        render_brief_tab(brief)
    with markets_tab:
        render_markets(brief)
    with buyers_tab:
        render_buyers_tab(brief)
    with pitch_tab:
        render_pitch_tab(brief)
    with evidence_tab:
        render_evidence_tab(brief)


# --------------------------------------------------------------------------- page

def main() -> None:
    load_dotenv()
    st.set_page_config(page_title="ExportScout", layout="wide")
    load_sample_brief()
    settings = render_sidebar()
    render_header()
    inputs = render_inputs(settings)
    if inputs is not None:
        run(inputs, settings)
    brief: Brief | None = st.session_state.get("brief")
    st.divider()
    if brief is None:
        st.info("Pick a demo product (or upload a photo) and press **Scout the UK market**.", icon=":material/info:")
    else:
        if st.session_state.get("sample_brief"):
            st.caption(f"Showing a sample brief loaded from `{Path(st.session_state['sample_brief']).name}` "
                       "(EXPORTSCOUT_SAMPLE_BRIEF). All of its data is fictional.")
        render_tabs(brief)
    st.divider()
    st.caption(DISCLAIMER)


if __name__ == "__main__":
    main()
