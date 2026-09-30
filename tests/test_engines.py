import importlib.util
import io
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from PIL import Image

from exportscout.config import market
from exportscout.models import AdsActivity, EvidenceStore, Listing, Place, ProductDetail, TrendsSeries
from exportscout.serp import engines as E
from exportscout.serp.client import BudgetExceeded, SerpClient

UK = market("uk")
NO_RESULTS = {"error": "Google hasn't returned any results for this query."}
DAY = 86400


class FakeSerpApi:
    """Canned JSON per engine; a value may be a function of the request params."""

    def __init__(self, responses, searches_left=238):
        self.responses = responses
        self.searches_left = searches_left
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/image":
            return httpx.Response(200, json={"image_id": "img1"})
        if request.url.path == "/account.json":
            return httpx.Response(200, json={"plan_searches_left": self.searches_left})
        params = dict(request.url.params)
        body = self.responses.get(params["engine"], NO_RESULTS)
        if callable(body):
            body = body(params)
        return httpx.Response(200, json=body)

    def params(self, engine):
        return [dict(r.url.params) for r in self.requests if r.url.params.get("engine") == engine]


@pytest.fixture
def serp(tmp_path):
    """serp(responses, **client_kwargs) -> (client, ev, fake)."""
    clients = []

    def make(responses, searches_left=238, **kwargs):
        fake = FakeSerpApi(responses, searches_left)
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


# --------------------------------------------------------------------------- parsing helpers


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1K+ bought in past month", 1000),
        ("50+ bought in past month", 50),
        ("2.5K+ bought in past month", 2500),
        ("1M+", 1_000_000),
        ("1,234", 1234),
        ("(987)", 987),
        ("<1", 0),
        (42, 42),
        ("", None),
        ("n/a", None),
        (None, None),
        (True, None),
    ],
)
def test_parse_count(text, expected):
    assert E.parse_count(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("£24.99", 24.99),
        ("£1,299.00", 1299.0),
        ("24,99 €", 24.99),
        ("1.299,00 €", 1299.0),
        ("£24.99 - £30.00", 24.99),
        ("4.5 out of 5 stars", 4.5),
        (12, 12.0),
        ("free", None),
        (None, None),
        ({"value": 1}, None),
    ],
)
def test_parse_price(text, expected):
    assert E.parse_price(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("£24.99", "GBP"),
        ("$5.00", "USD"),
        ("US $15.00", "USD"),
        ("CA$10", "CAD"),
        ("€", "EUR"),
        ("₹1,200", "INR"),
        ("GBP", "GBP"),
        ("gbp", "GBP"),
        ("24.99 EUR", "EUR"),
        ("24.99", None),
        (None, None),
    ],
)
def test_currency_code(text, expected):
    assert E.currency_code(text) == expected


def test_currency_code_default():
    assert E.currency_code("24.99", "GBP") == "GBP"


def test_parse_datetime_epochs_iso_and_relative():
    ref = datetime(2026, 9, 30, tzinfo=timezone.utc)
    assert E.parse_datetime(1_700_000_000) == E.parse_datetime("1700000000") == E.parse_datetime(1_700_000_000_000)
    assert E.parse_datetime("2026-09-20").date().isoformat() == "2026-09-20"
    assert E.parse_datetime("2026-09-20T10:00:00Z").hour == 10
    assert E.parse_datetime("09/20/2026, 07:00 AM, +0000 UTC").date().isoformat() == "2026-09-20"
    assert E.parse_datetime("Sep 20, 2026").date().isoformat() == "2026-09-20"
    assert E.parse_datetime("3 days ago", ref=ref) == ref - timedelta(days=3)
    assert E.parse_datetime("3 days ago") is None
    assert E.parse_datetime("sometime") is None and E.parse_datetime(None) is None


def test_domain_of():
    assert E.domain_of("https://www.grahamandgreen.co.uk/x?y=1") == "grahamandgreen.co.uk"
    assert E.domain_of("shop.Example.com/path") == "shop.example.com"
    assert E.domain_of("not a url") is None and E.domain_of(None) is None


# --------------------------------------------------------------------------- shopping channels

LENS = {
    "visual_matches": [
        {
            "position": 1,
            "title": "Brass Hurricane Lantern",
            "link": "https://www.grahamandgreen.co.uk/lantern",
            "source": "Graham & Green",
            "price": {"value": "£24.99", "extracted_value": 24.99, "currency": "£"},
            "thumbnail": "https://t/1.jpg",
            "in_stock": True,
        },
        {"title": "Gold lantern", "link": "https://ex.com/a", "source": "Ex", "price": {"value": "$30.00", "extracted_value": 30.0, "currency": "$"}},
        {"title": "Coded", "link": "https://x.co.uk/b", "source": "X", "price": {"value": "45.00", "extracted_value": 45, "currency": "GBP"}},
        {"title": "No price", "link": "https://y.co.uk/c", "source": "Y"},
        {"link": "https://no-title.example"},
        {"title": "Euro", "link": "https://z.de/d", "price": {"value": "24,99 €"}},
        {"title": "Odd symbol", "link": "https://pl.example/e", "price": {"value": "99 zł", "extracted_value": 99, "currency": "zł"}},
        "garbage",
    ],
    "products": [
        {"title": "Brass Hurricane Lantern", "link": "https://www.grahamandgreen.co.uk/lantern"},
        {"title": "Product only", "link": "https://p.co.uk/x", "source": "P", "price": {"value": "£12", "extracted_value": 12}},
    ],
}


def test_lens_matches_prices_currencies_and_dedupe(serp):
    client, ev, fake = serp({"google_lens": LENS})
    found = E.lens_matches(client, ev, UK, url="https://example.com/photo.jpg")
    got = [(l.title, l.price, l.currency, l.merchant) for l in found]
    assert got == [
        ("Brass Hurricane Lantern", 24.99, "GBP", "Graham & Green"),
        ("Gold lantern", 30.0, "USD", "Ex"),
        ("Coded", 45.0, "GBP", "X"),
        ("No price", None, None, "Y"),
        ("Euro", 24.99, "EUR", None),
        ("Odd symbol", 99.0, "zł", None),
        ("Product only", 12.0, "GBP", "P"),
    ]
    assert all(l.channel == "lens" for l in found)
    assert found[0].thumbnail == "https://t/1.jpg" and found[0].url.endswith("/lantern")
    assert found[-1].evidence_id.endswith(":products.1")
    sent = fake.params("google_lens")[0]
    assert (sent["url"], sent["country"], sent["hl"], sent["type"]) == ("https://example.com/photo.jpg", "gb", "en", "all")
    assert "q" not in sent


def test_lens_matches_uploads_local_image(serp):
    client, ev, fake = serp({"google_lens": LENS})
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (180, 140, 40)).save(buf, "JPEG")
    found = E.lens_matches(client, ev, UK, image=buf.getvalue(), q="brass", type="products")
    assert len(found) == 7
    sent = fake.params("google_lens")[0]
    assert sent["image_id"] == "img1" and sent["q"] == "brass" and sent["type"] == "products"


SHOPPING = {
    "shopping_results": [
        {
            "position": 1,
            "title": "Brass Lantern",
            "product_id": "1",
            "product_link": "https://www.google.co.uk/shopping/product/1",
            "source": "Nkuku",
            "price": "£45.00",
            "extracted_price": 45.0,
            "rating": 4.6,
            "reviews": 120,
            "thumbnail": "https://t/s.jpg",
        },
        {"title": "Lantern two", "source": "John Lewis", "price": "£1,299.00"},
        {"title": "US lantern", "source": "Etsy", "price": "$20.00", "extracted_price": 20},
        {"title": "No price", "source": "Z"},
        {"source": "no title"},
    ]
}


def test_shopping_listings(serp):
    client, ev, fake = serp({"google_shopping": SHOPPING})
    found = E.shopping_listings(client, ev, UK, "brass lantern")
    assert [(l.title, l.price, l.currency, l.merchant) for l in found] == [
        ("Brass Lantern", 45.0, "GBP", "Nkuku"),
        ("Lantern two", 1299.0, "GBP", "John Lewis"),
        ("US lantern", 20.0, "USD", "Etsy"),
        ("No price", None, None, "Z"),
    ]
    assert found[0].rating == 4.6 and found[0].reviews == 120 and found[0].channel == "google_shopping"
    assert found[0].url == "https://www.google.co.uk/shopping/product/1"
    sent = fake.params("google_shopping")[0]
    assert (sent["gl"], sent["hl"], sent["google_domain"], sent["location"]) == (
        "uk",
        "en",
        "google.co.uk",
        "London, England, United Kingdom",
    )


def test_shopping_categorized_fallback(serp):
    data = {"categorized_shopping_results": [{"title": "Lanterns", "shopping_results": [{"title": "A", "source": "S", "price": "£10"}]}]}
    client, ev, _ = serp({"google_shopping": data})
    found = E.shopping_listings(client, ev, UK, "x")
    assert [(l.title, l.price, l.currency) for l in found] == [("A", 10.0, "GBP")]
    assert found[0].evidence_id.endswith(":c0.0")


AMAZON = {
    "product_ads": {"products": [{"title": "An ad", "asin": "BAD"}]},
    "video_results": [{"title": "A video"}],
    "organic_results": [
        {
            "position": 1,
            "asin": "B001",
            "title": "Brass Hurricane Lantern",
            "link": "https://www.amazon.co.uk/dp/B001?ref=x",
            "link_clean": "https://www.amazon.co.uk/dp/B001",
            "rating": 4.5,
            "reviews": 1234,
            "price": "£24.99",
            "extracted_price": 24.99,
            "bought_last_month": "1K+ bought in past month",
            "sponsored": True,
        },
        {"asin": "B002", "title": "Brass Lantern Small", "price": "£19.50", "bought_last_month": "50+ bought in past month", "reviews": "1,020"},
        {"asin": "B003", "title": "No price lantern"},
        {"asin": "B004"},
    ],
}


def test_amazon_listings(serp):
    client, ev, fake = serp({"amazon": AMAZON})
    found = E.amazon_listings(client, ev, UK, "brass lantern")
    assert [(l.asin, l.price, l.currency, l.bought_last_month, l.sponsored) for l in found] == [
        ("B001", 24.99, "GBP", 1000, True),
        ("B002", 19.5, "GBP", 50, False),
        ("B003", None, None, None, False),
    ]
    assert found[0].url == "https://www.amazon.co.uk/dp/B001" and found[0].reviews == 1234 and found[0].rating == 4.5
    assert found[1].url == "https://www.amazon.co.uk/dp/B002" and found[1].reviews == 1020
    assert fake.params("amazon")[0]["k"] == "brass lantern"
    assert fake.params("amazon")[0]["amazon_domain"] == "amazon.co.uk"


def _product_pages(params):
    pages = {
        "B001": {
            "product_results": {
                "title": "Brass Hurricane Lantern, Handmade in India",
                "brand": "Visit the Hosley Store",
                "price": "£29.99",
                "extracted_price": 29.99,
                "rating": 4.4,
                "reviews": 312,
            },
            "product_details": {"brand": "Hosley", "manufacturer": "‎Hosley Ltd", "country_of_origin": "‎India"},
            "about_item": ["Solid brass", "Hand crafted in India"],
            "reviews_information": {
                "summary": {"text": "Customers say it looks lovely"},
                "authors_reviews": [
                    {"title": "Tarnished", "text": "It started to tarnish after a month", "rating": 2},
                    {"title": "Lovely", "body": "Lovely and heavy", "rating": "5.0 out of 5 stars"},
                    {"text": "It started to tarnish after a month"},
                ],
                "other_countries_reviews": [{"review": "Arrived dented", "rating": 1}],
            },
        },
        "B002": {
            "product_results": {"title": "Gold Lantern", "original_price": "£39.99", "price": "£19.99"},
            "product_details": {"item_details": {"country_region_of_origin": "China"}},
        },
        "B003": {
            "product_results": {"title": "Planter", "original_price": "£39.99", "brand": {"name": "Brand: Ivyline"}},
            "product_details": [{"name": "Manufacturer", "value": "Ivyline"}, {"name": "Country of origin", "value": "India"}],
        },
        "B004": {
            "product_results": {"title": "Bowl", "original_price": "£9.99"},
            "product_details": {"brand": "Generic"},
            "product_description": "Crafted by Indian artisans from recycled brass.",
            "related_products": [{"product_details": {"country_of_origin": "China"}}],
        },
    }
    return pages.get(params["asin"], NO_RESULTS)


def test_amazon_product_origin_signal_and_reviews(serp):
    client, ev, fake = serp({"amazon_product": _product_pages})
    p = E.amazon_product(client, ev, UK, "B001")
    assert (p.asin, p.title, p.brand, p.manufacturer, p.origin) == (
        "B001",
        "Brass Hurricane Lantern, Handmade in India",
        "Hosley",
        "Hosley Ltd",
        "India",
    )
    assert p.origin_text_signal == "Handmade in India"
    assert (p.price, p.currency, p.rating, p.reviews_count) == (29.99, "GBP", 4.4, 312)
    assert [(r.text, r.title, r.rating) for r in p.reviews] == [
        ("It started to tarnish after a month", "Tarnished", 2.0),
        ("Lovely and heavy", "Lovely", 5.0),
        ("Arrived dented", None, 1.0),
    ]
    assert all(r.evidence_id in ev for r in p.reviews) and p.evidence_id in ev
    assert fake.params("amazon_product")[0]["amazon_domain"] == "amazon.co.uk"


def test_amazon_product_origin_shapes(serp):
    client, ev, _ = serp({"amazon_product": _product_pages})
    nested = E.amazon_product(client, ev, UK, "B002")
    assert nested.origin == "China" and nested.price == 19.99 and nested.origin_text_signal is None
    rows = E.amazon_product(client, ev, UK, "B003")
    assert (rows.origin, rows.manufacturer, rows.brand) == ("India", "Ivyline", "Ivyline")
    text_only = E.amazon_product(client, ev, UK, "B004")
    assert text_only.origin is None  # neither original_price nor a related product's origin
    assert text_only.origin_text_signal == "Indian artisans"
    assert text_only.reviews == [] and text_only.price is None and text_only.currency is None


EBAY = {
    "organic_results": [
        {
            "title": "Brass lantern",
            "link": "https://www.ebay.co.uk/itm/1",
            "product_id": "1",
            "condition": "Brand New",
            "price": {"raw": "£12.99", "extracted": 12.99},
            "shipping": "Free postage",
            "location": "Located in India",
            "seller": {"username": "moradabad_crafts", "reviews": 500, "positive_feedback_in_percentage": 99.1},
            "sponsored": True,
        },
        {
            "title": "Brass lantern set",
            "price": {"from": {"raw": "£8.00", "extracted": 8.0}, "to": {"raw": "£20.00", "extracted": 20.0}},
            "location": "from India",
            "seller": {"username": "x"},
        },
        {"title": "US lantern", "price": {"raw": "US $15.00", "extracted": 15.0}},
        {"title": "No price", "extensions": ["Located in United Kingdom"]},
    ]
}


def test_ebay_listings_both_price_shapes(serp):
    client, ev, fake = serp({"ebay": EBAY})
    found = E.ebay_listings(client, ev, UK, "brass lantern")
    assert [(l.price, l.currency, l.location, l.merchant, l.sponsored) for l in found] == [
        (12.99, "GBP", "Located in India", "moradabad_crafts", True),
        (8.0, "GBP", "from India", "x", False),
        (15.0, "USD", None, None, False),
        (None, None, "Located in United Kingdom", None, False),
    ]
    assert found[0].channel == "ebay" and found[0].url == "https://www.ebay.co.uk/itm/1"
    sent = fake.params("ebay")[0]
    assert (sent["_nkw"], sent["ebay_domain"]) == ("brass lantern", "ebay.co.uk")


# --------------------------------------------------------------------------- demand


def _day(ts: str) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()


TIMESERIES = {
    "interest_over_time": {
        "timeline_data": [
            {
                "date": "Sep 25 – Oct 1, 2022",
                "timestamp": "1664064000",
                "values": [
                    {"query": "brass lantern", "value": "<1", "extracted_value": 0},
                    {"query": "lantern", "value": "45", "extracted_value": 45},
                ],
            },
            {
                "date": "Oct 2 – 8, 2022",
                "timestamp": "1664668800",
                "values": [{"query": "Brass Lantern", "value": "3"}, {"query": "lantern", "extracted_value": 50}],
            },
            {"date": "Oct 9 – 15, 2022", "values": [{"value": "2"}, {"value": "55"}]},
            {"values": []},
        ]
    }
}


def test_trends_timeseries(serp):
    client, ev, fake = serp({"google_trends": TIMESERIES})
    s = E.trends_timeseries(client, ev, UK, ["brass lantern", "lantern", " "])
    assert s.terms == ["brass lantern", "lantern"]
    assert s.dates == [_day("1664064000"), _day("1664668800"), "2022-10-09"]
    assert s.values == {"brass lantern": [0, 3, 2], "lantern": [45, 50, 55]}
    assert s.evidence_id in ev and "trends.google.com" in ev.get(s.evidence_id).url
    sent = fake.params("google_trends")[0]
    assert (sent["q"], sent["geo"], sent["date"], sent["data_type"]) == ("brass lantern,lantern", "GB", "today 5-y", "TIMESERIES")


def test_trends_timeseries_caps_terms_and_handles_empty(serp):
    client, ev, fake = serp({"google_trends": {"interest_over_time": {}}})
    assert E.trends_timeseries(client, ev, UK, ["a", "b", "c", "d", "e", "f"]) is None
    assert fake.params("google_trends")[0]["q"] == "a,b,c,d,e"
    assert E.trends_timeseries(client, ev, UK, []) is None


def test_trends_regions_strongest_first(serp):
    data = {
        "interest_by_region": [
            {"location": "Manchester", "value": "60", "extracted_value": 60},
            {"location": "London", "value": "100", "extracted_value": 100},
            {"location": "Leeds", "value": "0", "extracted_value": 0},
            {"value": "5"},
        ]
    }
    client, ev, fake = serp({"google_trends": data})
    regions = E.trends_regions(client, ev, UK, "lantern")
    assert [(r.region, r.value) for r in regions] == [("London", 100), ("Manchester", 60)]
    sent = fake.params("google_trends")[0]
    assert (sent["data_type"], sent["region"], sent["q"]) == ("GEO_MAP_0", "CITY", "lantern")
    E.trends_regions(client, ev, UK, "lantern", resolution="REGION")
    assert fake.params("google_trends")[1]["region"] == "REGION"


def test_trends_related_and_autocomplete(serp):
    related = {
        "related_queries": {
            "top": [{"query": "brass lantern large", "value": "100", "extracted_value": 100}, {"query": "gold lantern"}],
            "rising": [{"query": "Gold Lantern"}, {"query": "moroccan lantern", "value": "Breakout"}],
        }
    }
    suggest = {"suggestions": [{"value": "brass lantern"}, {"value": "brass lantern large"}, {"value": "Brass Lantern"}, {}]}
    client, ev, fake = serp({"google_trends": related, "google_autocomplete": suggest})
    assert E.trends_related(client, ev, UK, "brass lantern") == ["brass lantern large", "gold lantern", "moroccan lantern"]
    assert fake.params("google_trends")[0]["data_type"] == "RELATED_QUERIES"
    assert E.autocomplete(client, ev, UK, "brass lantern") == ["brass lantern", "brass lantern large"]
    sent = fake.params("google_autocomplete")[0]
    assert (sent["gl"], sent["hl"]) == ("uk", "en")
    assert len(ev) == 2


# --------------------------------------------------------------------------- buyers' engines


def test_maps_places(serp):
    data = {
        "local_results": [
            {
                "position": 1,
                "title": "Nkuku Showroom",
                "address": "1 High St, London",
                "phone": "+44 20 1234 5678",
                "website": "https://www.nkuku.com/",
                "rating": 4.7,
                "reviews": 210,
                "type": "Home goods store",
                "data_id": "0x1",
            },
            {"title": "Types only", "types": ["Wholesaler", "Store"]},
            {"address": "no title"},
        ]
    }
    client, ev, fake = serp({"google_maps": data})
    places = E.maps_places(client, ev, UK, "homeware shop", ll="@51.5072,-0.1276,12z", city="London")
    assert [(p.title, p.phone, p.website, p.reviews, p.type, p.city) for p in places] == [
        ("Nkuku Showroom", "+44 20 1234 5678", "https://www.nkuku.com/", 210, "Home goods store", "London"),
        ("Types only", None, None, None, "Wholesaler", "London"),
    ]
    assert places[0].data_id == "0x1" and places[0].rating == 4.7
    assert "google.com/maps" in ev.get(places[0].evidence_id).url
    sent = fake.params("google_maps")[0]
    assert (sent["q"], sent["ll"], sent["type"], sent["hl"]) == ("homeware shop", "@51.5072,-0.1276,12z", "search", "en")


def test_maps_single_place_result(serp):
    client, ev, _ = serp({"google_maps": {"place_results": {"title": "Single Place", "website": "https://s.co.uk"}}})
    places = E.maps_places(client, ev, UK, "x", ll="@1,2,12z")
    assert [(p.title, p.website, p.city) for p in places] == [("Single Place", "https://s.co.uk", None)]


def test_web_results(serp):
    data = {
        "organic_results": [
            {"position": 1, "title": "Brass Lanterns | Graham & Green", "link": "https://www.grahamandgreen.co.uk/lanterns", "snippet": "Shop brass lanterns"},
            {"title": "No link"},
            {"title": "Sub", "link": "https://shop.example.co.uk/x"},
        ]
    }
    client, ev, fake = serp({"google": data})
    found = E.web_results(client, ev, UK, "brass lantern stockist")
    assert [(w.domain, w.snippet) for w in found] == [("grahamandgreen.co.uk", "Shop brass lanterns"), ("shop.example.co.uk", None)]
    assert found[1].evidence_id.endswith(":2")
    sent = fake.params("google")[0]
    assert (sent["gl"], sent["hl"], sent["google_domain"]) == ("uk", "en", "google.co.uk") and "location" not in sent


def test_ads_activity_epochs_iso_and_recent_count(serp):
    now = time.time()
    data = {
        "search_information": {"total_results": 57},
        "ad_creatives": [
            {
                "advertiser_id": "AR1",
                "advertiser": "Graham and Green Ltd",
                "ad_creative_id": "CR1",
                "format": "text",
                "total_days_shown": 400,
                "first_shown": int(now - 400 * DAY),
                "last_shown": int(now - 5 * DAY),
            },
            {"advertiser": "Graham and Green Ltd", "first_shown": str(int(now - 100 * DAY)), "last_shown": str(int(now - 20 * DAY))},
            {"first_shown": "2024-01-15", "last_shown": datetime.fromtimestamp(now - 90 * DAY, tz=timezone.utc).isoformat()},
            {"format": "image"},
        ],
    }
    client, ev, fake = serp({"google_ads_transparency_center": data})
    ads = E.ads_activity(client, ev, UK, "grahamandgreen.co.uk")
    assert isinstance(ads, AdsActivity)
    assert (ads.domain, ads.advertiser, ads.total_creatives, ads.active_last_30d) == (
        "grahamandgreen.co.uk",
        "Graham and Green Ltd",
        57,
        2,
    )
    assert ads.first_shown == "2024-01-15"
    assert ads.last_shown == datetime.fromtimestamp(now - 5 * DAY, tz=timezone.utc).date().isoformat()
    sent = fake.params("google_ads_transparency_center")[0]
    assert (sent["text"], sent["region"]) == ("grahamandgreen.co.uk", "2826")


def test_ads_activity_none_without_creatives(serp):
    client, ev, _ = serp({"google_ads_transparency_center": {"ad_creatives": []}})
    assert E.ads_activity(client, ev, UK, "nobody.co.uk") is None
    assert len(ev) == 0


def test_news_articles_flatten_stories(serp):
    data = {
        "news_results": [
            {
                "position": 1,
                "title": "Nkuku opens new store",
                "link": "https://news.example/a",
                "source": {"name": "Retail Gazette", "icon": "i"},
                "date": "09/20/2026, 07:00 AM, +0000 UTC",
                "snippet": "The homeware brand...",
            },
            {
                "title": "Cluster heading",
                "stories": [
                    {"title": "Story one", "link": "https://n/1", "source": {"name": "BBC"}, "date": "2 days ago"},
                    {"title": "Story two", "link": "https://n/2", "source": "Guardian", "iso_date": "2026-09-01T00:00:00Z"},
                ],
            },
            {
                "highlight": {"title": "Top story", "link": "https://n/h", "source": {"name": "FT"}},
                "stories": [{"title": "Nkuku opens new store", "link": "https://news.example/a"}],
            },
        ]
    }
    client, ev, fake = serp({"google_news": data})
    items = E.news_articles(client, ev, UK, '"Nkuku"')
    assert [(n.title, n.source) for n in items] == [
        ("Nkuku opens new store", "Retail Gazette"),
        ("Story one", "BBC"),
        ("Story two", "Guardian"),
        ("Top story", "FT"),
    ]
    assert items[0].date.startswith("09/20/2026") and items[2].date == "2026-09-01T00:00:00Z"
    assert items[1].evidence_id.endswith(":1.0")
    sent = fake.params("google_news")[0]
    assert (sent["q"], sent["gl"], sent["hl"]) == ('"Nkuku"', "uk", "en")


def test_fx_rate(serp):
    client, ev, fake = serp({"google_finance": {"summary": {"title": "GBP / INR", "extracted_price": 112.34}}})
    assert E.fx_rate(client, ev, "GBP-INR") == 112.34
    assert fake.params("google_finance")[0]["q"] == "GBP-INR" and len(ev) == 1
    client, ev, _ = serp({"google_finance": {"summary": {"price": "₹113.10"}}})
    assert E.fx_rate(client, ev, "GBP-INR") == 113.1
    client, ev, _ = serp({"google_finance": {"markets": {}}})
    assert E.fx_rate(client, ev, "GBP-INR") is None and len(ev) == 0


# --------------------------------------------------------------------------- cross-cutting


def test_no_results_error_is_empty_everywhere(serp):
    client, ev, _ = serp({})  # every engine answers "hasn't returned any results"
    assert E.lens_matches(client, ev, UK, url="https://x/y.jpg") == []
    assert E.shopping_listings(client, ev, UK, "q") == []
    assert E.amazon_listings(client, ev, UK, "q") == []
    assert E.ebay_listings(client, ev, UK, "q") == []
    assert E.trends_timeseries(client, ev, UK, ["q"]) is None
    assert E.trends_regions(client, ev, UK, "q") == []
    assert E.trends_related(client, ev, UK, "q") == []
    assert E.autocomplete(client, ev, UK, "q") == []
    assert E.maps_places(client, ev, UK, "q", ll="@1,2,12z") == []
    assert E.web_results(client, ev, UK, "q") == []
    assert E.ads_activity(client, ev, UK, "x.co.uk") is None
    assert E.news_articles(client, ev, UK, "q") == []
    assert E.fx_rate(client, ev, "GBP-INR") is None
    p = E.amazon_product(client, ev, UK, "B0NONE")
    assert (p.asin, p.title, p.origin, p.reviews) == ("B0NONE", None, None, [])


def test_odd_shapes_do_not_raise(serp):
    odd = {"organic_results": {"not": "a list"}, "shopping_results": "nope", "visual_matches": [None, 3, {"title": 5}]}
    client, ev, _ = serp({e: odd for e in ("google", "google_shopping", "google_lens", "amazon", "ebay")})
    assert E.web_results(client, ev, UK, "q") == []
    assert E.shopping_listings(client, ev, UK, "q") == []
    assert E.lens_matches(client, ev, UK, url="https://x/y.jpg") == []
    assert E.amazon_listings(client, ev, UK, "q") == []
    assert E.ebay_listings(client, ev, UK, "q") == []


def test_evidence_ids_registered(serp):
    client, ev, _ = serp({"amazon": AMAZON})
    found = E.amazon_listings(client, ev, UK, "brass lantern")
    assert all(re.fullmatch(r"amazon:[0-9a-f]{8}:\d+", l.evidence_id) for l in found)
    assert len(ev) == len(found) == len({l.evidence_id for l in found})
    rec = ev.get(found[0].evidence_id)
    assert rec.title == "Brass Hurricane Lantern" and rec.url == "https://www.amazon.co.uk/dp/B001"
    assert 'k="brass lantern"' in rec.query and rec.engine == "amazon"
    assert "£24.99" in rec.snippet


def test_budget_exceeded_propagates(serp):
    client, ev, _ = serp({"amazon": AMAZON}, budget=0)
    with pytest.raises(BudgetExceeded):
        E.amazon_listings(client, ev, UK, "brass lantern")


# --------------------------------------------------------------------------- scripts/probe.py metrics


@pytest.fixture(scope="module")
def probe():
    path = Path(__file__).resolve().parents[1] / "scripts" / "probe.py"
    spec = importlib.util.spec_from_file_location("exportscout_probe", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _listings(n_priced, n_unpriced=0, **kw):
    priced = [Listing(channel="amazon", title=f"p{i}", price=10.0 + i, currency="GBP", evidence_id=f"e{i}", **kw) for i in range(n_priced)]
    unpriced = [Listing(channel="amazon", title=f"u{i}", evidence_id=f"u{i}") for i in range(n_unpriced)]
    return priced + unpriced


def test_probe_plan_credits(probe):
    assert probe.estimated_credits(probe.plan_requests(["a.jpg", "https://x/b.jpg"], "grahamandgreen.co.uk", UK)) == 14
    assert probe.estimated_credits(probe.plan_requests([], "grahamandgreen.co.uk", UK)) == 12
    assert probe.estimated_credits(probe.plan_requests(["a", "b", "c"], "x.co.uk", UK)) == 14


def test_probe_dry_run_needs_no_key(probe, capsys, monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    assert probe.main(["--image", "https://example.com/a.jpg"]) == 0
    out = capsys.readouterr().out
    assert "Estimated credits: 13" in out and "google_lens" in out and "--live" in out


def test_probe_thresholds(probe):
    assert probe.lens_check(_listings(5, 3), "a").status == "pass"
    assert probe.lens_check(_listings(4, 9), "a").status == "fail"
    assert probe.amazon_check("q", _listings(20)).status == "pass"
    assert probe.amazon_check("q", _listings(19, 5)).status == "fail"
    assert "bought_last_month on 3" in probe.amazon_check("q", _listings(3, bought_last_month=50)).detail
    assert probe.shopping_check(_listings(10, merchant="M")).status == "pass"
    assert probe.shopping_check(_listings(10)).status == "fail"  # no merchant
    assert probe.fx_check(112.3, "GBP-INR").status == "pass"
    assert probe.fx_check(1.2, "GBP-INR").status == "fail" and probe.fx_check(None, "GBP-INR").status == "fail"


def test_probe_origin_ebay_trends_maps_ads(probe):
    def product(asin, origin=None, signal=None):
        return ProductDetail(asin=asin, origin=origin, origin_text_signal=signal, evidence_id=asin)

    four = [product("1", "India"), product("2", "China"), product("3", signal="Made in India"), product("4")]
    result = probe.origin_check(four)
    assert result.status == "pass" and "India×1" in result.detail and "text signal on 1" in result.detail
    assert probe.origin_check(four[2:]).status == "fail"

    ebay = [Listing(channel="ebay", title="a", location="Located in India", evidence_id="a"), Listing(channel="ebay", title="b", evidence_id="b")]
    assert probe.ebay_check(ebay).status == "pass" and "1 from India" in probe.ebay_check(ebay).detail
    assert probe.ebay_check(ebay[1:]).status == "fail"

    series = TrendsSeries(terms=["brass lantern", "lantern"], dates=["d1", "d2"], values={"brass lantern": [0, 0], "lantern": [0, 4]}, evidence_id="t")
    assert probe.trends_check(series).status == "pass"
    flat = series.model_copy(update={"values": {"brass lantern": [3, 3], "lantern": [0, 0]}})
    assert probe.trends_check(flat).status == "fail" and probe.trends_check(None).status == "fail"

    places = [Place(title=f"p{i}", phone="1" if i % 2 else None, website=None if i % 2 else "w", evidence_id=f"p{i}") for i in range(10)]
    assert probe.maps_check(places).status == "pass"
    assert probe.maps_check(places[:9] + [Place(title="x", evidence_id="x")]).status == "fail"

    ads = AdsActivity(domain="d", total_creatives=3, first_shown="2025-01-01", last_shown="2026-09-20", evidence_id="a")
    assert probe.ads_check(ads, "d").status == "pass"
    assert probe.ads_check(ads.model_copy(update={"first_shown": None}), "d").status == "fail"
    assert probe.ads_check(None, "d").status == "fail"


def test_probe_asins_and_verdict(probe):
    items = [
        Listing(channel="amazon", title="s", asin="S1", sponsored=True, evidence_id="s"),
        Listing(channel="amazon", title="a", asin="A1", evidence_id="a"),
        Listing(channel="amazon", title="a2", asin="A1", evidence_id="a2"),
        Listing(channel="amazon", title="b", asin="B1", evidence_id="b"),
        Listing(channel="amazon", title="c", asin="C1", evidence_id="c"),
    ]
    assert probe.top_asins(items) == ["A1", "B1"]

    R = probe.CheckResult
    two = [R(1, "Lens a", "pass", ""), R(1, "Lens b", "fail", ""), R(2, "Amazon", "error", ""), R(8, "FX", "fail", ""), R(9, "Shop", "fail", "")]
    assert probe.failed_core_checks(two) == [1, 2] and probe.verdict(two) == "GO"
    three = two + [R(5, "Trends", "fail", ""), R(6, "Maps", "skip", "")]
    assert probe.verdict(three) == "RETHINK"
    skipped = [R(1, "Lens", "skip", ""), R(3, "Origin", "skip", ""), R(4, "eBay", "fail", ""), R(5, "Trends", "fail", "")]
    assert probe.verdict(skipped) == "GO"

    report = probe.render_report(three, started="2026-10-01T09:00:00+00:00", credits_used=14, searches_before=240, searches_after=226)
    assert "**RETHINK**" in report and "Credits used: 14" in report and "240 before, 226 after" in report
    assert "| 5 | Trends | FAIL |" in report
    assert "Trends" in probe.format_table(three)


def _probe_responses():
    now = time.time()
    return {
        "google_lens": LENS,
        "amazon": AMAZON,
        "amazon_product": _product_pages,
        "ebay": EBAY,
        "google_trends": TIMESERIES,
        "google_maps": {"local_results": [{"title": f"Shop {i}", "phone": "020 1234"} for i in range(12)]},
        "google_ads_transparency_center": {
            "ad_creatives": [{"advertiser": "G&G", "first_shown": int(now - 90 * DAY), "last_shown": int(now - 2 * DAY)}]
        },
        "google_finance": {"summary": {"extracted_price": 112.3}},
        "google_shopping": SHOPPING,
    }


def test_probe_live_flow_with_mock_client(probe, serp, tmp_path, capsys):
    client, _, fake = serp(_probe_responses(), budget=16)
    report = tmp_path / "day1.md"
    assert probe.run_live(["https://example.com/a.jpg"], "grahamandgreen.co.uk", client=client, report_path=report) == 0
    text = report.read_text(encoding="utf-8")
    # Lens 7 priced, Amazon 2 priced (fail x2), origin 4/4, eBay located, Trends ok, Maps 12, Ads ok, FX ok, Shopping 3 (fail)
    assert "Verdict: **GO** (1 of checks 1-7 failed: 2" in text
    assert "238 before, 238 after" in text and "Credits used: 11" in text
    assert "| 3 | Amazon product origin | PASS | origin field on 4/4 (China×2, India×2)" in text
    assert "| 9 | Shopping" in text and "FAIL" in text
    assert [p["asin"] for p in fake.params("amazon_product")] == ["B002", "B003"]  # repeats are cache hits
    assert "Verdict: GO" in capsys.readouterr().out


def test_probe_live_stops_at_budget(probe, serp, tmp_path):
    client, _, _ = serp(_probe_responses(), budget=3)
    report = tmp_path / "day1.md"
    probe.run_live([], "grahamandgreen.co.uk", client=client, report_path=report)
    text = report.read_text(encoding="utf-8")
    assert "stopped early" in text and "not run: credit budget reached" in text
    assert "| 1 | Lens | SKIP | not run: no --image given |" in text
    assert "Credits used: 3" in text


def test_probe_live_aborts_when_few_searches_left(probe, serp, tmp_path):
    client, _, fake = serp(_probe_responses(), searches_left=5)
    report = tmp_path / "day1.md"
    assert probe.run_live([], "x.co.uk", client=client, report_path=report) == 2
    assert not report.exists() and fake.params("amazon") == []
