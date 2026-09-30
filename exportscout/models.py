"""Shared data models. Every pipeline step reads and writes these.

Numbers in the output always point back to ``Evidence`` records (search results) through
``evidence_id`` / ``evidence_ids``. Evidence IDs look like ``amazon:3f2a91c0:4``
(engine, first 8 chars of the cache key, index of the result in the response).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from exportscout.serp.client import SerpResponse


# --------------------------------------------------------------------------- evidence


class Evidence(BaseModel):
    """One search result that backs a number or a claim."""

    id: str
    engine: str
    fetched_at: str
    title: str
    url: str | None = None
    snippet: str | None = None
    query: str | None = None  # readable request summary, e.g. 'amazon amazon.co.uk k="brass lantern"'


class EvidenceStore:
    """Collects evidence during a run. Engine normalisers add to it; the report reads it."""

    def __init__(self) -> None:
        self._items: dict[str, Evidence] = {}

    def add(
        self,
        resp: SerpResponse,
        index: int | str,
        *,
        title: str,
        url: str | None = None,
        snippet: str | None = None,
    ) -> str:
        eid = f"{resp.engine}:{resp.cache_key[:8]}:{index}"
        if eid not in self._items:
            shown = {k: v for k, v in resp.params.items() if k not in {"image_sha256"}}
            query = resp.engine + " " + " ".join(f'{k}="{v}"' for k, v in shown.items())
            self._items[eid] = Evidence(
                id=eid,
                engine=resp.engine,
                fetched_at=resp.fetched_at,
                title=title[:300],
                url=url,
                snippet=(snippet or None) and snippet[:500],
                query=query,
            )
        return eid

    def get(self, eid: str) -> Evidence | None:
        return self._items.get(eid)

    def __contains__(self, eid: object) -> bool:
        return eid in self._items

    def __len__(self) -> int:
        return len(self._items)

    def all(self) -> list[Evidence]:
        return list(self._items.values())


# --------------------------------------------------------------------------- inputs


class RunInputs(BaseModel):
    """What the exporter types into the app."""

    image_path: str | None = None  # local photo (uploaded to SerpApi's Image API)
    image_url: str | None = None  # or a public image URL
    description: str | None = None  # optional hint / text-only fallback, e.g. "brass hurricane lantern"
    unit_cost_inr: float | None = None  # ex-factory cost per piece
    extra_costs_inr: float = 0.0  # packing + inland freight to port, per piece
    moq: int | None = None
    material: str | None = None  # e.g. "brass"
    finish: str | None = None  # e.g. "antique gold"
    market: str = "uk"
    category: str = "metal_handicrafts"
    assumption_overrides: dict[str, float] = Field(default_factory=dict)  # keys as in QuoteRange.assumptions


# --------------------------------------------------------------------------- normalised search data

Channel = Literal["lens", "google_shopping", "amazon", "ebay"]


class Listing(BaseModel):
    """A product for sale in the target market, from any shopping channel."""

    channel: Channel
    title: str
    price: float | None = None  # in `currency`; for eBay ranges, the "from" price
    currency: str | None = None  # ISO code, e.g. "GBP"
    url: str | None = None
    merchant: str | None = None  # Shopping `source`, eBay seller username, Lens `source`
    brand: str | None = None
    rating: float | None = None
    reviews: int | None = None
    bought_last_month: int | None = None  # Amazon "500+ bought in past month" -> 500
    asin: str | None = None
    location: str | None = None  # eBay "Located in India"
    sponsored: bool = False
    thumbnail: str | None = None
    evidence_id: str


class ReviewSnippet(BaseModel):
    text: str
    title: str | None = None
    rating: float | None = None
    evidence_id: str


class ProductDetail(BaseModel):
    """One Amazon product page (amazon_product engine)."""

    asin: str
    title: str | None = None
    brand: str | None = None
    manufacturer: str | None = None
    origin: str | None = None  # a country-of-origin field, if Amazon shows one
    origin_text_signal: str | None = None  # e.g. "Handmade in India" found in title/bullets/description
    price: float | None = None
    currency: str | None = None
    rating: float | None = None
    reviews_count: int | None = None
    reviews: list[ReviewSnippet] = Field(default_factory=list)
    evidence_id: str


class TrendsSeries(BaseModel):
    """Google Trends interest over time; `values[term]` aligns with `dates`."""

    terms: list[str]
    dates: list[str]  # ISO date of each point (start of the week/month)
    values: dict[str, list[int]]
    evidence_id: str


class RegionInterest(BaseModel):
    region: str
    value: int
    evidence_id: str


class Place(BaseModel):
    """A Google Maps business."""

    title: str
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    rating: float | None = None
    reviews: int | None = None
    type: str | None = None
    data_id: str | None = None
    city: str | None = None
    evidence_id: str


class WebResult(BaseModel):
    title: str
    link: str
    domain: str
    snippet: str | None = None
    evidence_id: str


class AdsActivity(BaseModel):
    """Google Ads Transparency Center summary for one advertiser domain."""

    domain: str
    advertiser: str | None = None
    total_creatives: int = 0
    first_shown: str | None = None  # ISO date
    last_shown: str | None = None  # ISO date
    active_last_30d: int = 0  # creatives whose last_shown is within 30 days of fetch
    evidence_id: str


class NewsItem(BaseModel):
    title: str
    link: str | None = None
    source: str | None = None
    date: str | None = None
    snippet: str | None = None
    evidence_id: str


# --------------------------------------------------------------------------- derived cards


class ProductIdentity(BaseModel):
    """What the product is, in the target market's retail words (from Lens titles via the LLM)."""

    product_type: str  # e.g. "hurricane lantern"
    keywords: list[str]  # 3-5 retail search phrases, most specific first
    broad_term: str  # broader term for Trends fallback, e.g. "lantern"
    material: str | None = None
    style_tags: list[str] = Field(default_factory=list)  # e.g. ["antique", "gold", "moroccan"]


class ChannelStats(BaseModel):
    n: int
    p25: float
    p50: float
    p75: float


class PriceLadder(BaseModel):
    currency: str
    n: int
    min: float
    p25: float
    p50: float
    p75: float
    max: float
    by_channel: dict[str, ChannelStats] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)


Verdict = Literal["go", "tight", "no_go", "unknown"]


class QuoteRange(BaseModel):
    """FOB price range the exporter can quote, worked back from retail (plan §6.1)."""

    currency: str
    retail_median: float
    retail_ex_vat: float
    fob_importer: float  # low end: sale goes through an importer/wholesaler
    fob_retailer: float  # high end: retailer imports directly
    fx_rate: float | None = None  # INR per 1 unit of `currency`
    fob_importer_inr: float | None = None
    fob_retailer_inr: float | None = None
    unit_cost_inr: float | None = None
    total_cost_inr: float | None = None  # unit cost + extra costs
    margin_retailer: float | None = None  # fraction of FOB, e.g. 0.38
    margin_importer: float | None = None
    verdict: Verdict = "unknown"  # judged on margin_retailer
    assumptions: dict[str, float] = Field(default_factory=dict)  # vat_rate, retailer_markup, importer_markup, freight_ins_pct, duty_pct


class DemandCard(BaseModel):
    term: str
    is_proxy: bool = False  # True when a broader term stood in for a low-volume one
    mean_12m: float | None = None  # mean interest (0-100) over the last 12 months
    yoy_change: float | None = None  # last 12 months vs the 12 before, e.g. 0.18
    peak_months: list[int] = Field(default_factory=list)  # 1-12, strongest first
    top_regions: list[str] = Field(default_factory=list)
    related_queries: list[str] = Field(default_factory=list)
    autocomplete: list[str] = Field(default_factory=list)
    months_to_window: int | None = None  # months until buyers choose the next peak's range
    buying_window_note: str | None = None  # e.g. "Buyers pick Oct–Dec ranges around Apr: pitch now"
    timeline: list[tuple[str, int]] = Field(default_factory=list)  # (ISO date, interest) for `term`, for the chart
    evidence_ids: list[str] = Field(default_factory=list)


class OriginShare(BaseModel):
    """Where competing products come from."""

    checked: int = 0  # Amazon products checked
    india: int = 0
    china: int = 0
    other: int = 0
    unknown: int = 0
    ebay_total: int = 0
    ebay_from_india: int = 0
    india_examples: list[str] = Field(default_factory=list)  # evidence ids of made-in-India items
    evidence_ids: list[str] = Field(default_factory=list)

    @property
    def india_share(self) -> float | None:
        known = self.india + self.china + self.other
        return self.india / known if known else None


class ReviewTheme(BaseModel):
    label: str  # e.g. "Tarnishing"
    kind: Literal["complaint", "praise"]
    count: int  # reviews mentioning it
    share: float  # count / reviews analysed
    quotes: list[str] = Field(default_factory=list)  # short verbatim quotes
    fix: str | None = None  # spec improvement / pitch line, for complaints
    evidence_ids: list[str] = Field(default_factory=list)


class ScoreComponent(BaseModel):
    points: float
    max_points: float
    detail: str
    evidence_ids: list[str] = Field(default_factory=list)


class MarketScore(BaseModel):
    """Market Opportunity Score (plan §6.2)."""

    total: float  # 0-100
    components: dict[str, ScoreComponent]  # demand, price_headroom, duty_advantage, proven_india, buyer_depth, timing


# --------------------------------------------------------------------------- buyers

SignalKind = Literal[
    "category_match",
    "india_sourcing",
    "price_tier",
    "ads_active",
    "news",
    "trade_page",
    "showroom",
    "website",
    "phone",
    "multi_engine",
    "size",
]
BuyerKind = Literal["retailer", "online_brand", "wholesaler", "importer", "marketplace", "not_a_buyer", "unknown"]


class BuyerSignal(BaseModel):
    kind: SignalKind
    detail: str  # human-readable, e.g. "9 UK ads running since Jun 2026"
    evidence_id: str


class BuyerCandidate(BaseModel):
    name: str
    domain: str | None = None  # dedupe key, e.g. "grahamandgreen.co.uk"
    kind: BuyerKind = "unknown"
    city: str | None = None
    website: str | None = None
    phone: str | None = None
    address: str | None = None
    sources: list[str] = Field(default_factory=list)  # engines it was seen in
    seen_texts: list[str] = Field(default_factory=list)  # titles/snippets seen, for style matching
    listing_prices: list[float] = Field(default_factory=list)  # their retail prices for this product type
    review_count: int | None = None  # size proxy (Maps / Shopping reviews)
    locations_count: int = 0  # Maps locations found
    ad_creatives: int | None = None
    is_giant: bool = False  # too big for a small unit (config list or size proxies)
    enriched: bool = False
    signals: list[BuyerSignal] = Field(default_factory=list)
    fit_score: float | None = None  # 0-100 (plan §6.3)
    fit: dict[str, ScoreComponent] = Field(default_factory=dict)  # category, india, price_tier, activity, size, reachability
    why: str | None = None  # one-line reason to pitch them


# --------------------------------------------------------------------------- output


class Pitch(BaseModel):
    buyer_name: str
    subject: str
    body: str
    evidence_ids: list[str] = Field(default_factory=list)


class StepEvent(BaseModel):
    """A line in the live step log."""

    step: int  # 1-8 as in plan §5
    name: str  # "Identify", "Demand", "Prices", "Origin", "Reviews", "Buyers", "Enrich", "Write"
    message: str
    status: Literal["running", "done", "skipped", "warning"] = "done"
    credits_used: int = 0  # cumulative for the run
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


class Brief(BaseModel):
    """Everything one run produces. The app renders it; report/brief.py exports it."""

    inputs: RunInputs
    market: str
    market_label: str
    product: ProductIdentity | None = None
    listings: list[Listing] = Field(default_factory=list)  # all priced listings behind the ladder
    ladder: PriceLadder | None = None
    quote: QuoteRange | None = None
    demand: DemandCard | None = None
    origin: OriginShare | None = None
    themes: list[ReviewTheme] = Field(default_factory=list)
    buyers: list[BuyerCandidate] = Field(default_factory=list)  # ranked, best first
    market_score: MarketScore | None = None
    headline: str = ""  # e.g. "Brass hurricane lantern → United Kingdom: GO (74/100)"
    summary_md: str = ""  # narrative; cites evidence as [ev:ID]
    spec_improvements: list[str] = Field(default_factory=list)
    pitches: list[Pitch] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    steps: list[StepEvent] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    credits_used: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
