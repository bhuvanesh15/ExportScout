"""Exports: Markdown brief and buyers CSV."""
from __future__ import annotations

import csv
import io

from exportscout.config import category
from exportscout.llm.tasks import MONTHS, money, pct, sorted_signals
from exportscout.models import Brief, BuyerCandidate

FOOTER = "Estimates from live search data — verify duty rates and rules of origin before quoting."
CSV_COLUMNS = ["rank", "name", "kind", "city", "fit_score", "website", "phone", "domain", "top_signals", "why"]
_ASSUMPTION_LABELS = {
    "vat_rate": "UK VAT",
    "retailer_markup": "Retailer markup",
    "importer_markup": "Importer / wholesaler markup",
    "freight_ins_pct": "Freight, insurance and clearance (share of FOB)",
    "duty_pct": "Import duty from India",
}


def _cell(value: object) -> str:
    if value is None or value == "":
        return "—"
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _table(headers: list[str], rows: list[list[object]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return out


def _link(url: str | None, text: str | None = None) -> str:
    if not url:
        return "—"
    return f"[{_cell(text or url)}]({url.replace(' ', '%20').replace(')', '%29')})"


def _assumption_value(key: str, value: float) -> str:
    if key.endswith("markup"):
        return f"{value:g}×"
    return f"{value * 100:g}%"


def _top_signals(b: BuyerCandidate, n: int = 3) -> str:
    return "; ".join(s.detail for s in sorted_signals(b)[:n])


def _quote_section(brief: Brief) -> list[str]:
    q, lad = brief.quote, brief.ladder
    out = ["## Quote range (estimate)", ""]
    if q is None:
        if lad:
            out.append(f"UK retail median {money(lad.p50, lad.currency)} across {lad.n} listings; no quote range was computed.")
        else:
            out.append("Not enough UK prices were found to estimate a quote range.")
        return out + [""]
    rows: list[list[object]] = [["FOB price", money(q.fob_importer, q.currency), money(q.fob_retailer, q.currency)]]
    if q.fob_importer_inr is not None or q.fob_retailer_inr is not None:
        rows.append(["FOB in ₹", money(q.fob_importer_inr, "INR"), money(q.fob_retailer_inr, "INR")])
    if q.margin_importer is not None or q.margin_retailer is not None:
        rows.append(["Your margin", pct(q.margin_importer), pct(q.margin_retailer)])
    out += _table(["", "Via an importer", "Direct to a retailer"], rows)
    out.append("")
    facts = [f"**Verdict: {q.verdict.replace('_', '-').upper()}**"]
    retail = f"UK retail median {money(q.retail_median, q.currency)} ({money(q.retail_ex_vat, q.currency)} ex VAT)"
    if lad:
        retail += f" from {lad.n} listings, range {money(lad.min, lad.currency)}–{money(lad.max, lad.currency)}"
    facts.append(retail)
    if q.total_cost_inr is not None:
        facts.append(f"Your cost {money(q.total_cost_inr, 'INR')} per piece incl. packing and inland freight")
    if q.fx_rate is not None:
        facts.append(f"FX 1 {q.currency} = ₹{q.fx_rate:.2f}")
    out.append(" · ".join(facts))
    return out + [""]


def _demand_section(brief: Brief) -> list[str]:
    d = brief.demand
    out = ["## Demand (estimate)", ""]
    if d is None:
        return out + ["No demand data.", ""]
    term = f"“{d.term}”" + (" (broader proxy term)" if d.is_proxy else "")
    out.append(f"- Search term: {term}")
    if d.mean_12m is not None:
        out.append(f"- Mean interest, last 12 months: {d.mean_12m:.0f}/100")
    if d.yoy_change is not None:
        out.append(f"- Year on year: {pct(d.yoy_change, signed=True)}")
    if d.peak_months:
        out.append("- Peak months: " + ", ".join(MONTHS[m - 1] for m in d.peak_months if 1 <= m <= 12))
    if d.top_regions:
        out.append("- Strongest regions: " + ", ".join(d.top_regions))
    if d.buying_window_note:
        out.append(f"- Buying window: {d.buying_window_note}")
    if d.related_queries:
        out.append("- Related searches: " + ", ".join(d.related_queries[:8]))
    if d.autocomplete:
        out.append("- Autocomplete: " + ", ".join(d.autocomplete[:8]))
    return out + [""]


def _origin_section(brief: Brief) -> list[str]:
    o = brief.origin
    out = ["## Competition and origin", ""]
    if o is None or not (o.checked or o.ebay_total):
        return out + ["No origin data.", ""]
    if o.checked:
        line = f"- Amazon products checked: {o.checked} — India {o.india}, China {o.china}, other {o.other}, unknown {o.unknown}"
        if o.india_share is not None:
            line += f" (India {pct(o.india_share)} of known origins)"
        out.append(line)
    if o.ebay_total:
        out.append(f"- eBay: {o.ebay_from_india} of {o.ebay_total} listings located in India")
    if brief.market == "uk":
        out.append("- Duty from India: 0% under the India–UK CETA, with valid proof of origin")
    return out + [""]


def _themes_section(brief: Brief) -> list[str]:
    out = ["## What UK customers say", ""]
    if not brief.themes:
        return out + ["No review themes found.", ""]
    for kind, title in (("complaint", "Complaints"), ("praise", "Praise")):
        themes = [t for t in brief.themes if t.kind == kind]
        if not themes:
            continue
        out += [f"### {title}", ""]
        headers = ["Theme", "Share", "Reviews", "Quote"] + (["Fix"] if kind == "complaint" else [])
        rows = []
        for t in themes:
            quote = f"“{t.quotes[0]}”" if t.quotes else None
            row: list[object] = [t.label, pct(t.share), t.count, quote]
            if kind == "complaint":
                row.append(t.fix)
            rows.append(row)
        out += _table(headers, rows) + [""]
    return out


def _buyers_section(brief: Brief) -> list[str]:
    out = ["## Buyers", ""]
    if not brief.buyers:
        return out + ["No buyers found.", ""]
    rows = [
        [
            i,
            b.name,
            b.kind.replace("_", " "),
            b.city,
            None if b.fit_score is None else f"{b.fit_score:.0f}",
            _link(b.website, b.domain or b.website),
            b.phone,
            b.why,
        ]
        for i, b in enumerate(brief.buyers, 1)
    ]
    return out + _table(["#", "Name", "Kind", "City", "Fit", "Website", "Phone", "Why"], rows) + [""]


def brief_markdown(brief: Brief) -> str:
    """The whole brief as Markdown (headline, quote, demand, origin, themes, buyers, pitches,
    evidence list, assumptions, "verify duty and rules of origin before quoting" note)."""
    title = brief.headline or (
        f"{brief.product.product_type} → {brief.market_label}" if brief.product else f"ExportScout brief → {brief.market_label}"
    )
    out = [f"# {title}", ""]
    meta = [f"Generated {brief.created_at}", brief.market_label, f"{brief.credits_used} SerpApi credits"]
    if brief.market_score is not None:
        meta.insert(1, f"Market Opportunity Score {brief.market_score.total:.0f}/100 (estimate)")
    out += ["_" + " · ".join(meta) + "_", ""]
    if brief.product:
        out += [f"**Product:** {brief.product.product_type} · keywords: {', '.join(brief.product.keywords)}", ""]
    if brief.summary_md:
        out += ["## Summary", "", brief.summary_md.strip(), ""]
    if brief.warnings:
        out += ["> **Warnings**"] + [f"> - {w}" for w in brief.warnings] + [""]
    out += _quote_section(brief)
    out += _demand_section(brief)
    out += _origin_section(brief)
    out += _themes_section(brief)
    if brief.market_score is not None and brief.market_score.components:
        rows = [
            [name.replace("_", " "), f"{c.points:.1f} / {c.max_points:g}", c.detail]
            for name, c in brief.market_score.components.items()
        ]
        out += ["## Market Opportunity Score", ""] + _table(["Component", "Points", "Detail"], rows) + [""]
    out += _buyers_section(brief)
    if brief.pitches:
        out += ["## Pitches", ""]
        for p in brief.pitches:
            out += [f"### {p.buyer_name}", "", f"**Subject:** {p.subject}", "", p.body.strip(), ""]
    if brief.spec_improvements:
        out += ["## Spec improvements", ""] + [f"{i}. {s}" for i, s in enumerate(brief.spec_improvements, 1)] + [""]
    try:
        notes = category(brief.inputs.category).get("compliance_notes", [])
    except KeyError:
        notes = []
    if notes:
        out += ["## Compliance notes", ""] + [f"- {n}" for n in notes] + [""]
    if brief.quote is not None and (brief.quote.assumptions or brief.quote.fx_rate is not None):
        rows = [
            [_ASSUMPTION_LABELS.get(k, k.replace("_", " ")), _assumption_value(k, v)] for k, v in brief.quote.assumptions.items()
        ]
        if brief.quote.fx_rate is not None:
            rows.append([f"Exchange rate ({brief.quote.currency}→INR, live)", f"{brief.quote.fx_rate:.2f}"])
        out += ["## Assumptions", ""] + _table(["Assumption", "Value"], rows) + [""]
    if brief.evidence:
        rows = [[f"`{e.id}`", e.engine, e.title, _link(e.url, "link"), e.fetched_at] for e in brief.evidence]
        out += ["## Evidence", ""] + _table(["ID", "Engine", "Title", "URL", "Fetched"], rows) + [""]
    out += ["---", "", f"_{FOOTER}_", ""]
    return "\n".join(out)


def buyers_csv(brief: Brief) -> str:
    """Ranked buyers as CSV: rank, name, kind, city, fit_score, website, phone, top signals, why."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for i, b in enumerate(brief.buyers, 1):
        writer.writerow(
            [
                i,
                b.name,
                b.kind,
                b.city or "",
                "" if b.fit_score is None else f"{b.fit_score:.1f}",
                b.website or "",
                b.phone or "",
                b.domain or "",
                _top_signals(b),
                b.why or "",
            ]
        )
    return buf.getvalue()
