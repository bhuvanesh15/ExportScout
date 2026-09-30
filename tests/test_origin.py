import pytest

from exportscout.models import Listing, ProductDetail
from exportscout.pipeline.origin import classify_origin, origin_shares


def product(origin=None, signal=None, asin="B000000001", i=0):
    return ProductDetail(
        asin=asin, origin=origin, origin_text_signal=signal, evidence_id=f"amazon_product:aaaa1111:{i}"
    )


def ebay(location, i=0, channel="ebay"):
    return Listing(
        channel=channel,
        title=f"Brass lantern {i}",
        price=30.0,
        currency="GBP",
        location=location,
        evidence_id=f"ebay:bbbb2222:{i}",
    )


@pytest.mark.parametrize(
    "origin, signal, expected",
    [
        ("India", None, "india"),
        ("  INDIA ", None, "india"),
        ("Made in India", None, "india"),
        ("China", None, "china"),
        ("china", "Handmade in India", "china"),  # the origin field wins over the text signal
        ("PRC", None, "china"),
        ("People's Republic of China", None, "china"),
        ("People’s Republic of China", None, "china"),
        ("Vietnam", None, "other"),
        ("United Kingdom", "Indian style", "other"),
        ("People's Republic of Bangladesh", None, "other"),
        ("Indiana", None, "other"),
        ("Not specified", "Handmade in India", "india"),
        ("unknown", None, "unknown"),
        ("N/A", None, "unknown"),
        ("", "Hand-crafted by Indian artisans", "india"),
        (None, "Handmade in India", "india"),
        (None, "Made in Indiana", "unknown"),
        (None, "Made in China", "unknown"),  # only India is read from the text signal
        (None, None, "unknown"),
    ],
)
def test_classify_origin(origin, signal, expected):
    assert classify_origin(product(origin, signal)) == expected


def test_origin_shares_counts_and_evidence():
    products = [
        product("India", asin="A1", i=0),
        product(None, "Handmade in India", asin="A2", i=1),
        product("China", asin="A3", i=2),
        product("China", asin="A4", i=3),
        product("Turkey", asin="A5", i=4),
        product(None, None, asin="A6", i=5),
        product("China", asin="A3", i=6),  # duplicate ASIN is ignored
    ]
    listings = [
        ebay("Located in India", i=0),
        ebay("Moradabad, india", i=1),
        ebay("United Kingdom", i=2),
        ebay(None, i=3),
        ebay("Indianapolis, Indiana, United States", i=4),
        ebay("India", i=5, channel="amazon"),  # not an eBay listing
    ]
    s = origin_shares(products, listings)
    assert (s.checked, s.india, s.china, s.other, s.unknown) == (6, 2, 2, 1, 1)
    assert s.india_share == pytest.approx(2 / 5)
    assert (s.ebay_total, s.ebay_from_india) == (5, 2)
    assert s.india_examples == [
        "amazon_product:aaaa1111:0",
        "amazon_product:aaaa1111:1",
        "ebay:bbbb2222:0",
        "ebay:bbbb2222:1",
    ]
    assert len(s.evidence_ids) == 11
    assert "amazon_product:aaaa1111:6" not in s.evidence_ids


def test_origin_shares_empty():
    s = origin_shares([], [])
    assert s.checked == 0 and s.ebay_total == 0
    assert s.india_share is None
    assert s.india_examples == [] and s.evidence_ids == []
