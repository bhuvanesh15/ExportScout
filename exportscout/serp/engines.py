"""One function per SerpApi engine: run the search through SerpClient, normalise the JSON
into models, and record every result used as Evidence.

Every function takes ``client`` (SerpClient), ``ev`` (EvidenceStore) and ``market``
(a dict from ``exportscout.config.market(code)``). Normalisers must tolerate missing
fields and return empty lists rather than raise on odd responses. A SerpApi "hasn't
returned any results" answer (data with an ``error`` key) is treated as empty.

OWNER: agent A.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.parse import quote_plus, urlencode, urlsplit

from exportscout.models import (
    AdsActivity,
    EvidenceStore,
    JobPosting,
    Listing,
    NewsItem,
    Place,
    ProductDetail,
    RegionInterest,
    ReviewSnippet,
    TrendsSeries,
    WebResult,
)
from exportscout.serp.client import SerpClient, SerpResponse

Market = dict[str, Any]

MAX_REVIEWS = 30
TRENDS_DATE = "today 5-y"

# --------------------------------------------------------------------------- parsing helpers

# Checked in order, so longer symbols come before the "$" they contain.
_CURRENCY_SYMBOLS = (
    ("US$", "USD"),
    ("US $", "USD"),
    ("CA$", "CAD"),
    ("C$", "CAD"),
    ("AU$", "AUD"),
    ("AU $", "AUD"),
    ("A$", "AUD"),
    ("£", "GBP"),
    ("€", "EUR"),
    ("₹", "INR"),
    ("$", "USD"),
    ("¥", "JPY"),
)
_ISO_CODES = {"GBP", "USD", "EUR", "INR", "AUD", "CAD", "JPY", "CNY", "AED", "CHF", "SEK", "NZD"}
_ISO_IN_TEXT = re.compile(r"\b([A-Z]{3})\b")
_NUMBER = re.compile(r"\d[\d.,]*")
_DECIMAL_COMMA = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{1,2}")
_COUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([KkMm])?(?![A-Za-z])")
_INVISIBLE = re.compile(r"[‎‏‪-‮﻿]")
_RELATIVE = re.compile(r"(\d+)\s+(minute|min|hour|day|week|month|year)s?\s+ago", re.I)
_US_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%b %Y", "%B %Y", "%Y-%m-%d")
_RELATIVE_UNITS = {"minute": 1 / 1440, "min": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}


def currency_code(text: Any, default: str | None = None) -> str | None:
    """ISO currency code from a symbol or code: "£24.99" -> "GBP", "€" -> "EUR", "GBP" -> "GBP"."""
    s = _str(text)
    if s is None:
        return default
    if s.upper() in _ISO_CODES:
        return s.upper()
    m = _ISO_IN_TEXT.search(s)
    if m and m.group(1) in _ISO_CODES:
        return m.group(1)
    for symbol, code in _CURRENCY_SYMBOLS:
        if symbol in s:
            return code
    return default


def parse_price(value: Any) -> float | None:
    """Number from 24.99, "£24.99", "£1,299.00", "24,99 €" or "4.5 out of 5"; the first number wins."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    m = _NUMBER.search(value)
    if not m:
        return None
    num = m.group(0).rstrip(".,")
    if _DECIMAL_COMMA.fullmatch(num):
        num = num.replace(".", "").replace(",", ".")
    else:
        num = num.replace(",", "")
    try:
        return float(num)
    except ValueError:
        return None


def parse_count(value: Any) -> int | None:
    """Int from 1234, "1,234", "(1.2K)", "1K+ bought in past month" (1000) or "<1" (0)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = _str(value)
    if s is None:
        return None
    if s.startswith("<"):
        return 0
    m = _COUNT.search(s)
    if not m:
        return None
    mult = {"k": 1_000, "m": 1_000_000}.get((m.group(2) or "").lower(), 1)
    try:
        return int(round(float(m.group(1).replace(",", "")) * mult))
    except ValueError:
        return None


def parse_datetime(value: Any, ref: datetime | None = None) -> datetime | None:
    """UTC datetime from epoch seconds or ms (number or digit string), ISO text, "MM/DD/YYYY, ...",
    "Sep 20, 2026", or "3 days ago" (relative to ``ref``). None if it can't be read."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    s = _str(value)
    if s is None:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return _from_epoch(float(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    m = _US_DATE.search(s)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)), tzinfo=timezone.utc)
        except ValueError:
            return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    if ref is not None:
        if s.lower() == "yesterday":
            return ref - timedelta(days=1)
        m = _RELATIVE.search(s)
        if m:
            return ref - timedelta(days=int(m.group(1)) * _RELATIVE_UNITS[m.group(2).lower()])
    return None


def domain_of(url: Any) -> str | None:
    """Host of a URL without "www.": "https://www.grahamandgreen.co.uk/x" -> "grahamandgreen.co.uk"."""
    s = _str(url)
    if s is None:
        return None
    if "://" not in s:
        s = "//" + s
    try:
        host = urlsplit(s).hostname
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _from_epoch(seconds: float) -> datetime | None:
    if seconds > 1e11:  # milliseconds
        seconds /= 1000
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _iso_date(dt: datetime | None) -> str | None:
    return dt.date().isoformat() if dt else None


def _str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(_INVISIBLE.sub("", value).split())
    return value or None


def _num(value: Any) -> float | None:
    return parse_price(value)


def _join(*parts: Any, sep: str = " · ") -> str | None:
    text = sep.join(p for p in (_str(x) for x in parts) if p)
    return text or None


def _data(resp: SerpResponse) -> dict[str, Any]:
    """The response JSON, or {} for SerpApi's "no results" error answer."""
    data = resp.data
    if not isinstance(data, dict) or data.get("error"):
        return {}
    return data


def _list(obj: Any, key: str) -> list[dict[str, Any]]:
    items = obj.get(key) if isinstance(obj, dict) else None
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, dict)]


def _dict(obj: Any, key: str) -> dict[str, Any]:
    value = obj.get(key) if isinstance(obj, dict) else None
    return value if isinstance(value, dict) else {}


def _strings(obj: Any, depth: int = 0) -> Iterator[str]:
    if depth > 6:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v, depth + 1)


def _price_parts(value: Any) -> tuple[float | None, str | None, str | None]:
    """(amount, raw text, currency hint) from a price field: 24.99, "£24.99",
    {value, extracted_value, currency}, {raw, extracted} or {from: {...}, to: {...}}."""
    if isinstance(value, dict):
        if isinstance(value.get("from"), dict):
            return _price_parts(value["from"])
        raw = _str(value.get("raw")) or _str(value.get("value"))
        amount = None
        for key in ("extracted_value", "extracted", "value", "raw"):
            amount = _num(value.get(key))
            if amount is not None:
                break
        return amount, raw, _str(value.get("currency"))
    if isinstance(value, str):
        return parse_price(value), _str(value), None
    return _num(value), None, None


def _currency_of(hint: str | None, raw: str | None, default: str | None) -> str | None:
    """ISO code from an explicit currency field, else the price text, else ``default``.
    An unknown explicit symbol is kept as given, so it never passes as the market currency."""
    return currency_code(hint) or currency_code(raw) or hint or default


def _google(market: Market, *keys: str) -> dict[str, Any]:
    g = market.get("google") or {}
    return {k: g.get(k) for k in keys}


def _seller_name(seller: Any) -> str | None:
    if isinstance(seller, dict):
        return _str(seller.get("username")) or _str(seller.get("name"))
    return _str(seller)


# --------------------------------------------------------------------------- shopping channels

_LENS_BLOCKS = ("visual_matches", "products", "exact_matches")


def lens_matches(
    client: SerpClient,
    ev: EvidenceStore,
    market: Market,
    *,
    image: str | bytes | None = None,
    url: str | None = None,
    q: str | None = None,
    type: str = "all",
) -> list[Listing]:
    """Google Lens look-alikes (visual_matches + products), priced or not, channel="lens"."""
    lens = market.get("lens") or {}
    resp = client.lens(image, url=url, country=lens.get("country"), hl=lens.get("hl"), type=type, q=q)
    data = _data(resp)
    out: list[Listing] = []
    seen: set[str] = set()
    for block in _LENS_BLOCKS:
        for i, item in enumerate(_list(data, block)):
            title = _str(item.get("title"))
            if title is None:
                continue
            link = _str(item.get("link"))
            if (link or title) in seen:
                continue
            seen.add(link or title)
            amount, raw, hint = _price_parts(item.get("price"))
            currency = _currency_of(hint, raw, market.get("currency"))
            merchant = _str(item.get("source"))
            index = i if block == "visual_matches" else f"{block}.{i}"
            eid = ev.add(resp, index, title=title, url=link, snippet=_join(raw, merchant))
            out.append(
                Listing(
                    channel="lens",
                    title=title,
                    price=amount,
                    currency=currency if amount is not None else None,
                    url=link,
                    merchant=merchant,
                    rating=_num(item.get("rating")),
                    reviews=parse_count(item.get("reviews")),
                    thumbnail=_str(item.get("thumbnail")),
                    evidence_id=eid,
                )
            )
    return out


def shopping_listings(client: SerpClient, ev: EvidenceStore, market: Market, q: str) -> list[Listing]:
    """Google Shopping results in the market (gl/hl/google_domain/location from market["google"])."""
    resp = client.search("google_shopping", q=q, **_google(market, "gl", "hl", "google_domain", "location"))
    data = _data(resp)
    items: list[tuple[int | str, dict[str, Any]]] = list(enumerate(_list(data, "shopping_results")))
    if not items:
        for g, group in enumerate(_list(data, "categorized_shopping_results")):
            items += [(f"c{g}.{i}", item) for i, item in enumerate(_list(group, "shopping_results"))]
    out: list[Listing] = []
    for index, item in items:
        title = _str(item.get("title"))
        if title is None:
            continue
        link = _str(item.get("product_link")) or _str(item.get("link"))
        amount = _num(item.get("extracted_price"))
        _, raw, _ = _price_parts(item.get("price"))
        if amount is None:
            amount = parse_price(raw)
        merchant = _str(item.get("source"))
        eid = ev.add(resp, index, title=title, url=link, snippet=_join(raw, merchant))
        out.append(
            Listing(
                channel="google_shopping",
                title=title,
                price=amount,
                currency=currency_code(raw, market.get("currency")) if amount is not None else None,
                url=link,
                merchant=merchant,
                rating=_num(item.get("rating")),
                reviews=parse_count(item.get("reviews")),
                thumbnail=_str(item.get("thumbnail")),
                evidence_id=eid,
            )
        )
    return out


def amazon_listings(client: SerpClient, ev: EvidenceStore, market: Market, k: str) -> list[Listing]:
    """Amazon search on market["amazon_domain"]; parses bought_last_month to an int."""
    domain = market.get("amazon_domain")
    resp = client.search("amazon", k=k, amazon_domain=domain)
    out: list[Listing] = []
    for i, item in enumerate(_list(_data(resp), "organic_results")):
        title = _str(item.get("title"))
        if title is None:
            continue
        asin = _str(item.get("asin"))
        link = _str(item.get("link_clean")) or _str(item.get("link"))
        if link is None and asin and domain:
            link = f"https://www.{domain}/dp/{asin}"
        amount = _num(item.get("extracted_price"))
        _, raw, _ = _price_parts(item.get("price"))
        if amount is None:
            amount = parse_price(raw)
        if amount is None and _list(item, "prices"):
            amount, raw, _ = _price_parts(_list(item, "prices")[0])
        bought = parse_count(item.get("bought_last_month"))
        eid = ev.add(resp, i, title=title, url=link, snippet=_join(raw, _str(item.get("bought_last_month"))))
        out.append(
            Listing(
                channel="amazon",
                title=title,
                price=amount,
                currency=market.get("currency") if amount is not None else None,
                url=link,
                brand=_clean_brand(item.get("brand")),
                rating=_num(item.get("rating")),
                reviews=parse_count(item.get("reviews")),
                bought_last_month=bought,
                asin=asin,
                sponsored=bool(item.get("sponsored")),
                thumbnail=_str(item.get("thumbnail")),
                evidence_id=eid,
            )
        )
    return out


def ebay_listings(client: SerpClient, ev: EvidenceStore, market: Market, q: str) -> list[Listing]:
    """eBay search on market["ebay_domain"]; handles single and from/to price shapes; keeps location."""
    resp = client.search("ebay", _nkw=q, ebay_domain=market.get("ebay_domain"))
    out: list[Listing] = []
    for i, item in enumerate(_list(_data(resp), "organic_results")):
        title = _str(item.get("title"))
        if title is None:
            continue
        amount, raw, hint = _price_parts(item.get("price"))
        if amount is None:
            amount = _num(item.get("extracted_price"))
        currency = _currency_of(hint, raw, market.get("currency"))
        location = _str(item.get("location")) or next(
            (s for s in _strings(item.get("extensions")) if re.search(r"located in|^from\s", s, re.I)), None
        )
        link = _str(item.get("link"))
        merchant = _seller_name(item.get("seller"))
        eid = ev.add(resp, i, title=title, url=link, snippet=_join(raw, location, merchant))
        out.append(
            Listing(
                channel="ebay",
                title=title,
                price=amount,
                currency=currency if amount is not None else None,
                url=link,
                merchant=merchant,
                rating=_num(item.get("rating")),
                reviews=parse_count(item.get("reviews")),
                location=location,
                sponsored=bool(item.get("sponsored")),
                thumbnail=_str(item.get("thumbnail")),
                evidence_id=eid,
            )
        )
    return out


# --------------------------------------------------------------------------- amazon product page

# "origin" but not "original" (e.g. original_price).
_ORIGIN_KEY = re.compile(r"origin(?!al)", re.I)
_ORIGIN_TEXT = re.compile(
    r"\b(?:hand[\s-]?(?:made|crafted)\s+in\s+india|made\s+in\s+india|crafted\s+in\s+india|product\s+of\s+india"
    r"|indian\s+(?:artisans?|craftsm[ae]n)|artisans?\s+(?:of|from|in)\s+india|from\s+india)\b",
    re.I,
)
# Top-level keys that describe other products or metadata, not this one.
_NOT_THIS_PRODUCT = re.compile(r"review|search_|related|similar|compare|sponsored|also|frequently|customers_", re.I)
_REVIEW_TEXT_KEYS = ("text", "body", "review", "content", "snippet")
# AI "customers say" summaries and rating histograms are not individual reviews.
_NOT_A_REVIEW = re.compile(r"summary|customers_say|insight|histogram|breakdown", re.I)


def amazon_product(client: SerpClient, ev: EvidenceStore, market: Market, asin: str) -> ProductDetail:
    """Amazon product page: brand, manufacturer, country of origin (field or text signal), reviews."""
    domain = market.get("amazon_domain")
    resp = client.search("amazon_product", asin=asin, amazon_domain=domain)
    data = _data(resp)
    pr = _dict(data, "product_results")
    details = _details(data.get("product_details"))
    title = _str(pr.get("title")) or _str(data.get("title"))
    brand = _clean_brand(pr.get("brand")) or _clean_brand(details.get("brand"))
    manufacturer = _str(details.get("manufacturer")) or _str(pr.get("manufacturer"))
    origin = _find_origin(details) or _find_origin(pr) or _find_origin(
        {k: v for k, v in data.items() if not _NOT_THIS_PRODUCT.search(str(k))}
    )
    signal = _origin_signal(title, data.get("about_item"), data.get("product_description"), data.get("description"))
    amount = _num(pr.get("extracted_price"))
    if amount is None:
        amount = _price_parts(pr.get("price"))[0]
    link = _str(pr.get("link")) or (f"https://www.{domain}/dp/{asin}" if domain else None)
    eid = ev.add(
        resp,
        0,
        title=title or f"Amazon product {asin}",
        url=link,
        snippet=_join(
            brand and f"Brand: {brand}",
            origin and f"Country of origin: {origin}",
            signal and f'Text says "{signal}"',
        ),
    )
    reviews = []
    for i, (text, rtitle, rating) in enumerate(_find_reviews(data.get("reviews_information"))):
        rid = ev.add(resp, f"review.{i}", title=rtitle or text[:80], url=link, snippet=text)
        reviews.append(ReviewSnippet(text=text, title=rtitle, rating=rating, evidence_id=rid))
    return ProductDetail(
        asin=asin,
        title=title,
        brand=brand,
        manufacturer=manufacturer,
        origin=origin,
        origin_text_signal=signal,
        price=amount,
        currency=market.get("currency") if amount is not None else None,
        rating=_num(pr.get("rating")),
        reviews_count=parse_count(pr.get("reviews")),
        reviews=reviews,
        evidence_id=eid,
    )


def _clean_brand(value: Any) -> str | None:
    """ "Visit the Hosley Store" -> "Hosley"; "Brand: Hosley" -> "Hosley"."""
    if isinstance(value, dict):
        value = value.get("name") or value.get("text")
    s = _str(value)
    if s is None:
        return None
    s = re.sub(r"^(?:visit the\s+)(.+?)(?:\s+store)$", r"\1", s, flags=re.I)
    s = re.sub(r"^brand\s*:\s*", "", s, flags=re.I)
    return s.strip(" :") or None


def _details(value: Any) -> dict[str, Any]:
    """product_details as a dict; also accepts a list of {name, value} rows."""
    if isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    if isinstance(value, list):
        for row in value:
            if isinstance(row, dict):
                name = _str(row.get("name")) or _str(row.get("label")) or _str(row.get("title"))
                if name:
                    out[re.sub(r"\W+", "_", name.lower()).strip("_")] = row.get("value")
    return out


def _origin_value(value: Any) -> str | None:
    s = _str(value)
    if s is None or len(s) > 60 or s.lower().startswith("http"):
        return None
    return s.strip(" :")


def _find_origin(obj: Any, depth: int = 0) -> str | None:
    """First string value under a key (or a {name, value} row) that mentions "origin"."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        label = obj.get("name") or obj.get("label") or obj.get("title")
        if isinstance(label, str) and _ORIGIN_KEY.search(label):
            found = _origin_value(obj.get("value"))
            if found:
                return found
        for k, v in obj.items():
            if isinstance(k, str) and _ORIGIN_KEY.search(k):
                found = _origin_value(v)
                if found:
                    return found
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for v in children:
        if isinstance(v, (dict, list)):
            found = _find_origin(v, depth + 1)
            if found:
                return found
    return None


def _origin_signal(*sources: Any) -> str | None:
    for text in _strings(sources):
        m = _ORIGIN_TEXT.search(text)
        if m:
            return " ".join(m.group(0).split())
    return None


def _find_reviews(obj: Any) -> list[tuple[str, str | None, float | None]]:
    """(text, title, rating) for every review-like dict under ``obj``, deduped, up to MAX_REVIEWS."""
    out: list[tuple[str, str | None, float | None]] = []
    seen: set[str] = set()

    def walk(node: Any, depth: int) -> None:
        if depth > 6 or len(out) >= MAX_REVIEWS:
            return
        if isinstance(node, list):
            for v in node:
                walk(v, depth + 1)
            return
        if not isinstance(node, dict):
            return
        text = next((t for t in (_str(node.get(k)) for k in _REVIEW_TEXT_KEYS) if t), None)
        if text:
            if text not in seen:
                seen.add(text)
                out.append((text, _str(node.get("title")), _num(node.get("rating"))))
            return
        for k, v in node.items():
            if isinstance(v, (dict, list)) and not (isinstance(k, str) and _NOT_A_REVIEW.search(k)):
                walk(v, depth + 1)

    walk(obj, 0)
    return out[:MAX_REVIEWS]


# --------------------------------------------------------------------------- demand


def _trends(client: SerpClient, market: Market, q: str, data_type: str, **extra: Any) -> SerpResponse:
    # trends_cat (a Google Trends category id) keeps e.g. "lantern" from matching "Green Lantern".
    return client.search(
        "google_trends",
        q=q,
        geo=market.get("trends_geo"),
        date=TRENDS_DATE,
        data_type=data_type,
        cat=market.get("trends_cat"),
        **extra,
    )


def _trends_url(market: Market, q: str) -> str:
    return "https://trends.google.com/trends/explore?" + urlencode(
        {"date": TRENDS_DATE, "geo": market.get("trends_geo") or "", "q": q}
    )


def _trends_date(point: dict[str, Any]) -> datetime | None:
    when = parse_datetime(point.get("timestamp"))
    if when is not None:
        return when
    s = _str(point.get("date"))
    if s is None:
        return None
    first = re.split(r"\s+[–-]\s+", s)[0]  # "Sep 25 – Oct 1, 2022" -> "Sep 25"
    if not re.search(r"\d{4}", first):
        year = re.findall(r"\d{4}", s)
        first = f"{first}, {year[-1]}" if year else first
    return parse_datetime(first)


def trends_timeseries(client: SerpClient, ev: EvidenceStore, market: Market, terms: list[str]) -> TrendsSeries | None:
    """Interest over 5 years for up to 5 terms in market["trends_geo"]."""
    terms = list(dict.fromkeys(t.strip() for t in terms if t and t.strip()))[:5]
    if not terms:
        return None
    q = ",".join(terms)
    resp = _trends(client, market, q, "TIMESERIES")
    by_lower = {t.lower(): t for t in terms}
    dates: list[str] = []
    values: dict[str, list[int]] = {t: [] for t in terms}
    for point in _list(_dict(_data(resp), "interest_over_time"), "timeline_data"):
        when = _trends_date(point)
        if when is None:
            continue
        row: dict[str, int] = {}
        for pos, v in enumerate(_list(point, "values")):
            term = by_lower.get((_str(v.get("query")) or "").lower()) or (terms[pos] if pos < len(terms) else None)
            n = parse_count(v.get("extracted_value"))
            if n is None:
                n = parse_count(v.get("value"))
            if term and term not in row:
                row[term] = n or 0
        dates.append(when.date().isoformat())
        for t in terms:
            values[t].append(row.get(t, 0))
    if not dates:
        return None
    eid = ev.add(
        resp,
        0,
        title=f"Google Trends {market.get('trends_geo')}, past 5 years: {', '.join(terms)}",
        url=_trends_url(market, q),
        snippet=f"{len(dates)} points from {dates[0]} to {dates[-1]}",
    )
    return TrendsSeries(terms=terms, dates=dates, values=values, evidence_id=eid)


def trends_regions(
    client: SerpClient, ev: EvidenceStore, market: Market, term: str, *, resolution: str | None = "CITY"
) -> list[RegionInterest]:
    """Interest by region (GEO_MAP_0), strongest first. ``resolution`` is SerpApi's ``region`` param."""
    resp = _trends(client, market, term, "GEO_MAP_0", region=resolution)
    data = _data(resp)
    items = next(
        (found for key in ("interest_by_region", "interest_by_city", "interest_by_subregion") if (found := _list(data, key))),
        [],
    )
    out: list[RegionInterest] = []
    for i, item in enumerate(items):
        name = _str(item.get("location")) or _str(item.get("geo_name")) or _str(item.get("name"))
        value = parse_count(item.get("extracted_value"))
        if value is None:
            value = parse_count(item.get("value"))
        if not name or not value:
            continue
        eid = ev.add(
            resp,
            i,
            title=f'Google Trends: "{term}" interest in {name} = {value}',
            url=_trends_url(market, term),
            snippet=f"Interest by {(resolution or 'region').lower()}, {market.get('trends_geo') or 'Worldwide'}, past 5 years",
        )
        out.append(RegionInterest(region=name, value=value, evidence_id=eid))
    out.sort(key=lambda r: r.value, reverse=True)
    return out


def trends_related(client: SerpClient, ev: EvidenceStore, market: Market, term: str) -> list[str]:
    """Related queries (top + rising). Registers one evidence record (index 0) for the response."""
    resp = _trends(client, market, term, "RELATED_QUERIES")
    related = _dict(_data(resp), "related_queries")
    out: dict[str, str] = {}
    for group in ("top", "rising"):
        for item in _list(related, group):
            q = _str(item.get("query"))
            if q and q.lower() not in out:
                out[q.lower()] = q
    if out:
        ev.add(
            resp,
            0,
            title=f'Google Trends related queries for "{term}"',
            url=_trends_url(market, term),
            snippet=", ".join(list(out.values())[:15]),
        )
    return list(out.values())


def autocomplete(client: SerpClient, ev: EvidenceStore, market: Market, q: str) -> list[str]:
    """Google Autocomplete suggestions: how shoppers in the market phrase the product.
    Registers one evidence record (index 0) for the response."""
    resp = client.search("google_autocomplete", q=q, **_google(market, "gl", "hl"))
    out: dict[str, str] = {}
    suggestions = _data(resp).get("suggestions")
    for item in suggestions if isinstance(suggestions, list) else []:
        value = _str(item.get("value")) if isinstance(item, dict) else _str(item)
        if value and value.lower() not in out:
            out[value.lower()] = value
    if out:
        ev.add(resp, 0, title=f'Google Autocomplete for "{q}"', snippet=", ".join(list(out.values())[:15]))
    return list(out.values())


# --------------------------------------------------------------------------- buyers


def maps_places(
    client: SerpClient, ev: EvidenceStore, market: Market, q: str, *, ll: str, city: str | None = None
) -> list[Place]:
    """Google Maps local results around ``ll`` (e.g. "@51.5072,-0.1276,12z")."""
    resp = client.search("google_maps", q=q, ll=ll, type="search", **_google(market, "hl"))
    data = _data(resp)
    items = _list(data, "local_results")
    if not items and _dict(data, "place_results"):
        items = [_dict(data, "place_results")]
    out: list[Place] = []
    for i, item in enumerate(items):
        title = _str(item.get("title"))
        if title is None:
            continue
        address = _str(item.get("address"))
        phone = _str(item.get("phone"))
        types = item.get("types") if isinstance(item.get("types"), list) else []
        kind = _str(item.get("type")) or next((t for t in (_str(x) for x in types) if t), None)
        maps_url = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(" ".join(filter(None, [title, address])))
        eid = ev.add(resp, i, title=title, url=maps_url, snippet=_join(kind, address, phone))
        out.append(
            Place(
                title=title,
                address=address,
                phone=phone,
                website=_str(item.get("website")),
                rating=_num(item.get("rating")),
                reviews=parse_count(item.get("reviews")),
                type=kind,
                data_id=_str(item.get("data_id")),
                city=city,
                evidence_id=eid,
            )
        )
    return out


def web_results(client: SerpClient, ev: EvidenceStore, market: Market, q: str) -> list[WebResult]:
    """Google organic results in the market."""
    resp = client.search("google", q=q, **_google(market, "gl", "hl", "google_domain"))
    out: list[WebResult] = []
    for i, item in enumerate(_list(_data(resp), "organic_results")):
        title = _str(item.get("title"))
        link = _str(item.get("link"))
        domain = domain_of(link)
        if not (title and link and domain):
            continue
        snippet = _str(item.get("snippet"))
        eid = ev.add(resp, i, title=title, url=link, snippet=snippet)
        out.append(WebResult(title=title, link=link, domain=domain, snippet=snippet, evidence_id=eid))
    return out


def ads_activity(client: SerpClient, ev: EvidenceStore, market: Market, domain: str) -> AdsActivity | None:
    """Google Ads Transparency Center activity for an advertiser domain in market["ads_region"].
    None if no ad creatives are found."""
    resp = client.search("google_ads_transparency_center", text=domain, region=market.get("ads_region"))
    data = _data(resp)
    creatives = _list(data, "ad_creatives")
    if not creatives:
        return None
    fetched = parse_datetime(resp.fetched_at) or datetime.now(timezone.utc)
    advertiser = None
    firsts: list[datetime] = []
    lasts: list[datetime] = []
    active = 0
    for c in creatives:
        adv = c.get("advertiser")
        advertiser = advertiser or _str(adv) or (_str(adv.get("name")) if isinstance(adv, dict) else None)
        first, last = parse_datetime(c.get("first_shown")), parse_datetime(c.get("last_shown"))
        if first:
            firsts.append(first)
        if last:
            lasts.append(last)
            if fetched - last <= timedelta(days=30):
                active += 1
    total = max(parse_count(_dict(data, "search_information").get("total_results")) or 0, len(creatives))
    first_shown, last_shown = _iso_date(min(firsts, default=None)), _iso_date(max(lasts, default=None))
    eid = ev.add(
        resp,
        0,
        title=f"Google Ads Transparency Center: {advertiser or domain}",
        url=f"https://adstransparency.google.com/?domain={domain}",
        snippet=f"{total} ad creatives; first shown {first_shown or '?'}; last shown {last_shown or '?'}; "
        f"{active} shown in the 30 days before {fetched.date().isoformat()}",
    )
    return AdsActivity(
        domain=domain,
        advertiser=advertiser,
        total_creatives=total,
        first_shown=first_shown,
        last_shown=last_shown,
        active_last_30d=active,
        evidence_id=eid,
    )


def news_articles(client: SerpClient, ev: EvidenceStore, market: Market, q: str) -> list[NewsItem]:
    """Google News articles in the market; story clusters are flattened."""
    resp = client.search("google_news", q=q, **_google(market, "gl", "hl"))
    out: list[NewsItem] = []
    seen: set[str] = set()
    for i, item in enumerate(_list(_data(resp), "news_results")):
        entries: list[tuple[int | str, dict[str, Any]]] = []
        if _str(item.get("link")) or not (_list(item, "stories") or _dict(item, "highlight")):
            entries.append((i, item))
        if _dict(item, "highlight"):
            entries.append((f"{i}.h", _dict(item, "highlight")))
        entries += [(f"{i}.{j}", s) for j, s in enumerate(_list(item, "stories"))]
        for index, entry in entries:
            title = _str(entry.get("title"))
            link = _str(entry.get("link"))
            if title is None or (link or title) in seen:
                continue
            seen.add(link or title)
            src = entry.get("source")
            source = _str(src) or (_str(src.get("name")) if isinstance(src, dict) else None)
            date = _str(entry.get("iso_date")) or _str(entry.get("date"))
            snippet = _str(entry.get("snippet"))
            eid = ev.add(resp, index, title=title, url=link, snippet=_join(source, date, snippet))
            out.append(NewsItem(title=title, link=link, source=source, date=date, snippet=snippet, evidence_id=eid))
    return out


# --------------------------------------------------------------------------- jobs

# Roles that choose suppliers; other ads a jobs search returns (sales assistant, ...) are ignored.
_BUYING_ROLE = re.compile(
    r"\b(?:buyer|buying|sourcing|merchandis\w*|product\s+develop\w*|range\s+plann\w*|category\s+manager)\b", re.I
)
_JOB_INDIA = re.compile(r"\b(?:india|indian)\b", re.I)
_JOB_OVERSEAS = re.compile(
    r"\b(?:overseas|far\s+east|asia|sourcing\s+trips?|international\s+suppliers?|global\s+suppliers?"
    r"|factor(?:y|ies)|direct\s+sourcing|supplier\s+visits?|trade\s+(?:fairs?|shows?))\b",
    re.I,
)
_RECRUITER_NAME = re.compile(r"\b(?:recruit\w*|staffing|personnel|resourcing|talent|selection|headhunt\w*)\b", re.I)
_EMAIL = re.compile(r"\S+@\S+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_POSTED = re.compile(r"(\d+)\+?\s*(minute|min|hour|day|week|month)s?\s+ago", re.I)
SNIPPET_CHARS = 80


def _posted_days(text: str | None) -> float | None:
    """ "3 days ago" -> 3.0, "21 hours ago" -> 0.9, "30+ days ago" -> 30.0."""
    m = _POSTED.search(text or "")
    if not m:
        return None
    return round(int(m.group(1)) * _RELATIVE_UNITS[m.group(2).lower()], 1)


def _job_snippet(description: str | None) -> str | None:
    """A short window around the first India (else overseas-sourcing) mention, contacts removed."""
    text = re.sub(r"\s+", " ", description or "").strip()
    m = _JOB_INDIA.search(text) or _JOB_OVERSEAS.search(text)
    if not m:
        return None
    start, end = max(0, m.start() - SNIPPET_CHARS), min(len(text), m.end() + SNIPPET_CHARS)
    # Cut at word boundaries, so the window never starts or ends mid-word.
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    window = _PHONE.sub("[phone]", _EMAIL.sub("[email]", text[start:end]))
    return ("…" if start else "") + window.strip() + ("…" if end < len(text) else "")


def job_postings(client: SerpClient, ev: EvidenceStore, market: Market, q: str) -> list[JobPosting]:
    """Google Jobs ads for buying/sourcing roles in the market (``market["jobs_location"]``)."""
    resp = client.search("google_jobs", q=q, location=market.get("jobs_location"), **_google(market, "gl", "hl"))
    out: list[JobPosting] = []
    seen: set[tuple[str, str]] = set()
    for i, item in enumerate(_list(_data(resp), "jobs_results")):
        title = _str(item.get("title"))
        company = _str(item.get("company_name"))
        if not (title and company and _BUYING_ROLE.search(title)):
            continue
        key = (company.lower(), title.lower())
        if key in seen:
            continue
        seen.add(key)
        description = _str(item.get("description")) or ""
        highlights = " ".join(s for s in _strings(item.get("job_highlights")) if s)
        text = f"{description} {highlights}"
        ext = _dict(item, "detected_extensions")
        posted = _str(ext.get("posted_at")) or next(
            (e for e in (_str(x) for x in item.get("extensions") or []) if e and _POSTED.search(e)), None
        )
        via = _str(item.get("via"))
        via = re.sub(r"^via\s+", "", via, flags=re.I) if via else None
        options = item.get("apply_options") if isinstance(item.get("apply_options"), list) else []
        link = _str(item.get("share_link")) or next(
            (_str(o.get("link")) for o in options if isinstance(o, dict) and _str(o.get("link"))), None
        )
        location = _str(item.get("location"))
        eid = ev.add(
            resp,
            i,
            title=f"{company} is hiring: {title}",
            url=link,
            snippet=_join(location, posted, via and f"via {via}"),
        )
        out.append(
            JobPosting(
                company=company,
                title=title,
                location=location,
                via=via,
                posted=posted,
                posted_days=_posted_days(posted),
                link=link,
                query=q,
                mentions_india=bool(_JOB_INDIA.search(text)),
                mentions_overseas=bool(_JOB_OVERSEAS.search(text)),
                is_recruiter=bool(_RECRUITER_NAME.search(company)),
                snippet=_job_snippet(text),
                evidence_id=eid,
            )
        )
    return out


def fx_rate(client: SerpClient, ev: EvidenceStore, pair: str) -> float | None:
    """Exchange rate from Google Finance, e.g. pair="GBP-INR" -> INR per GBP."""
    resp = client.search("google_finance", q=pair)
    summary = _dict(_data(resp), "summary")
    rate = _num(summary.get("extracted_price"))
    if rate is None:
        rate = _num(summary.get("price"))
    if rate is None or rate <= 0:
        return None
    ev.add(
        resp,
        0,
        title=f"Google Finance {pair}: {rate:g}",
        url=f"https://www.google.com/finance/quote/{pair}",
        snippet=_str(summary.get("title")),
    )
    return rate
