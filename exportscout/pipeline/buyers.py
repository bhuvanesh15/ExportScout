"""Buyer discovery and enrichment (plan §5 steps 6-7).

Discovery is free: it merges merchants, brands, web results and Maps places already fetched
into ``BuyerCandidate``s. Enrichment spends 2-3 credits per candidate.

OWNER: agent A.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from exportscout.models import (
    BuyerCandidate,
    BuyerKind,
    BuyerSignal,
    EvidenceStore,
    Listing,
    Place,
    ProductDetail,
    ProductIdentity,
    SignalKind,
    WebResult,
)
from exportscout.serp.client import CacheMiss, SerpApiError, SerpClient
from exportscout.serp.engines import ads_activity, domain_of, news_articles, parse_datetime, web_results

# Public suffixes with two labels, so "shop.nkuku.co.uk" dedupes as "nkuku.co.uk".
_SECOND_LEVEL = {
    "co.uk", "org.uk", "ltd.uk", "plc.uk", "me.uk", "net.uk", "ac.uk", "gov.uk",
    "com.au", "net.au", "org.au", "co.nz", "co.in", "net.in", "org.in", "firm.in",
    "co.za", "com.sg", "com.hk", "co.jp", "com.br",
}
# Store hosts where every subdomain is a different business.
_HOSTED = ("myshopify.com", "wixsite.com", "squarespace.com", "business.site", "wordpress.com", "blogspot.com")
# Never buyers, on top of category["marketplaces"].
_ALWAYS_SKIP = (
    "wikipedia.org", "gov.uk", "linkedin.com", "trustpilot.com", "yell.com", "tiktok.com", "twitter.com", "x.com",
    "imdb.com", "bbc.co.uk", "theguardian.com", "dailymail.co.uk", "independent.co.uk", "telegraph.co.uk", "companieshouse.gov.uk",
)  # fmt: skip
_NO_BRAND = {"generic", "unbranded", "unknown", "na", "none", "nobrand", "brandless", "various"}
_COMPANY_SUFFIX = re.compile(r"(?:[\s,]+(?:ltd|limited|plc|llp|inc|uk|&\s*co|and\s+co)\.?)+$", re.I)

ENGINE_LABELS = {
    "google_lens": "Google Lens",
    "google_shopping": "Google Shopping",
    "amazon": "Amazon",
    "google": "Google Search",
    "google_maps": "Google Maps",
}
_LISTING_ENGINE = {"lens": "google_lens", "google_shopping": "google_shopping", "amazon": "amazon"}

_TRADE_TEXT = re.compile(r"\b(?:trade|wholesale|wholesaler|stockists?)\b", re.I)
_TRADE_PATH = re.compile(r"/(?:trade|wholesale|stockist)", re.I)
_INDIA_TEXT = re.compile(
    r"\b(?:hand[\s-]?(?:made|crafted)\s+in\s+india|made\s+in\s+india|crafted\s+in\s+india|sourced\s+from\s+india"
    r"|indian\s+(?:artisans?|craftsm[ae]n)|artisans?\s+(?:of|from|in)\s+india)\b",
    re.I,
)
NEWS_RECENT_DAYS = 180
MAX_NEWS_SIGNALS = 2
MAX_SEEN_TEXTS = 40


# --------------------------------------------------------------------------- names and domains


def normalize_name(name: str | None) -> str:
    """Dedupe key for a business name: "Graham & Green Ltd" -> "grahamandgreen"."""
    if not name:
        return ""
    name = _COMPANY_SUFFIX.sub("", name.strip())
    return re.sub(r"[^a-z0-9]", "", name.lower().replace("&", "and"))


def site_domain(host: str | None) -> str | None:
    """Registrable domain used as the dedupe key: "shop.nkuku.com" -> "nkuku.com"."""
    if not host:
        return None
    host = host.lower().removeprefix("www.")
    if any(host.endswith("." + h) for h in _HOSTED):
        return host
    parts = host.split(".")
    n = 3 if len(parts) >= 3 and ".".join(parts[-2:]) in _SECOND_LEVEL else 2
    return ".".join(parts[-n:])


def _label(domain: str) -> str:
    return re.sub(r"[^a-z0-9]", "", domain.split(".")[0])


def _in_domains(host: str, domains: Iterable[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _first_word(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", name.lower())
    return words[0] if words else ""


def _is_marketplace_name(name: str, marketplaces: Iterable[str]) -> bool:
    norm, first = normalize_name(name), _first_word(name)
    for d in marketplaces:
        label = _label(d)
        if norm in (label, re.sub(r"[^a-z0-9]", "", d)) or first == label:
            return True
    return False


def _is_giant(name: str, domain: str | None, giants: Iterable[str]) -> bool:
    giants = list(giants)
    if domain and _in_domains(domain, giants):
        return True
    norm = normalize_name(name)
    for d in giants:
        label = _label(d)
        if norm in (label, re.sub(r"[^a-z0-9]", "", d)) or (len(label) >= 6 and norm.startswith(label)):
            return True
    return False


def _name_from_title(title: str, domain: str) -> str | None:
    """The part of a page title that names the site: "Brass Lanterns | Graham & Green" -> "Graham & Green"."""
    label = _label(domain)
    for part in re.split(r"\s+[|–—\-·]\s+|\s*\|\s*|:\s+", title):
        part = part.strip()
        norm = normalize_name(part)
        if len(norm) >= 3 and (norm in label or (len(label) >= 5 and label in norm)):
            return part
    return None


def _pretty_label(domain: str) -> str:
    """ "the-lantern-company.co.uk" -> "The Lantern Company"."""
    return " ".join(w.capitalize() for w in re.split(r"[-_]", domain.split(".")[0]) if w)


# --------------------------------------------------------------------------- category matching


def _product_phrases(product: ProductIdentity | None) -> list[str]:
    if product is None:
        return []
    phrases = [product.product_type, *product.keywords]
    words = product.product_type.split()
    if words and len(words[-1]) >= 4:
        phrases.append(words[-1])  # head noun: "hurricane lantern" -> "lantern"
    out = list(dict.fromkeys(p.lower().strip() for p in phrases if p and p.strip()))
    return sorted(out, key=len, reverse=True)


def _category_hit(text: str | None, phrases: list[str]) -> str | None:
    if not text:
        return None
    low = text.lower()
    for p in phrases:
        if re.search(r"\b" + re.escape(p) + r"(?:e?s)?\b", low):
            return p
    return None


# --------------------------------------------------------------------------- discovery


def discovery_queries(product: ProductIdentity, market: dict[str, Any], category: dict[str, Any]) -> list[str]:
    """Google queries that surface buyers for this product (from category["buyer_queries"] templates)."""
    kw = product.keywords[0] if product.keywords else product.product_type
    country = market.get("short_label") or market.get("label") or ""
    out = []
    for template in category.get("buyer_queries") or []:
        q = str(template).replace("{kw}", kw).replace("{country}", country)
        out.append(" ".join(q.split()))
    return list(dict.fromkeys(q for q in out if q))


@dataclass
class _Mention:
    engine: str
    evidence_id: str
    name: str | None = None  # explicit: merchant, brand or Maps title
    derived_name: str | None = None  # guessed from a web page title
    host: str | None = None
    domain: str | None = None
    texts: list[str] = field(default_factory=list)
    price: float | None = None
    currency: str | None = None
    reviews: int | None = None
    place: Place | None = None


def _mentions(
    listings: list[Listing], web: list[WebResult], places: list[Place], products: Iterable[ProductDetail]
) -> list[_Mention]:
    out: list[_Mention] = []
    for item in listings:
        engine = _LISTING_ENGINE.get(item.channel)
        if engine is None:  # eBay sellers are not buyers
            continue
        name = item.brand if item.channel == "amazon" else item.merchant
        host = domain_of(item.url) if item.channel == "lens" else None
        if not name and not host:
            continue
        out.append(
            _Mention(
                engine,
                item.evidence_id,
                name=name,
                host=host,
                texts=[item.title],
                price=item.price,
                currency=item.currency,
                reviews=item.reviews,
            )
        )
    for p in products:
        if p.brand:
            out.append(
                _Mention("amazon", p.evidence_id, name=p.brand, texts=[p.title or ""], price=p.price, currency=p.currency)
            )
    for w in web:
        out.append(_Mention("google", w.evidence_id, host=w.domain, texts=[w.title, w.snippet or ""]))
    for pl in places:
        out.append(
            _Mention(
                "google_maps",
                pl.evidence_id,
                name=pl.title,
                host=domain_of(pl.website),
                texts=[pl.title, pl.type or ""],
                reviews=pl.reviews,
                place=pl,
            )
        )
    return out


def _clean_mention(m: _Mention, excluded: list[str], marketplaces: list[str]) -> _Mention | None:
    """Drop marketplace domains and marketplace names; None if nothing usable is left."""
    if m.host and _in_domains(m.host, excluded):
        if m.engine == "google":
            return None
        m.host = None
    m.domain = site_domain(m.host)
    if m.engine == "google" and m.domain:
        m.derived_name = _name_from_title(m.texts[0], m.domain)
    if m.name and not normalize_name(m.name):
        m.name = None
    if m.name and (_is_marketplace_name(m.name, marketplaces) or normalize_name(m.name) in _NO_BRAND):
        return None
    if not m.name and not m.domain:
        return None
    return m


def _match_domain(key: str, groups: dict[str, list[_Mention]], names: dict[str, set[str]]) -> str | None:
    for d in groups:
        if key in names[d]:
            return d
    best: tuple[int, str] | None = None
    for d in groups:
        label = _label(d)
        if (len(key) >= 4 and key in label) or (len(label) >= 5 and label in key):
            gap = abs(len(label) - len(key))
            if best is None or gap < best[0]:
                best = (gap, d)
    return best[1] if best else None


def discover_candidates(
    *,
    listings: list[Listing],
    web: list[WebResult],
    places: list[Place],
    category: dict[str, Any],
    products: Iterable[ProductDetail] = (),
    product: ProductIdentity | None = None,
    currency: str | None = None,
) -> list[BuyerCandidate]:
    """Merge merchants/brands from listings, web results and Maps places into candidates.

    Dedupe by domain (fall back to normalised name). Skip marketplaces (category["marketplaces"]).
    Flag giants (category["giants"]). Add a "multi_engine" signal when seen in 2+ engines.
    ``products`` adds Amazon brands; ``product`` enables "category_match" signals;
    ``currency`` keeps only listing prices in that currency (default: the most common one).
    """
    marketplaces = list(category.get("marketplaces") or [])
    giants = list(category.get("giants") or [])
    excluded = marketplaces + list(_ALWAYS_SKIP)
    if currency is None:
        counts = Counter(l.currency for l in listings if l.price is not None and l.currency)
        currency = counts.most_common(1)[0][0] if counts else None

    by_domain: dict[str, list[_Mention]] = {}
    name_only: list[_Mention] = []
    for raw in _mentions(listings, web, places, products):
        m = _clean_mention(raw, excluded, marketplaces)
        if m is None:
            continue
        if m.domain:
            by_domain.setdefault(m.domain, []).append(m)
        else:
            name_only.append(m)

    domain_names = {
        d: {normalize_name(x) for m in ms for x in (m.name, m.derived_name) if x} for d, ms in by_domain.items()
    }
    by_name: dict[str, list[_Mention]] = {}
    for m in name_only:
        key = normalize_name(m.name)
        target = _match_domain(key, by_domain, domain_names)
        if target:
            by_domain[target].append(m)
        else:
            by_name.setdefault(key, []).append(m)

    phrases = _product_phrases(product)
    groups = [(d, ms) for d, ms in by_domain.items()] + [(None, ms) for ms in by_name.values()]
    return [_build(ms, domain, phrases=phrases, currency=currency, giants=giants) for domain, ms in groups]


def _pick_name(ms: list[_Mention], domain: str | None) -> str:
    explicit = Counter(m.name.strip() for m in ms if m.name and m.name.strip())
    if explicit:
        top = max(explicit.values())
        return next(n for n in (m.name.strip() for m in ms if m.name) if explicit[n] == top)
    derived = next((m.derived_name for m in ms if m.derived_name), None)
    return derived or (_pretty_label(domain) if domain else "Unknown")


def _guess_kind(ms: list[_Mention], places: list[Place]) -> BuyerKind:
    web_titles = (m.texts[0] for m in ms if m.engine == "google" and m.texts)
    words = " ".join([*(m.name or "" for m in ms), *(p.type or "" for p in places), *web_titles]).lower()
    if "wholesal" in words or "distributor" in words:
        return "wholesaler"
    if "import" in words:
        return "importer"
    engines = {m.engine for m in ms}
    if places or engines & {"google_lens", "google_shopping"}:
        return "retailer"
    if engines == {"amazon"}:
        return "online_brand"
    return "unknown"


def _build(
    ms: list[_Mention], domain: str | None, *, phrases: list[str], currency: str | None, giants: list[str]
) -> BuyerCandidate:
    name = _pick_name(ms, domain)
    sources = list(dict.fromkeys(m.engine for m in ms))
    texts = list(dict.fromkeys(t.strip() for m in ms for t in m.texts if t and t.strip()))[:MAX_SEEN_TEXTS]
    prices = [m.price for m in ms if m.price is not None and (currency is None or m.currency == currency)]
    places: list[Place] = []
    seen_places: set[str] = set()
    for m in ms:
        if m.place is not None:
            key = m.place.data_id or f"{m.place.title}|{m.place.address}"
            if key not in seen_places:
                seen_places.add(key)
                places.append(m.place)
    place_mentions = [m for m in ms if m.place is not None]

    signals: list[BuyerSignal] = []

    def add(kind: SignalKind, detail: str, eid: str) -> None:
        signals.append(BuyerSignal(kind=kind, detail=detail, evidence_id=eid))

    if len(sources) >= 2:
        second = next(m for m in ms if m.engine == sources[1])
        add("multi_engine", "Seen in " + ", ".join(ENGINE_LABELS.get(s, s) for s in sources), second.evidence_id)
    hit = next(((m, t, p) for m in ms for t in m.texts if (p := _category_hit(t, phrases))), None)
    if hit:
        m, text, phrase = hit
        add("category_match", f'Sells "{phrase}": {text[:100]}', m.evidence_id)

    website = None
    web_src = next((m for m in place_mentions if m.domain and m.place and m.place.website), None)
    if web_src is not None:
        website = web_src.place.website
    else:
        web_src = next((m for m in ms if m.host), None)
        website = f"https://{web_src.host}" if web_src else None
    if website and web_src:
        add("website", f"Website: {domain or website}", web_src.evidence_id)
    phone_src = next((m for m in place_mentions if m.place and m.place.phone), None)
    phone = phone_src.place.phone if phone_src and phone_src.place else None
    if phone and phone_src:
        add("phone", f"Phone on Google Maps: {phone}", phone_src.evidence_id)

    place_reviews = [p.reviews for p in places if p.reviews is not None]
    other_reviews = [m.reviews for m in ms if m.reviews is not None and m.engine == "google_shopping"]
    review_count = max(place_reviews) if place_reviews else (max(other_reviews) if other_reviews else None)
    first_place = places[0] if places else None
    return BuyerCandidate(
        name=name,
        domain=domain,
        kind=_guess_kind(ms, places),
        city=first_place.city if first_place else None,
        website=website,
        phone=phone,
        address=first_place.address if first_place else None,
        sources=sources,
        seen_texts=texts,
        listing_prices=prices,
        review_count=review_count,
        locations_count=len(places),
        is_giant=_is_giant(name, domain, giants),
        signals=signals,
    )


# --------------------------------------------------------------------------- ranking and enrichment


def prior_score(candidate: BuyerCandidate) -> float:
    """Cheap score from discovery data only, to order enrichment spend."""
    kinds = {s.kind for s in candidate.signals}
    score = 2.0 * max(len(set(candidate.sources)) - 1, 0)
    score += 2.0 if "category_match" in kinds else 0.0
    score += 1.5 if "google_lens" in candidate.sources else 0.0  # sells a look-alike
    score += 1.0 if candidate.listing_prices else 0.0
    score += 1.0 if candidate.kind in ("wholesaler", "importer") else 0.0
    score += 0.5 if candidate.phone else 0.0
    score += 0.5 if candidate.locations_count else 0.0
    return score


def rank_for_enrichment(candidates: list[BuyerCandidate], k: int) -> list[BuyerCandidate]:
    """Pick the K most promising candidates to spend enrichment credits on (cheap prior score).
    Giants, marketplaces and non-buyers are left out; candidates without a domain get one
    looked up during enrichment."""
    eligible = [c for c in candidates if not c.is_giant and c.kind not in ("marketplace", "not_a_buyer")]
    return sorted(eligible, key=prior_score, reverse=True)[: max(k, 0)]


def site_query(domain: str) -> str:
    return f'site:{domain} trade OR wholesale OR "made in India" OR "handmade in India"'


def name_query(name: str, product: ProductIdentity) -> str:
    """Finds a merchant's own website, e.g. "Kayu Home UK" -> kayuhome.co.uk."""
    return f"{name} UK"


def resolve_domain(name: str, results: list[WebResult]) -> str | None:
    """The first result domain whose name matches the business name ("Kayu Home" -> kayuhome.co.uk)."""
    key = normalize_name(name)
    if len(key) < 3:
        return None
    for r in results:
        domain = site_domain(r.domain)
        if not domain or _in_domains(domain, _ALWAYS_SKIP):
            continue
        label = _label(domain)
        # "turquoise.eu" must not claim "Turquoise Living": a shorter label has to cover most of the name.
        if len(label) >= 3 and (key in label or (label in key and len(label) >= 0.75 * len(key))):
            return domain
    return None


def enrich(
    client: SerpClient,
    ev: EvidenceStore,
    market: dict[str, Any],
    candidate: BuyerCandidate,
    product: ProductIdentity,
    *,
    with_news: bool = False,
) -> BuyerCandidate:
    """Spend 2-3 credits on one candidate: site search for trade/wholesale page and
    "handmade/made in India", Ads Transparency activity, optionally News. Returns an
    updated copy with new signals and enriched=True.

    A failed search (SerpApiError, or CacheMiss in Demo Mode) skips that check;
    BudgetExceeded propagates."""
    signals = list(candidate.signals)
    texts = list(candidate.seen_texts)
    have = {(s.kind, s.evidence_id) for s in signals}

    def add(kind: SignalKind, detail: str, eid: str) -> None:
        if (kind, eid) not in have:
            have.add((kind, eid))
            signals.append(BuyerSignal(kind=kind, detail=detail, evidence_id=eid))

    domain = candidate.domain
    ad_creatives = candidate.ad_creatives
    phrases = _product_phrases(product)
    website = candidate.website
    try:
        results = web_results(client, ev, market, site_query(domain) if domain else name_query(candidate.name, product))
    except (SerpApiError, CacheMiss):
        results = []
    if not domain:
        # Shopping merchants come without a website: find it from the same search.
        domain = resolve_domain(candidate.name, results)
        if domain:
            website = website or f"https://{domain}"
            match = next(r for r in results if _in_domains(r.domain, [domain]))
            add("website", f"Website: {domain}", match.evidence_id)
    if domain:
        found: set[str] = set()
        for r in results:
            if not _in_domains(r.domain, [domain]):
                continue
            text = " ".join(filter(None, [r.title, r.snippet]))
            texts += [t for t in (r.title, r.snippet) if t]
            if "trade_page" not in found and (_TRADE_TEXT.search(text) or _TRADE_PATH.search(r.link)):
                found.add("trade_page")
                add("trade_page", f"Trade/wholesale page: {r.title}", r.evidence_id)
            m = _INDIA_TEXT.search(text)
            if "india_sourcing" not in found and m:
                found.add("india_sourcing")
                add("india_sourcing", f'Site says "{" ".join(m.group(0).split())}": {r.title}', r.evidence_id)
            phrase = _category_hit(text, phrases)
            if "category_match" not in found and phrase:
                found.add("category_match")
                add("category_match", f'Site lists "{phrase}": {r.title}', r.evidence_id)
        try:
            ads = ads_activity(client, ev, market, domain)
        except (SerpApiError, CacheMiss):
            pass
        else:
            ad_creatives = ads.total_creatives if ads else 0
            if ads and ads.active_last_30d > 0:
                region = market.get("short_label") or ""
                detail = f"{ads.total_creatives} {region} ad creatives, last shown {ads.last_shown or '?'}"
                add("ads_active", " ".join(detail.split()), ads.evidence_id)
    if with_news:
        try:
            news = news_articles(client, ev, market, f'"{candidate.name}"')
        except (SerpApiError, CacheMiss):
            news = []
        key = normalize_name(candidate.name)
        added = 0
        for item in news:
            if added >= MAX_NEWS_SIGNALS:
                break
            if not key or key not in normalize_name(f"{item.title} {item.snippet or ''}"):
                continue
            record = ev.get(item.evidence_id)
            ref = parse_datetime(record.fetched_at) if record else None
            ref = ref or datetime.now(timezone.utc)
            when = parse_datetime(item.date, ref=ref)
            if when is None or ref - when > timedelta(days=NEWS_RECENT_DAYS):
                continue
            detail = f"News: {item.title}" + f" ({', '.join(filter(None, [item.source, when.date().isoformat()]))})"
            add("news", detail, item.evidence_id)
            added += 1
    return candidate.model_copy(
        update={
            "signals": signals,
            "seen_texts": list(dict.fromkeys(texts))[:MAX_SEEN_TEXTS],
            "ad_creatives": ad_creatives,
            "domain": domain,
            "website": website,
            "enriched": True,
        }
    )
