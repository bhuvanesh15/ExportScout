import time
from datetime import datetime, timezone

import httpx
import pytest

from exportscout.config import category, market
from exportscout.models import BuyerCandidate, BuyerSignal, EvidenceStore, Listing, Place, ProductDetail, ProductIdentity, WebResult
from exportscout.pipeline import buyers as B
from exportscout.serp.client import BudgetExceeded, SerpClient

CAT = category("metal_handicrafts")
UK = market("uk")
PRODUCT = ProductIdentity(
    product_type="hurricane lantern",
    keywords=["brass hurricane lantern", "gold metal lantern"],
    broad_term="lantern",
    material="brass",
)
NO_RESULTS = {"error": "Google hasn't returned any results for this query."}
DAY = 86400


def lst(channel, title, eid, *, merchant=None, url=None, price=None, currency="GBP", brand=None, reviews=None):
    return Listing(
        channel=channel,
        title=title,
        merchant=merchant,
        url=url,
        price=price,
        currency=currency if price is not None else None,
        brand=brand,
        reviews=reviews,
        evidence_id=eid,
    )


def web(title, link, eid, snippet=None):
    from exportscout.serp.engines import domain_of

    return WebResult(title=title, link=link, domain=domain_of(link), snippet=snippet, evidence_id=eid)


def discover(listings=(), webs=(), places=(), products=(), product=PRODUCT, **kw):
    return B.discover_candidates(
        listings=list(listings), web=list(webs), places=list(places), category=CAT, products=list(products), product=product, **kw
    )


def by_name(cands):
    return {c.name: c for c in cands}


# --------------------------------------------------------------------------- helpers


def test_normalize_name_and_site_domain():
    assert B.normalize_name("Graham & Green Ltd") == B.normalize_name("graham and green") == "grahamandgreen"
    assert B.normalize_name("Selfridges & Co") == "selfridges"
    assert B.normalize_name("  ") == "" and B.normalize_name(None) == ""
    assert B.site_domain("shop.nkuku.co.uk") == "nkuku.co.uk"
    assert B.site_domain("www.nkuku.com") == "nkuku.com"
    assert B.site_domain("brassco.myshopify.com") == "brassco.myshopify.com"


def test_discovery_queries_fill_templates():
    qs = B.discovery_queries(PRODUCT, UK, CAT)
    assert qs == [
        "brass hurricane lantern wholesale UK",
        "brass hurricane lantern stockist UK handmade",
        "homeware brand brass hurricane lantern handmade in India",
        "trade account homeware wholesaler UK",
    ]
    bare = PRODUCT.model_copy(update={"keywords": []})
    assert B.discovery_queries(bare, UK, CAT)[0] == "hurricane lantern wholesale UK"


# --------------------------------------------------------------------------- discovery


def test_dedupe_by_domain_merges_every_source():
    cands = discover(
        listings=[
            lst("lens", "Brass Hurricane Lantern", "google_lens:aaaa:0", merchant="Graham & Green", url="https://www.grahamandgreen.co.uk/p/lantern", price=45.0),
            lst("google_shopping", "Antique brass lantern", "google_shopping:bbbb:0", merchant="Graham and Green", price=50.0, reviews=80),
            lst("google_shopping", "Lantern", "google_shopping:bbbb:1", merchant="Graham & Green", price=30.0, currency="USD"),
            lst("ebay", "Brass lantern", "ebay:cccc:0", merchant="moradabad_crafts", price=9.0),
        ],
        webs=[web("Brass Lanterns | Graham & Green", "https://www.grahamandgreen.co.uk/lanterns", "google:dddd:0", "Shop hurricane lanterns")],
        places=[
            Place(
                title="Graham & Green",
                website="https://www.grahamandgreen.co.uk/",
                phone="+44 20 7243 8908",
                address="4 Elgin Crescent, London",
                reviews=150,
                type="Home goods store",
                city="London",
                data_id="0x1",
                evidence_id="google_maps:eeee:0",
            )
        ],
    )
    assert len(cands) == 1  # eBay sellers are skipped
    c = cands[0]
    assert (c.name, c.domain, c.website, c.phone, c.city) == (
        "Graham & Green",
        "grahamandgreen.co.uk",
        "https://www.grahamandgreen.co.uk/",
        "+44 20 7243 8908",
        "London",
    )
    assert set(c.sources) == {"google_lens", "google_shopping", "google", "google_maps"}
    assert sorted(c.listing_prices) == [45.0, 50.0]  # the USD price is left out
    assert (c.review_count, c.locations_count, c.is_giant, c.kind) == (150, 1, False, "retailer")
    assert "Brass Lanterns | Graham & Green" in c.seen_texts and "Home goods store" in c.seen_texts
    signals = {s.kind: s for s in c.signals}
    assert set(signals) == {"multi_engine", "category_match", "website", "phone"}
    assert signals["category_match"].evidence_id == "google_lens:aaaa:0"
    assert "brass hurricane lantern" in signals["category_match"].detail
    assert signals["phone"].evidence_id == "google_maps:eeee:0"
    assert signals["website"].evidence_id == "google_maps:eeee:0"
    assert signals["multi_engine"].evidence_id in {"google:dddd:0", "google_maps:eeee:0", "google_shopping:bbbb:0"}


def test_name_only_merges_into_domain_by_label():
    cands = discover(
        listings=[
            lst("google_shopping", "Hammered brass planter", "s:0", merchant="Graham & Green", price=30.0),
            lst("google_shopping", "Brass bowl", "s:1", merchant="Nkuku", price=25.0),
        ],
        webs=[
            web("Lanterns - Shop now", "https://www.grahamandgreen.co.uk/lanterns", "g:0"),
            web("Home Accessories", "https://uk.nkuku.com/home", "g:1"),
        ],
    )
    got = {c.domain: c for c in cands}
    assert set(got) == {"grahamandgreen.co.uk", "nkuku.com"}
    assert got["grahamandgreen.co.uk"].name == "Graham & Green"
    assert got["nkuku.com"].name == "Nkuku" and got["nkuku.com"].website == "https://uk.nkuku.com"
    assert all("multi_engine" in {s.kind for s in c.signals} for c in cands)


def test_web_only_name_from_title_or_domain():
    cands = discover(
        webs=[
            web("Wholesale Lanterns UK - The Lantern Company", "https://thelanterncompany.co.uk/", "g:0"),
            web("Buy brass online", "https://www.brass-and-co.com/shop", "g:1"),
        ]
    )
    assert [c.name for c in cands] == ["The Lantern Company", "Brass And Co"]
    assert cands[0].signals[0].kind == "category_match" and cands[0].kind == "wholesaler"
    assert all(c.sources == ["google"] for c in cands)
    assert all("multi_engine" not in {s.kind for s in c.signals} for c in cands)


def test_name_only_dedupe_by_normalised_name():
    cands = discover(
        listings=[
            lst("google_shopping", "Brass lantern", "s:0", merchant="The Lantern Co", price=20.0),
            lst("google_shopping", "Gold lantern", "s:1", merchant="the lantern co.", price=22.0),
        ]
    )
    assert len(cands) == 1
    c = cands[0]
    assert c.domain is None and c.name == "The Lantern Co" and c.sources == ["google_shopping"]
    assert c.listing_prices == [20.0, 22.0]


def test_marketplaces_are_excluded():
    cands = discover(
        listings=[
            lst("lens", "Brass lantern", "l:0", merchant="Amazon.co.uk", url="https://www.amazon.co.uk/dp/B001", price=20.0),
            lst("lens", "Brass lantern", "l:1", merchant="Etsy", url="https://www.etsy.com/uk/listing/1", price=21.0),
            lst("google_shopping", "Brass lantern", "s:0", merchant="eBay - brassworld", price=9.0),
            lst("google_shopping", "Brass lantern", "s:1", merchant="OnBuy.com", price=9.0),
            lst("google_shopping", "Brass lantern", "s:2", merchant="Not On The High Street", price=30.0),
        ],
        webs=[
            web("Brass lanterns | Etsy UK", "https://www.etsy.com/uk/market/brass_lantern", "g:0"),
            web("Brass lantern ideas", "https://uk.pinterest.com/ideas/brass-lantern", "g:1"),
            web("Lantern - Wikipedia", "https://en.wikipedia.org/wiki/Lantern", "g:2"),
            web("Brass lantern", "https://www.amazon.co.uk/s?k=brass+lantern", "g:3"),
        ],
        places=[
            Place(title="Brass Emporium", website="https://www.facebook.com/brassemporium", phone="0161 123 4567", city="Manchester", evidence_id="m:0")
        ],
        products=[
            ProductDetail(asin="B1", brand="Generic", title="Brass lantern", evidence_id="p:0"),
            ProductDetail(asin="B2", brand="Hosley", title="Hosley brass hurricane lantern", price=24.0, currency="GBP", evidence_id="p:1"),
        ],
    )
    got = by_name(cands)
    assert set(got) == {"Brass Emporium", "Hosley"}
    assert got["Brass Emporium"].domain is None and got["Brass Emporium"].website is None
    assert got["Brass Emporium"].phone == "0161 123 4567" and got["Brass Emporium"].kind == "retailer"
    assert got["Hosley"].sources == ["amazon"] and got["Hosley"].kind == "online_brand"
    assert got["Hosley"].listing_prices == [24.0]


def test_giants_flagged_and_not_ranked():
    cands = discover(
        listings=[
            lst("google_shopping", "Brass lantern", "s:0", merchant="John Lewis & Partners", price=60.0),
            lst("google_shopping", "Brass lantern", "s:1", merchant="Dunelm", price=15.0),
        ],
        webs=[web("Brass Lanterns | John Lewis & Partners", "https://www.johnlewis.com/browse/lanterns", "g:0")],
    )
    got = by_name(cands)
    assert set(got) == {"John Lewis & Partners", "Dunelm"}
    assert got["John Lewis & Partners"].domain == "johnlewis.com" and got["John Lewis & Partners"].is_giant
    assert got["Dunelm"].is_giant and got["Dunelm"].domain is None
    assert B.rank_for_enrichment(cands, 5) == []


def test_currency_filter_defaults_to_most_common():
    listings = [
        lst("google_shopping", "a", "s:0", merchant="Shop A", price=10.0),
        lst("google_shopping", "b", "s:1", merchant="Shop A", price=12.0, currency="USD"),
        lst("google_shopping", "c", "s:2", merchant="Shop B", price=14.0),
    ]
    assert by_name(discover(listings=listings))["Shop A"].listing_prices == [10.0]
    assert by_name(discover(listings=listings, currency="USD"))["Shop A"].listing_prices == [12.0]


def test_no_product_means_no_category_signal():
    cands = discover(listings=[lst("google_shopping", "Brass hurricane lantern", "s:0", merchant="Shop A")], product=None)
    assert cands[0].signals == []


# --------------------------------------------------------------------------- ranking


def _cand(name, domain, sources, signals=(), **kw):
    sig = [BuyerSignal(kind=k, detail=k, evidence_id=f"{name}:{k}") for k in signals]
    return BuyerCandidate(name=name, domain=domain, sources=list(sources), signals=sig, **kw)


def test_rank_for_enrichment_prior_and_exclusions():
    a = _cand("A", "a.co.uk", ["google_lens", "google_shopping", "google"], ["multi_engine", "category_match"], listing_prices=[30.0])
    b = _cand("B", "b.co.uk", ["google"], ["category_match"])
    c = _cand("C", "c.co.uk", ["google_maps"], phone="1", locations_count=1)
    d = _cand("D", "d.co.uk", ["google"])
    no_domain = _cand("N", None, ["google_shopping", "amazon"], ["multi_engine", "category_match"])
    giant = _cand("G", "johnlewis.com", ["google", "google_shopping"], ["multi_engine"], is_giant=True)
    market = _cand("M", "faire.com", ["google"], ["category_match"]).model_copy(update={"kind": "marketplace"})
    ranked = B.rank_for_enrichment([d, no_domain, c, giant, market, b, a], 3)
    assert [x.name for x in ranked] == ["A", "N", "B"]  # domainless is eligible: enrich looks its site up
    assert [x.name for x in B.rank_for_enrichment([d, c, b, a], 10)] == ["A", "B", "C", "D"]
    assert B.rank_for_enrichment([a, b], 0) == []


# --------------------------------------------------------------------------- enrichment


class FakeSerpApi:
    def __init__(self, responses):
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        params = dict(request.url.params)
        body = self.responses.get(params["engine"], NO_RESULTS)
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)

    def params(self, engine):
        return [dict(r.url.params) for r in self.requests if r.url.params.get("engine") == engine]


@pytest.fixture
def serp(tmp_path):
    clients = []

    def make(responses, **kwargs):
        fake = FakeSerpApi(responses)
        client = SerpClient(
            api_key="test",
            mode="live",
            cache_path=tmp_path / f"serp{len(clients)}.sqlite",
            http=httpx.Client(transport=httpx.MockTransport(fake)),
            **kwargs,
        )
        clients.append(client)
        return client, EvidenceStore(), fake

    yield make
    for c in clients:
        c.close()


def _iso(seconds_ago):
    return datetime.fromtimestamp(time.time() - seconds_ago, tz=timezone.utc).isoformat()


SITE = {
    "organic_results": [
        {
            "title": "Trade Account | Nkuku",
            "link": "https://www.nkuku.com/pages/trade",
            "snippet": "Apply for a trade account. Our pieces are handmade in India by artisans.",
        },
        {"title": "Hurricane Lanterns | Nkuku", "link": "https://nkuku.com/collections/lanterns", "snippet": "Brass hurricane lanterns"},
        {"title": "Wholesale lanterns", "link": "https://other.com/x", "snippet": "Made in India"},
    ]
}


def _ads():
    now = time.time()
    return {
        "search_information": {"total_results": 12},
        "ad_creatives": [
            {"advertiser": "Nkuku Ltd", "first_shown": int(now - 200 * DAY), "last_shown": int(now - 3 * DAY)},
            {"advertiser": "Nkuku Ltd", "first_shown": int(now - 90 * DAY), "last_shown": int(now - 60 * DAY)},
        ],
    }


NEWS = {
    "news_results": [
        {"title": "Nkuku opens Bristol store", "link": "https://n/1", "source": {"name": "Retail Gazette"}, "iso_date": _iso(10 * DAY)},
        {"title": "Nkuku wins award", "link": "https://n/2", "source": {"name": "Old News"}, "iso_date": _iso(900 * DAY)},
        {"title": "Some other brand expands", "link": "https://n/3", "iso_date": _iso(2 * DAY)},
        {"title": "Nkuku no date", "link": "https://n/4"},
    ]
}


def test_enrich_adds_signals_and_spends_three_credits(serp):
    client, ev, fake = serp({"google": SITE, "google_ads_transparency_center": _ads(), "google_news": NEWS})
    cand = _cand("Nkuku", "nkuku.com", ["google_shopping"], ["category_match"])
    out = B.enrich(client, ev, UK, cand, PRODUCT, with_news=True)

    assert out.enriched and not cand.enriched and len(cand.signals) == 1  # the input is not mutated
    assert client.credits_used == 3
    assert fake.params("google")[0]["q"] == 'site:nkuku.com trade OR wholesale OR "made in India" OR "handmade in India"'
    assert fake.params("google_ads_transparency_center")[0]["text"] == "nkuku.com"
    assert fake.params("google_news")[0]["q"] == '"Nkuku"'

    kinds = [s.kind for s in out.signals]
    assert kinds.count("trade_page") == 1 and kinds.count("india_sourcing") == 1 and kinds.count("news") == 1
    assert kinds.count("category_match") == 2  # one from discovery, one from the site search
    signals = {s.kind: s for s in out.signals}
    assert signals["trade_page"].evidence_id in ev and "Trade Account" in signals["trade_page"].detail
    assert "handmade in India" in signals["india_sourcing"].detail
    assert signals["ads_active"].detail.startswith("12 UK ad creatives, last shown ")
    assert out.ad_creatives == 12
    assert "Bristol" in signals["news"].detail and "Retail Gazette" in signals["news"].detail
    new = out.signals[len(cand.signals):]
    assert len(new) == 5 and all(s.evidence_id in ev for s in new)
    assert "Trade Account | Nkuku" in out.seen_texts and "Wholesale lanterns" not in out.seen_texts


def test_enrich_without_ads_or_news(serp):
    client, ev, fake = serp({"google": {"organic_results": []}})
    out = B.enrich(client, ev, UK, _cand("Shop", "shop.co.uk", ["google"]), PRODUCT)
    assert out.enriched and out.ad_creatives == 0 and out.signals == []
    assert client.credits_used == 2 and fake.params("google_news") == []


def test_enrich_old_ads_are_not_active(serp):
    now = time.time()
    old = {"ad_creatives": [{"advertiser": "X", "first_shown": int(now - 400 * DAY), "last_shown": int(now - 100 * DAY)}]}
    client, ev, _ = serp({"google_ads_transparency_center": old})
    out = B.enrich(client, ev, UK, _cand("X", "x.co.uk", ["google"]), PRODUCT)
    assert out.ad_creatives == 1 and "ads_active" not in {s.kind for s in out.signals}


def test_enrich_survives_serpapi_error(serp):
    bad = httpx.Response(400, json={"error": "Invalid region"})
    client, ev, _ = serp({"google": SITE, "google_ads_transparency_center": bad})
    out = B.enrich(client, ev, UK, _cand("Nkuku", "nkuku.com", ["google"]), PRODUCT)
    assert out.enriched and out.ad_creatives is None
    assert "trade_page" in {s.kind for s in out.signals}


def test_enrich_budget_exceeded_propagates(serp):
    client, ev, _ = serp({"google": SITE}, budget=1)
    with pytest.raises(BudgetExceeded):
        B.enrich(client, ev, UK, _cand("Nkuku", "nkuku.com", ["google"]), PRODUCT)


def test_resolve_domain_matches_business_name():
    results = [
        WebResult(title="Brass lanterns | Amazon", link="https://www.amazon.co.uk/x", domain="amazon.co.uk", evidence_id="g:1"),
        WebResult(title="Kayu Home — lanterns", link="https://www.kayuhome.co.uk/lanterns", domain="kayuhome.co.uk", evidence_id="g:2"),
    ]
    assert B.resolve_domain("Kayu Home", results) == "kayuhome.co.uk"
    assert B.resolve_domain("Pooky", results) is None
    eu = [WebResult(title="Turquoise", link="https://turquoise.eu/", domain="turquoise.eu", evidence_id="g:3")]
    assert B.resolve_domain("Turquoise Living", eu) is None
    assert B.resolve_domain("", results) is None
