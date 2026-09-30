"""Step 5: quality gaps from customer reviews (plan §5).

The LLM only groups reviews into themes (by review index) and picks quotes. Counts, shares
and evidence IDs are computed here from the indices, and quotes are checked to be verbatim.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from exportscout.llm import prompts
from exportscout.llm.client import LLM
from exportscout.llm.tasks import dump, run_with_fallback
from exportscout.models import ReviewSnippet, ReviewTheme

MAX_REVIEWS = 200
MAX_REVIEW_CHARS = 1000
MAX_COMPLAINTS = 5
MAX_PRAISE = 3
MAX_QUOTE_CHARS = 160


class _ThemeOut(BaseModel):
    label: str
    kind: Literal["complaint", "praise"]
    review_indices: list[int] = Field(default_factory=list)
    quotes: list[str] = Field(default_factory=list)
    fix: str | None = None


class _ThemesOut(BaseModel):
    themes: list[_ThemeOut]


def _norm(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def _review_text(r: ReviewSnippet) -> str:
    return f"{r.title}. {r.text}" if r.title else r.text


def _excerpt(text: str, term: str | None = None) -> str:
    """The sentence containing ``term`` (or the first sentence), trimmed to a short quote."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]
    if not sentences:
        return ""
    pick = sentences[0]
    if term:
        pick = next((s for s in sentences if term in s.lower()), pick)
    return pick if len(pick) <= MAX_QUOTE_CHARS else pick[: MAX_QUOTE_CHARS - 1].rsplit(" ", 1)[0] + "…"


def build_theme(
    label: str,
    kind: Literal["complaint", "praise"],
    indices: list[int],
    reviews: list[ReviewSnippet],
    quotes: list[str],
    fix: str | None,
) -> ReviewTheme:
    """A theme with count, share and evidence computed from review indices (invalid ones ignored)."""
    n = len(reviews)
    valid = sorted({i for i in indices if isinstance(i, int) and 0 <= i < n})
    corpus = [_norm(_review_text(reviews[i])) for i in valid] or [_norm(_review_text(r)) for r in reviews]
    kept = []
    for q in quotes:
        q = q.strip().strip('"').strip()
        if q and len(q) <= 2 * MAX_QUOTE_CHARS and any(_norm(q) in text for text in corpus) and q not in kept:
            kept.append(q if len(q) <= MAX_QUOTE_CHARS else q[: MAX_QUOTE_CHARS - 1].rsplit(" ", 1)[0] + "…")
    if not kept and valid:
        ex = _excerpt(reviews[valid[0]].text)
        kept = [ex] if ex else []
    return ReviewTheme(
        label=label.strip(),
        kind=kind,
        count=len(valid),
        share=len(valid) / n if n else 0.0,
        quotes=kept[:2],
        fix=((fix or "").strip() or None) if kind == "complaint" else None,
        evidence_ids=list(dict.fromkeys(reviews[i].evidence_id for i in valid)),
    )


def order_themes(themes: list[ReviewTheme]) -> list[ReviewTheme]:
    """Complaints first by count, then praise; drop empty themes; cap each list."""
    live = [t for t in themes if t.count > 0]
    complaints = sorted((t for t in live if t.kind == "complaint"), key=lambda t: t.count, reverse=True)
    praise = sorted((t for t in live if t.kind == "praise"), key=lambda t: t.count, reverse=True)
    return complaints[:MAX_COMPLAINTS] + praise[:MAX_PRAISE]


def lexicon_themes(reviews: list[ReviewSnippet], category: dict[str, Any]) -> list[ReviewTheme]:
    """Fallback: substring matching with the category's complaint and praise lexicons."""
    texts = [_review_text(r).lower() for r in reviews]
    themes = []
    for kind, lexicon in (("complaint", category.get("complaint_lexicon", {})), ("praise", category.get("praise_lexicon", {}))):
        for label, spec in lexicon.items():
            terms = [t.lower() for t in spec.get("terms", [])]
            hits: list[int] = []
            quotes: list[str] = []
            for i, text in enumerate(texts):
                if kind == "praise" and reviews[i].rating is not None and reviews[i].rating <= 2:
                    continue
                term = next((t for t in terms if t in text), None)
                if term is None:
                    continue
                hits.append(i)
                if len(quotes) < 2:
                    ex = _excerpt(reviews[i].text, term) or _excerpt(_review_text(reviews[i]), term)
                    if ex:
                        quotes.append(ex)
            themes.append(build_theme(label, kind, hits, reviews, quotes, spec.get("fix")))  # type: ignore[arg-type]
    return order_themes(themes)


def review_themes(llm: LLM | None, reviews: list[ReviewSnippet], category: dict[str, Any]) -> list[ReviewTheme]:
    """Cluster reviews into complaint and praise themes with counts, shares, short quotes and,
    for complaints, a ``fix`` (spec change / pitch line). Complaints first, by count.
    Fallback without LLM: keyword lexicon in category["complaint_lexicon"]."""
    reviews = [r for r in reviews if r.text and r.text.strip()][:MAX_REVIEWS]
    if not reviews:
        return []

    def with_llm(llm: LLM) -> list[ReviewTheme]:
        lines = []
        for i, r in enumerate(reviews):
            stars = f" ({r.rating:g}★)" if r.rating is not None else ""
            lines.append(f"[{i}]{stars} {_review_text(r)[:MAX_REVIEW_CHARS]}".replace("\n", " "))
        hints = {label: spec.get("fix") for label, spec in category.get("complaint_lexicon", {}).items()}
        user = (
            f"{len(reviews)} UK customer reviews of products similar to the exporter's.\n"
            f"Example complaint themes and fixes for this product family (use only if the reviews support them): "
            f"{dump(hints)}\n<data>\n" + "\n".join(lines) + "\n</data>"
        )
        out = llm.parse(task="review_themes", system=prompts.REVIEWS, user=user, schema=_ThemesOut, effort="low")
        merged: dict[tuple[str, str], _ThemeOut] = {}
        for t in out.themes:
            key = (t.kind, t.label.strip().lower())
            if key in merged:
                prev = merged[key]
                prev.review_indices += t.review_indices
                prev.quotes += t.quotes
                prev.fix = prev.fix or t.fix
            else:
                merged[key] = t.model_copy(deep=True)
        themes = [build_theme(t.label, t.kind, t.review_indices, reviews, t.quotes, t.fix) for t in merged.values()]
        return order_themes(themes) or lexicon_themes(reviews, category)

    return run_with_fallback(llm, "review_themes", with_llm, lambda: lexicon_themes(reviews, category))
