"""LLM tasks used by the orchestrator. Every task has a deterministic fallback used when
``llm`` is None or ``llm.available`` is False, or the call fails, so a run never breaks
because of the LLM.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, Field

from exportscout.llm import prompts
from exportscout.llm.client import LLM
from exportscout.models import (
    Brief,
    BuyerCandidate,
    BuyerKind,
    BuyerSignal,
    MarketRow,
    Pitch,
    ProductIdentity,
    ReviewTheme,
)
from exportscout.pipeline import markets

log = logging.getLogger(__name__)
R = TypeVar("R")

SIGN_OFF = "[Your name], [Company], Moradabad"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
SYMBOLS = {"GBP": "£", "INR": "₹", "USD": "$", "EUR": "€", "AUD": "A$"}
MAX_PHRASE_CHARS = 60  # longest Amazon search phrase accepted from market_keyword
_QUOTES = "\"'“”„‟‘’‚‛«»‹›`"
_SIGNAL_ORDER = [
    "category_match",
    "india_sourcing",
    "ads_active",
    "trade_page",
    "news",
    "hiring",
    "price_tier",
    "showroom",
    "multi_engine",
    "size",
    "website",
    "phone",
]


class BriefText(BaseModel):
    summary_md: str  # 120-220 words, cites evidence as [ev:ID]
    spec_improvements: list[str] = Field(default_factory=list)  # 3-5 concrete spec changes


class _BuyerTag(BaseModel):
    index: int
    kind: BuyerKind
    why: str


class _BuyerTags(BaseModel):
    buyers: list[_BuyerTag]


class _PitchOut(BaseModel):
    index: int
    subject: str
    body: str


class _PitchesOut(BaseModel):
    pitches: list[_PitchOut]


class _MarketKeyword(BaseModel):
    phrase: str


# --------------------------------------------------------------------------- helpers


def run_with_fallback(llm: LLM | None, task: str, with_llm: Callable[[LLM], R], fallback: Callable[[], R]) -> R:
    """Run ``with_llm`` if the LLM is usable, else (or on any failure) ``fallback``."""
    if llm is not None and llm.available:
        try:
            return with_llm(llm)
        except Exception as exc:  # noqa: BLE001 - a run must never fail because of the LLM
            log.warning("%s: LLM step failed, using deterministic fallback (%s: %s)", task, type(exc).__name__, exc)
    return fallback()


def dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


def clip_words(text: str | None, n: int) -> str:
    words = (text or "").split()
    return " ".join(words[:n]).rstrip(",;:") if len(words) > n else " ".join(words)


def money(value: float | None, currency: str | None) -> str:
    if value is None:
        return "n/a"
    sym = SYMBOLS.get(currency or "", f"{currency} " if currency else "")
    if currency == "INR":
        return f"{sym}{value:,.0f}"
    return f"{sym}{value:,.2f}"


def pct(value: float | None, signed: bool = False) -> str | None:
    if value is None:
        return None
    return f"{value * 100:+.0f}%" if signed else f"{value * 100:.0f}%"


def _pct1(value: float) -> str:
    """One decimal, trailing zero dropped: 0.157 -> "15.7%", 0.1 -> "10%" (duty rates)."""
    return f"{value * 100:.1f}".rstrip("0").rstrip(".") + "%"


_CITE = re.compile(r"\[ev:\s*([^\]]*)\]")


def clean_citations(text: str, valid: set[str]) -> str:
    """Drop ``[ev:ID]`` citations whose ID is not a known evidence ID."""

    def repl(m: re.Match[str]) -> str:
        ids = [p.strip().removeprefix("ev:").strip() for p in re.split(r"[,;\s]+", m.group(1))]
        return "".join(f"[ev:{i}]" for i in ids if i in valid)

    out = _CITE.sub(repl, text)
    out = re.sub(r"[ \t]+(?=[.,;:!?)])", "", out)
    out = re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", out)
    return out.strip()


def strip_citations(text: str) -> str:
    return clean_citations(text, set())


def _cite(ids: list[str], valid: set[str], n: int = 1) -> str:
    return "".join(f" [ev:{i}]" for i in [i for i in ids if i in valid][:n])


def sorted_signals(c: BuyerCandidate) -> list[BuyerSignal]:
    return sorted(c.signals, key=lambda s: _SIGNAL_ORDER.index(s.kind) if s.kind in _SIGNAL_ORDER else 99)


# --------------------------------------------------------------------------- tag_buyers


def _heuristic_kind(c: BuyerCandidate) -> BuyerKind:
    ident = f"{c.name} {c.domain or ''}".lower()
    if "wholesale" in ident or "trade" in ident:
        return "wholesaler"
    if "import" in ident:
        return "importer"
    texts = " ".join(c.seen_texts + [s.detail for s in c.signals]).lower()
    if "wholesaler" in texts:  # Maps place type, e.g. "Home goods wholesaler"
        return "wholesaler"
    physical = c.address or c.locations_count or any("maps" in s for s in c.sources) or any(
        s.kind == "showroom" for s in c.signals
    )
    if physical or not c.domain:
        return "retailer"
    return "online_brand"


def _heuristic_why(c: BuyerCandidate, product: ProductIdentity) -> str:
    top = sorted_signals(c)[:2]
    if top:
        return clip_words("; ".join(s.detail.rstrip(".") for s in top), 20)
    if c.sources:
        return clip_words(f"Seen in {', '.join(c.sources)} results for {product.product_type}", 20)
    return f"Possible buyer of {product.product_type}"


def _tag_fallback(c: BuyerCandidate, product: ProductIdentity) -> BuyerCandidate:
    kind = c.kind if c.kind != "unknown" else _heuristic_kind(c)
    return c.model_copy(update={"kind": kind, "why": c.why or _heuristic_why(c, product)})


def _buyer_record(i: int, c: BuyerCandidate) -> dict[str, Any]:
    return {
        "index": i,
        "name": c.name,
        "domain": c.domain,
        "city": c.city,
        "seen_in": c.sources,
        "signals": [f"{s.kind}: {s.detail}" for s in sorted_signals(c)[:6]],
        "texts": [t[:240] for t in c.seen_texts[:6]],
    }


def tag_buyers(llm: LLM | None, candidates: list[BuyerCandidate], product: ProductIdentity) -> list[BuyerCandidate]:
    """Set ``kind`` (retailer / online_brand / wholesaler / importer / marketplace) and a
    one-line ``why`` from each candidate's seen_texts. Returns updated copies, same order."""
    fallback = [_tag_fallback(c, product) for c in candidates]
    if not candidates:
        return fallback

    def with_llm(llm: LLM) -> list[BuyerCandidate]:
        user = (
            f"Product: {product.product_type} (material: {product.material or 'unknown'})\n"
            f"<data>\n{dump([_buyer_record(i, c) for i, c in enumerate(candidates)])}\n</data>"
        )
        out = llm.parse(task="tag_buyers", system=prompts.TAG_BUYERS, user=user, schema=_BuyerTags, effort="low")
        tags = {t.index: t for t in out.buyers if 0 <= t.index < len(candidates)}
        result = []
        for i, (c, fb) in enumerate(zip(candidates, fallback)):
            tag = tags.get(i)
            if tag is None:
                result.append(fb)
                continue
            kind = tag.kind if tag.kind != "unknown" else fb.kind
            result.append(c.model_copy(update={"kind": kind, "why": clip_words(tag.why, 20) or fb.why}))
        return result

    return run_with_fallback(llm, "tag_buyers", with_llm, lambda: fallback)


# --------------------------------------------------------------------------- market_keyword


def _clean_phrase(text: str) -> str | None:
    """First non-empty line, spaces collapsed, surrounding quotes stripped; None if nothing is
    left or it is longer than MAX_PHRASE_CHARS."""
    line = next((ln for ln in text.splitlines() if ln.strip()), "")
    phrase = " ".join(line.split()).strip(" " + _QUOTES).strip()
    return phrase if 0 < len(phrase) <= MAX_PHRASE_CHARS else None


def market_keyword(llm: LLM | None, product: ProductIdentity, language: str, category: dict[str, Any]) -> str:
    """The Amazon search phrase for a market whose shoppers search in ``language`` (e.g. "de").
    Fallback (no LLM, or an unusable answer): the category's word-map translation of the
    first retail keyword."""
    fallback = markets.translate_keyword(product.keywords[0] if product.keywords else product.product_type, language, category)

    def with_llm(llm: LLM) -> str:
        payload = {
            "product_type": product.product_type,
            "keywords": product.keywords,
            "material": product.material,
            "language": language,
        }
        user = f"<data>\n{dump(payload)}\n</data>"
        out = llm.parse(task="market_keyword", system=prompts.MARKET_KEYWORD, user=user, schema=_MarketKeyword, effort="low")
        return _clean_phrase(out.phrase) or fallback

    return run_with_fallback(llm, "market_keyword", with_llm, lambda: fallback)


# --------------------------------------------------------------------------- write_brief


def brief_facts(brief: Brief) -> dict[str, Any]:
    """Compact JSON of the computed facts the writer may use, plus titles of cited evidence."""
    known = {e.id: e for e in brief.evidence}
    refs: dict[str, None] = {}

    def ev(ids: list[str], n: int) -> list[str]:
        kept = [i for i in ids if i in known][:n]
        refs.update(dict.fromkeys(kept))
        return kept

    facts: dict[str, Any] = {"headline": brief.headline, "market": brief.market_label}
    if brief.product:
        facts["product"] = {
            "type": brief.product.product_type,
            "material": brief.product.material or brief.inputs.material,
            "finish": brief.inputs.finish,
            "keywords": brief.product.keywords,
        }
    if brief.inputs.moq:
        facts["moq"] = brief.inputs.moq
    if brief.ladder:
        lad = brief.ladder
        facts["uk_retail_prices"] = {
            "currency": lad.currency,
            "listings": lad.n,
            "min": lad.min,
            "median": lad.p50,
            "max": lad.max,
            "evidence": ev(lad.evidence_ids, 3),
        }
    if brief.quote:
        q = brief.quote
        facts["quote_estimate"] = {
            "currency": q.currency,
            "fob_via_importer": round(q.fob_importer, 2),
            "fob_direct_to_retailer": round(q.fob_retailer, 2),
            "fob_via_importer_inr": q.fob_importer_inr and round(q.fob_importer_inr),
            "fob_direct_to_retailer_inr": q.fob_retailer_inr and round(q.fob_retailer_inr),
            "owner_cost_inr": q.total_cost_inr and round(q.total_cost_inr),
            "margin_at_direct_price": pct(q.margin_retailer),
            "margin_at_importer_price": pct(q.margin_importer),
            "verdict": q.verdict.replace("_", "-").upper(),
            "duty_from_india": pct(q.assumptions.get("duty_pct")),
        }
    if brief.demand:
        d = brief.demand
        facts["demand_estimate"] = {
            "term": d.term,
            "broader_proxy_term": d.is_proxy,
            "mean_interest_12m_of_100": d.mean_12m and round(d.mean_12m),
            "year_on_year": pct(d.yoy_change, signed=True),
            "peak_months": [MONTHS[m - 1] for m in d.peak_months if 1 <= m <= 12],
            "top_regions": d.top_regions[:3],
            "buying_window": d.buying_window_note,
            "evidence": ev(d.evidence_ids, 3),
        }
    if brief.origin and (brief.origin.checked or brief.origin.ebay_total):
        o = brief.origin
        facts["origin_of_competing_products"] = {
            "amazon_products_checked": o.checked,
            "made_in_india": o.india,
            "made_in_china": o.china,
            "other": o.other,
            "unknown": o.unknown,
            "ebay_listings": o.ebay_total,
            "ebay_located_in_india": o.ebay_from_india,
            "evidence": ev(o.india_examples or o.evidence_ids, 3),
        }
    if brief.market_score:
        facts["market_score_estimate"] = f"{brief.market_score.total:.0f}/100"
    if brief.themes:
        facts["review_themes"] = [
            {
                "label": t.label,
                "kind": t.kind,
                "reviews": t.count,
                "share": pct(t.share),
                "fix": t.fix,
                "quote": t.quotes[0] if t.quotes else None,
                "evidence": ev(t.evidence_ids, 2),
            }
            for t in brief.themes[:6]
        ]
    if brief.buyers:
        facts["top_buyers"] = [
            {
                "name": b.name,
                "kind": b.kind,
                "city": b.city,
                "fit_score": b.fit_score and round(b.fit_score),
                "why": b.why,
                "signals": [
                    {"detail": s.detail, "evidence": (ev([s.evidence_id], 1) or [None])[0]} for s in sorted_signals(b)[:3]
                ],
            }
            for b in brief.buyers[:5]
        ]
    if brief.markets:
        facts["market_compare"] = {
            "method": "quick scan: one Amazon search per market, worked back to FOB with that market's VAT, "
            "markups, freight and duty (estimates)",
            "markets_best_first": [_market_line(r, ev(r.evidence_ids, 2)) for r in brief.markets],
        }
        alt = markets.best_alternative(brief.markets)
        if alt is not None:
            facts["market_compare"]["best_other_market"] = alt.label
    facts["evidence_titles"] = {i: known[i].title[:120] for i in refs}
    return facts


def _market_line(r: MarketRow, evidence: list[str]) -> dict[str, Any]:
    """One Market Compare row for the writer: verdict, margin, FOB ₹ ceiling, duty, evidence."""
    q = r.quote
    return {
        "market": r.label,
        "main_market": r.is_home,
        "market_fit": f"{r.score.total:.0f}/100" if r.score else None,
        "verdict": (q.verdict if q else "unknown").replace("_", "-").upper(),
        "margin_at_direct_price": pct(q.margin_retailer) if q else None,
        "fob_ceiling_inr": round(q.fob_retailer_inr) if q and q.fob_retailer_inr is not None else None,
        "duty_from_india": _pct1(r.duty_pct),
        "duty_detail": r.duty_detail,
        "thin_data": r.thin_data,
        "evidence": evidence,
    }


def _complaint_fixes(brief: Brief) -> list[str]:
    fixes = [t.fix.strip() for t in brief.themes if t.kind == "complaint" and t.fix and t.fix.strip()]
    return list(dict.fromkeys(fixes))


def _fallback_summary(brief: Brief) -> str:
    valid = {e.id for e in brief.evidence}
    lines: list[str] = []
    q, lad = brief.quote, brief.ladder
    if q:
        s = (
            f"**Estimated FOB quote range: {money(q.fob_importer, q.currency)} (via an importer) – "
            f"{money(q.fob_retailer, q.currency)} (direct to a retailer)**"
        )
        if q.fob_importer_inr is not None and q.fob_retailer_inr is not None:
            s += f", about {money(q.fob_importer_inr, 'INR')}–{money(q.fob_retailer_inr, 'INR')}"
        s += f", worked back from a UK retail median of {money(q.retail_median, q.currency)}"
        if lad:
            s += f" across {lad.n} listings" + _cite(lad.evidence_ids, valid)
        s += "."
        if q.margin_retailer is not None:
            s += f" Estimated margin at the retailer price: {pct(q.margin_retailer)} ({q.verdict.replace('_', '-').upper()})."
        lines.append(s)
    elif lad:
        lines.append(
            f"UK retail median {money(lad.p50, lad.currency)} across {lad.n} listings{_cite(lad.evidence_ids, valid)}; "
            "add your unit cost to get a quote range and margin."
        )
    else:
        lines.append("Not enough UK prices were found to estimate a quote range.")
    d = brief.demand
    if d:
        bits = []
        if d.peak_months:
            bits.append("interest peaks in " + ", ".join(MONTHS[m - 1] for m in d.peak_months[:3] if 1 <= m <= 12))
        if d.yoy_change is not None:
            bits.append(f"{pct(d.yoy_change, signed=True)} year on year (estimate)")
        if d.top_regions:
            bits.append("strongest in " + ", ".join(d.top_regions[:2]))
        term = f"“{d.term}”" + (" (a broader proxy term)" if d.is_proxy else "")
        s = f"Demand for {term}: " + ("; ".join(bits) if bits else "little search data") + _cite(d.evidence_ids, valid) + "."
        if d.buying_window_note:
            s += f" {d.buying_window_note.rstrip('.')}."
        lines.append(s)
    o = brief.origin
    if o and o.checked:
        lines.append(
            f"Competition: of {o.checked} Amazon products checked, {o.india} were made in India and {o.china} in China"
            + _cite(o.india_examples or o.evidence_ids, valid)
            + "."
        )
    if brief.market == "uk":
        lines.append("Duty from India is 0% under the India–UK CETA, with valid proof of origin.")
    complaints = [t for t in brief.themes if t.kind == "complaint"][:3]
    if complaints:
        lines.append(
            "Top customer complaints: "
            + ", ".join(f"{t.label.lower()} ({pct(t.share)}){_cite(t.evidence_ids, valid)}" for t in complaints)
            + "."
        )
    top = brief.buyers[:3]
    if top:
        names = ", ".join(f"{b.name}" + (f" (fit {b.fit_score:.0f}/100)" if b.fit_score is not None else "") for b in top)
        lines.append(f"Contact first: {names}.")
    alternative = _alternative_line(brief, valid)
    if alternative:
        lines.append(alternative)
    lines.append("Next step: send photos, a spec sheet and samples to the top buyers.")
    return "\n".join(f"- {line}" for line in lines)  # the headline is shown above the summary


def _alternative_line(brief: Brief, valid: set[str]) -> str | None:
    """The best other market from Market Compare, if any; named as the next market to try when
    the home verdict is NO-GO and the alternative clears the cost (GO or TIGHT)."""
    alt = markets.best_alternative(brief.markets)
    q = alt.quote if alt is not None else None
    if alt is None or q is None or q.margin_retailer is None:
        return None
    s = (
        f"Best other market in a quick Amazon-only scan: {alt.label}, with an estimated "
        f"{pct(q.margin_retailer)} margin at {money(q.fob_retailer, q.currency)} FOB"
    )
    if q.fob_retailer_inr is not None:
        s += f" ({money(q.fob_retailer_inr, 'INR')})"
    s += _cite(alt.evidence_ids, valid) + "."
    if brief.quote is not None and brief.quote.verdict == "no_go" and q.verdict in ("go", "tight"):
        home = next((r.short_label or r.label for r in brief.markets if r.is_home), brief.market_label)
        s += f" The {home} margin is too thin, so it is the next market to try."
    return s


def _fallback_brief(brief: Brief) -> BriefText:
    return BriefText(summary_md=_fallback_summary(brief), spec_improvements=_complaint_fixes(brief)[:5])


def write_brief(llm: LLM | None, brief: Brief) -> BriefText:
    """Narrative summary for the owner from the computed brief. Must only use numbers
    present in the brief and cite evidence IDs that exist in brief.evidence."""
    valid = {e.id for e in brief.evidence}

    def with_llm(llm: LLM) -> BriefText:
        user = f"Facts computed by the tool (JSON):\n<data>\n{dump(brief_facts(brief))}\n</data>"
        out = llm.parse(task="write_brief", system=prompts.BRIEF, user=user, schema=BriefText, effort="medium")
        summary = clean_citations(out.summary_md, valid)
        if not summary:
            raise ValueError("empty summary")
        specs = [strip_citations(s) for s in out.spec_improvements if s and s.strip()]
        specs = list(dict.fromkeys(specs))
        for fix in _complaint_fixes(brief):
            if len(specs) >= 3:
                break
            if fix not in specs:
                specs.append(fix)
        return BriefText(summary_md=summary, spec_improvements=specs[:5])

    return run_with_fallback(llm, "write_brief", with_llm, lambda: _fallback_brief(brief))


# --------------------------------------------------------------------------- write_pitches

_MONEY = re.compile(r"(?:£|₹|\$|€|\bGBP\s?|\bINR\s?|\bRs\.?\s?)(\d[\d,]*(?:\.\d+)?)")
_JOB_AD = "Their job ad"  # india_sourcing signals found in a Google Jobs ad start with this (buyers._india_detail)
_HIRING_WORDS = re.compile(r"\b(?:hiring|recruit|job ad)", re.IGNORECASE)


def _prices_ok(body: str, allowed: list[float]) -> bool:
    """Every price in the email must be one of the computed quote numbers."""
    for m in _MONEY.finditer(body):
        value = float(m.group(1).replace(",", ""))
        if not any(abs(value - a) <= max(0.01, 0.006 * a) for a in allowed):
            return False
    return True


def _pitch_prices(brief: Brief) -> list[tuple[float, float | None]]:
    """(GBP, INR) ends of the quote range that don't lose the owner money."""
    q = brief.quote
    if q is None:
        return []
    ends = [(q.fob_importer, q.fob_importer_inr, q.margin_importer), (q.fob_retailer, q.fob_retailer_inr, q.margin_retailer)]
    return [(fob, inr) for fob, inr, margin in ends if margin is None or margin >= 0]


def _quote_range(brief: Brief) -> str | None:
    """Price line for pitches; a loss-making end of the range is never quoted to a buyer."""
    ends = _pitch_prices(brief)
    cur = brief.quote.currency if brief.quote else None
    if len(ends) == 2:
        return f"{money(ends[0][0], cur)}–{money(ends[1][0], cur)} FOB per piece"
    if len(ends) == 1:
        return f"around {money(ends[0][0], cur)} FOB per piece"
    return None


def _allowed_prices(brief: Brief) -> list[float]:
    return [v for pair in _pitch_prices(brief) for v in pair if v is not None]


def _top_complaints(brief: Brief, n: int = 3) -> list[ReviewTheme]:
    return [t for t in brief.themes if t.kind == "complaint" and t.fix][:n]


def _ensure_sign_off(body: str) -> str:
    body = body.strip()
    if "[Your name]" in body and "[Company]" in body:
        return body
    return f"{body}\n\nBest regards,\n{SIGN_OFF}"


def _pitch_evidence(brief: Brief, c: BuyerCandidate) -> list[str]:
    ids = [s.evidence_id for s in sorted_signals(c)]
    for t in _top_complaints(brief, 2):
        ids += t.evidence_ids[:1]
    return list(dict.fromkeys(ids))


def _plural(noun: str) -> str:
    if noun.endswith("s") and not noun.endswith("ss"):
        return noun  # already plural
    if noun.endswith(("ss", "x", "ch", "sh")):
        return noun + "es"
    if noun.endswith("y") and noun[-2:-1] not in "aeiou":
        return noun[:-1] + "ies"
    return noun + "s"


def _product_words(brief: Brief, plural: bool = False) -> str:
    pt = brief.product.product_type if brief.product else (brief.inputs.description or "metal handicrafts")
    material = brief.inputs.material or (brief.product.material if brief.product else None)
    if material and material.lower() not in pt.lower():
        pt = f"{material} {pt}"
    if plural and brief.product:
        pt = _plural(pt)
    if brief.inputs.finish:
        pt = f"{pt} in {brief.inputs.finish} finish"
    return pt


def _fallback_pitch(brief: Brief, c: BuyerCandidate) -> Pitch:
    pt = brief.product.product_type if brief.product else "home décor"
    kinds = {s.kind for s in c.signals}
    noticed = []
    if "category_match" in kinds:
        noticed.append(f"your {pt} range")
    india = [s for s in c.signals if s.kind == "india_sourcing"]
    if india and all(s.detail.startswith(_JOB_AD) for s in india):
        noticed.append("that you work with suppliers in India")  # a job ad mentions India: don't overstate it
    elif india:
        noticed.append("that you already stock handmade pieces from India")
    if "ads_active" in kinds and len(noticed) < 2:
        noticed.append("that you are actively promoting your range in the UK")
    if "trade_page" in kinds and len(noticed) < 2:
        noticed.append("that you work with trade suppliers")
    where = f" in {c.city}" if c.city else ""
    if noticed:
        opening = f"I came across {c.name}{where} and noticed {' and '.join(noticed)}."
    else:
        opening = f"I came across {c.name}{where} while looking for UK homeware stockists."
    offer = [
        f"We are a small manufacturer in Moradabad, India's brassware hub, making {_product_words(brief, plural=True)}.",
        "Under the India–UK CETA they enter the UK at zero import duty, with proof of origin.",
    ]
    if brief.inputs.moq:
        offer.append(f"Our minimum order is {brief.inputs.moq} pieces.")
    rng = _quote_range(brief)
    if rng:
        offer.append(f"Indicative price: {rng}, depending on finish and volume.")
    paras = [f"Dear {c.name} team,", opening, " ".join(offer)]
    fixes = _top_complaints(brief, 2)
    if fixes:
        labels = " and ".join(t.label.lower() for t in fixes)
        acts = "; and ".join((t.fix or "").strip().rstrip(".")[:1].lower() + (t.fix or "").strip().rstrip(".")[1:] for t in fixes)
        paras.append(f"UK reviews of similar products mention {labels}, so for your order we will {acts}.")
    paras.append("Could I send you photos, a spec sheet or a sample?")
    paras.append(f"Best regards,\n{SIGN_OFF}")
    return Pitch(
        buyer_name=c.name,
        subject=f"Handmade {pt} from Moradabad – zero UK import duty",
        body="\n\n".join(paras),
        evidence_ids=_pitch_evidence(brief, c),
    )


def _pitch_record(i: int, c: BuyerCandidate) -> dict[str, Any]:
    """One buyer for the pitch writer. Hiring signals go in a separate "hiring" note that the
    prompt allows only for timing; a "why" line about hiring or a job ad is left out."""
    why = c.why if c.why and not _HIRING_WORDS.search(c.why) else None
    record: dict[str, Any] = {
        "index": i,
        "name": c.name,
        "kind": c.kind,
        "city": c.city,
        "why": why,
        "signals": [s.detail for s in sorted_signals(c) if s.kind != "hiring"][:4],
        "texts": [t[:200] for t in c.seen_texts[:3]],
    }
    hiring = [s.detail for s in c.signals if s.kind == "hiring"]
    if hiring:
        record["hiring"] = hiring[:2]
    return record


def write_pitches(llm: LLM | None, brief: Brief, buyers: list[BuyerCandidate]) -> list[Pitch]:
    """A short English email per buyer, citing that buyer's own signals and the product's fixes."""
    fallback = [_fallback_pitch(brief, c) for c in buyers]
    if not buyers:
        return fallback

    def with_llm(llm: LLM) -> list[Pitch]:
        payload = {
            "product": _product_words(brief),
            "market": brief.market_label,
            "moq": brief.inputs.moq,
            "quote_range": _quote_range(brief),
            "duty": "0% UK import duty on these goods from India under the India-UK CETA (needs proof of origin)"
            if brief.market == "uk"
            else None,
            "product_fixes": [{"complaint": t.label, "fix": t.fix} for t in _top_complaints(brief)],
            "buyers": [_pitch_record(i, c) for i, c in enumerate(buyers)],
        }
        user = f"<data>\n{dump(payload)}\n</data>"
        out = llm.parse(task="write_pitches", system=prompts.PITCHES, user=user, schema=_PitchesOut, effort="medium")
        got = {p.index: p for p in out.pitches if 0 <= p.index < len(buyers)}
        allowed = _allowed_prices(brief)
        result = []
        for i, (c, fb) in enumerate(zip(buyers, fallback)):
            p = got.get(i)
            body = strip_citations(p.body) if p else ""
            if not body or not p.subject.strip() or not _prices_ok(body, allowed):
                result.append(fb)
                continue
            result.append(
                Pitch(
                    buyer_name=c.name,
                    subject=strip_citations(p.subject),
                    body=_ensure_sign_off(body),
                    evidence_ids=fb.evidence_ids,
                )
            )
        return result

    return run_with_fallback(llm, "write_pitches", with_llm, lambda: fallback)
