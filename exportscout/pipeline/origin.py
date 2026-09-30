"""Country-of-origin shares for competing products (plan §5 step 4). Deterministic; no I/O.

OWNER: agent B.
"""
from __future__ import annotations

import re

from exportscout.models import Listing, OriginShare, ProductDetail

# "India"/"Indian" but not "Indiana".
_INDIA = re.compile(r"\bindia(n)?\b", re.IGNORECASE)
_CHINA = re.compile(r"\bchin(a|ese)\b|\bprc\b|people'?s republic(?! of bangladesh)", re.IGNORECASE)
_PLACEHOLDERS = {
    "",
    "not specified",
    "unspecified",
    "unknown",
    "n/a",
    "na",
    "none",
    "null",
    "not applicable",
    "not available",
    "see description",
    "-",
}


def _is_placeholder(value: str) -> bool:
    return value.strip().strip(".").lower() in _PLACEHOLDERS


def mentions_india(text: str | None) -> bool:
    """True if ``text`` names India ("India", "Indian"; not "Indiana")."""
    return bool(text and _INDIA.search(text))


def classify_origin(product: ProductDetail) -> str:
    """"india" | "china" | "other" | "unknown", from the origin field, else the text signal.

    1. origin field: India -> india; China / PRC / People's Republic -> china; any other real value -> other
    2. text signal names India -> india
    3. otherwise unknown
    """
    origin = (product.origin or "").replace("’", "'")
    if not _is_placeholder(origin):
        if mentions_india(origin):
            return "india"
        if _CHINA.search(origin):
            return "china"
        return "other"
    if mentions_india(product.origin_text_signal):
        return "india"
    return "unknown"


def origin_shares(products: list[ProductDetail], ebay: list[Listing]) -> OriginShare:
    """Count Amazon products by origin and eBay listings located in India.

    Products are deduplicated by ASIN; only ``channel == "ebay"`` listings are counted.
    """
    counts = {"india": 0, "china": 0, "other": 0, "unknown": 0}
    examples: list[str] = []
    evidence: list[str] = []
    seen: set[str] = set()
    for product in products:
        if product.asin in seen:
            continue
        seen.add(product.asin)
        kind = classify_origin(product)
        counts[kind] += 1
        evidence.append(product.evidence_id)
        if kind == "india":
            examples.append(product.evidence_id)

    ebay_total = ebay_from_india = 0
    for listing in ebay:
        if listing.channel != "ebay":
            continue
        ebay_total += 1
        evidence.append(listing.evidence_id)
        if mentions_india(listing.location):
            ebay_from_india += 1
            examples.append(listing.evidence_id)

    return OriginShare(
        checked=len(seen),
        **counts,
        ebay_total=ebay_total,
        ebay_from_india=ebay_from_india,
        india_examples=list(dict.fromkeys(examples)),
        evidence_ids=list(dict.fromkeys(evidence)),
    )
