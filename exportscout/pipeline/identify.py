"""Step 1: what is this product, in UK retail words? (plan §5)"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from exportscout.llm import prompts
from exportscout.llm.client import LLM
from exportscout.llm.tasks import dump, run_with_fallback
from exportscout.models import Listing, ProductIdentity

MAX_TITLES = 30
MATERIALS = ["brass", "copper", "bronze", "aluminium", "aluminum", "iron", "steel", "nickel", "zinc", "metal"]
# Words that say nothing about what the product is; never count as overlap.
_WEAK = {"set", "pack", "piece", "pcs", "large", "small", "medium", "with", "and", "for", "the", "cm", "mm", "inch", "new", "home", "decor", "gift"}
STYLES = [
    "antique", "vintage", "moroccan", "hammered", "rustic", "industrial", "modern", "boho", "handmade",
    "indian", "traditional", "art deco", "nautical", "ornate", "embossed", "engraved", "gold", "silver",
]  # fmt: skip


def tokens(text: str) -> list[str]:
    """Lowercase word tokens with a light plural strip (lanterns -> lantern, but brass stays)."""
    out = []
    for tok in re.findall(r"[a-z]+", text.lower()):
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        out.append(tok)
    return out


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _rank_seeds(seeds: list[str], titles: list[str], hint: str | None) -> list[str]:
    """Seed keywords that share tokens with the titles or hint, best token overlap first."""
    hint_toks = set(tokens(hint or ""))
    df: Counter[str] = Counter()
    for t in titles:
        df.update(set(tokens(t)))
    n = max(1, len(titles))

    def weight(tok: str) -> float:
        if tok in _WEAK:
            return 0.0
        return (2.0 if tok in hint_toks else 0.0) + df[tok] / n

    scored = []
    for seed in seeds:
        toks = tokens(seed)
        if not toks:
            continue
        matched = [t for t in toks if weight(t) > 0]
        if not matched:
            continue
        head_match = weight(toks[-1]) > 0
        score = sum(weight(t) for t in toks) - 0.5 * (len(toks) - len(matched))
        scored.append((head_match, score, seed))
    if any(h for h, _, _ in scored):
        scored = [s for s in scored if s[0]]
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    top = scored[0][1] if scored else 0.0
    return [seed for _, score, seed in scored if score >= 0.35 * top]


def _broad_key(words: list[str], broad_terms: dict[str, list[str]]) -> str | None:
    for phrase in words:
        for tok in reversed(tokens(phrase)):
            if broad_terms.get(tok):
                return tok
    return None


def broad_term_for(words: list[str], broad_terms: dict[str, list[str]]) -> str | None:
    """Broad Trends term from the category mapping, matched by the head noun (last word first)."""
    key = _broad_key(words, broad_terms)
    return broad_terms[key][0] if key else None


def _material(material: str | None, texts: list[str]) -> str | None:
    if material:
        return _clean(material)
    counts = Counter(t for text in texts for t in set(tokens(text)) if t in MATERIALS)
    return counts.most_common(1)[0][0] if counts else None


def _style_tags(texts: list[str]) -> list[str]:
    blob = [" ".join(tokens(t)) for t in texts]
    counts = Counter(s for s in STYLES for b in blob if re.search(rf"\b{s}\b", b))
    return [s for s, _ in counts.most_common(4)]


def fallback_identity(titles: list[str], *, hint: str | None, material: str | None, category: dict[str, Any]) -> ProductIdentity:
    """Deterministic identity: hint first, then category seed keywords matching the titles."""
    seeds = category.get("seed_keywords", [])
    broad_terms = category.get("broad_terms", {})
    ranked = _rank_seeds(seeds, titles, hint)
    keywords: list[str] = []
    if hint and hint.strip():
        keywords.append(_clean(hint))
    keywords += ranked
    if not keywords:
        keywords = list(seeds[:3])
    keywords = list(dict.fromkeys(keywords))
    product_type = keywords[0] if keywords else "metal home décor"
    key = _broad_key([product_type, *keywords], broad_terms)
    if key and len(keywords) < 3:
        keywords += [b for b in broad_terms[key] if b not in keywords]
    head = tokens(product_type)
    broad = broad_terms[key][0] if key else (head[-1] if head else product_type)
    texts = [*titles, *([hint] if hint else [])]
    return ProductIdentity(
        product_type=product_type,
        keywords=keywords[:5],
        broad_term=broad,
        material=_material(material, texts),
        style_tags=_style_tags(texts),
    )


def _merge(out: ProductIdentity, fb: ProductIdentity, material: str | None, broad_terms: dict[str, list[str]]) -> ProductIdentity:
    product_type = _clean(out.product_type) or fb.product_type
    keywords = [k for k in dict.fromkeys(_clean(k) for k in out.keywords) if k][:5]
    for k in fb.keywords:
        if len(keywords) >= 3:
            break
        if k not in keywords:
            keywords.append(k)
    broad = _clean(out.broad_term)
    if not broad or len(broad.split()) > 2:
        broad = broad_term_for([product_type, *keywords], broad_terms) or fb.broad_term
    tags = [t for t in dict.fromkeys(_clean(t) for t in out.style_tags) if t][:4]
    return ProductIdentity(
        product_type=product_type,
        keywords=keywords,
        broad_term=broad,
        material=_clean(material) if material else (_clean(out.material) if out.material else fb.material),
        style_tags=tags or fb.style_tags,
    )


def identify_product(
    llm: LLM | None,
    lens: list[Listing],
    *,
    hint: str | None,
    material: str | None,
    category: dict[str, Any],
) -> ProductIdentity:
    """Derive product type, 3-5 retail keywords and a broad Trends term from Lens titles
    (+ the owner's hint). Fallback without LLM: hint / most frequent category seed keyword
    matching the titles."""
    titles = list(dict.fromkeys(item.title.strip() for item in lens if item.title and item.title.strip()))[:MAX_TITLES]
    fb = fallback_identity(titles, hint=hint, material=material, category=category)
    if not titles and not hint:
        return fb

    def with_llm(llm: LLM) -> ProductIdentity:
        payload = {
            "exporter_description": hint,
            "exporter_material": material,
            "lens_titles": titles,
            "example_uk_phrases": category.get("seed_keywords", [])[:16],
        }
        user = f"<data>\n{dump(payload)}\n</data>"
        out = llm.parse(task="identify", system=prompts.IDENTIFY, user=user, schema=ProductIdentity, effort="low")
        return _merge(out, fb, material, category.get("broad_terms", {}))

    return run_with_fallback(llm, "identify", with_llm, lambda: fb)
