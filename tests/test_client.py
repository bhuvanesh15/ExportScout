import io
import json

import httpx
import pytest
from PIL import Image

from exportscout.serp.client import (
    MAX_UPLOAD_BYTES,
    BudgetExceeded,
    CacheMiss,
    SerpApiError,
    SerpClient,
    cache_key,
    prepare_image,
    redact,
)

KEY = "0123456789abcdef0123456789abcdef"


class FakeSerpApi:
    """Stands in for serpapi.com and records every request."""

    def __init__(self, search_response=None, status=200):
        self.search_response = search_response or {
            "search_metadata": {"status": "Success"},
            "organic_results": [{"title": "Brass lantern", "serpapi_link": f"https://serpapi.com/x?api_key={KEY}"}],
        }
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/image":
            return httpx.Response(200, json={"message": "Image uploaded successfully.", "image_id": f"img{len(self.requests)}"})
        if request.url.path == "/account.json":
            return httpx.Response(200, json={"api_key": KEY, "plan_searches_left": 238})
        return httpx.Response(self.status, json=self.search_response)

    def searches(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/search.json"]


def make_client(tmp_path, fake=None, **kwargs):
    fake = fake if fake is not None else FakeSerpApi()
    http = httpx.Client(transport=httpx.MockTransport(fake))
    kwargs.setdefault("fixture_dir", tmp_path / "fixtures")
    client = SerpClient(KEY, cache_path=tmp_path / "cache.sqlite", http=http, **kwargs)
    return client, fake


def offline_http():
    def fail(request):
        raise AssertionError(f"unexpected network call to {request.url.path}")

    return httpx.Client(transport=httpx.MockTransport(fail))


def test_cache_key_ignores_api_key_order_and_bool_style():
    a = cache_key("amazon", {"k": "brass lantern", "amazon_domain": "amazon.co.uk", "api_key": "x"})
    b = cache_key("amazon", {"amazon_domain": "amazon.co.uk", "k": "brass lantern", "no_cache": True})
    assert a == b
    assert cache_key("google", {"q": "a", "safe": True}) == cache_key("google", {"q": "a", "safe": "true"})
    assert cache_key("google", {"q": "a"}) != cache_key("google_news", {"q": "a"})


def test_second_identical_search_is_served_from_cache(tmp_path):
    client, fake = make_client(tmp_path)
    first = client.search("amazon", k="brass lantern", amazon_domain="amazon.co.uk")
    second = client.search("amazon", amazon_domain="amazon.co.uk", k="brass lantern")
    assert (first.source, second.source) == ("network", "cache")
    assert len(fake.searches()) == 1
    assert client.credits_used == 1 and client.cache_hits == 1
    sent = fake.searches()[0].url.params
    assert sent["engine"] == "amazon" and sent["api_key"] == KEY


def test_api_key_never_stored(tmp_path):
    client, _ = make_client(tmp_path, mode="record")
    resp = client.search("google", q="brass planter")
    assert KEY not in json.dumps(resp.data)
    assert "api_key=REDACTED" in resp.data["organic_results"][0]["serpapi_link"]
    client.close()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes(), path


def test_record_then_replay_offline(tmp_path):
    client, _ = make_client(tmp_path, mode="record")
    recorded = client.search("ebay", _nkw="brass lantern", ebay_domain="ebay.co.uk")
    client.close()
    (tmp_path / "cache.sqlite").unlink()  # a fresh clone has only the fixture files

    demo = SerpClient(mode="replay", cache_path=tmp_path / "fresh.sqlite", fixture_dir=tmp_path / "fixtures", http=offline_http())
    replayed = demo.search("ebay", ebay_domain="ebay.co.uk", _nkw="brass lantern")
    assert replayed.source == "fixture"
    assert replayed.data == recorded.data and replayed.fetched_at == recorded.fetched_at
    assert demo.credits_used == 0
    with pytest.raises(CacheMiss):
        demo.search("ebay", _nkw="brass planter", ebay_domain="ebay.co.uk")


def test_replay_needs_no_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    SerpClient(mode="replay", cache_path=tmp_path / "c.sqlite", http=offline_http())
    with pytest.raises(ValueError):
        SerpClient(mode="live", cache_path=tmp_path / "c.sqlite", http=offline_http())


def test_budget_stops_live_searches_but_not_cache_hits(tmp_path):
    client, fake = make_client(tmp_path, budget=1)
    client.search("google", q="one")
    with pytest.raises(BudgetExceeded):
        client.search("google", q="two")
    assert client.search("google", q="one").source == "cache"
    assert len(fake.searches()) == 1 and client.credits_used == 1


def test_expired_entry_is_refetched(tmp_path):
    client, fake = make_client(tmp_path, ttl_hours={"google_news": 0})
    client.search("google_news", q="moradabad exports")
    assert client.search("google_news", q="moradabad exports").source == "network"
    assert len(fake.searches()) == 2


def test_empty_result_is_cached_real_error_is_not(tmp_path):
    empty = FakeSerpApi({"error": "Google hasn't returned any results for this query."})
    client, _ = make_client(tmp_path, empty)
    assert client.search("google", q="zzzz").data["error"].startswith("Google hasn't")
    assert client.search("google", q="zzzz").source == "cache"

    bad = FakeSerpApi({"error": f"Invalid API key. Your key {KEY}"}, status=401)
    client, fake = make_client(tmp_path / "b", bad)
    with pytest.raises(SerpApiError) as err:
        client.search("google", q="brass")
    assert KEY not in str(err.value)
    assert client.credits_used == 0
    with pytest.raises(SerpApiError):
        client.search("google", q="brass")
    assert len(fake.searches()) == 2  # errors are never cached


def _jpeg(size=(64, 64), color=(180, 140, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def test_lens_local_image_uploads_once_and_caches_by_content(tmp_path):
    client, fake = make_client(tmp_path)
    photo = _jpeg()
    first = client.lens(photo, country="gb", hl="en")
    second = client.lens(photo, country="gb", hl="en")
    assert (first.source, second.source) == ("network", "cache")
    uploads = [r for r in fake.requests if r.url.path == "/image"]
    assert len(uploads) == 1
    assert fake.searches()[0].url.params["image_id"] == "img1"
    assert "image_id" not in first.params and "image_sha256" in first.params
    assert client.lens(_jpeg(color=(10, 10, 10)), country="gb", hl="en").source == "network"


def test_lens_requires_exactly_one_input(tmp_path):
    client, _ = make_client(tmp_path)
    with pytest.raises(ValueError):
        client.lens()
    with pytest.raises(ValueError):
        client.lens(_jpeg(), url="https://example.com/a.jpg")


def test_prepare_image_shrinks_big_photos_under_limit():
    noisy = Image.effect_noise((3000, 3000), 100).convert("RGB")
    buf = io.BytesIO()
    noisy.save(buf, "PNG")
    assert len(buf.getvalue()) > MAX_UPLOAD_BYTES
    body, mime = prepare_image(buf.getvalue())
    assert len(body) <= MAX_UPLOAD_BYTES and mime == "image/jpeg"

    small = _jpeg()
    assert prepare_image(small) == (small, "image/jpeg")


def test_account_strips_key(tmp_path):
    client, _ = make_client(tmp_path)
    info = client.account()
    assert info == {"plan_searches_left": 238}


def test_redact_nested():
    data = {"a": [{"link": f"https://x.com/?q=1&api_key={KEY}&b=2"}], "b": KEY}
    out = json.dumps(redact(data, KEY))
    assert KEY not in out and "api_key=REDACTED&b=2" in out


def test_listener_sees_every_response(tmp_path):
    seen = []
    client, _ = make_client(tmp_path, listener=seen.append)
    client.search("google", q="a")
    client.search("google", q="a")
    assert [(r.source, r.credits) for r in seen] == [("network", 1), ("cache", 0)]


def test_prefer_fixtures_reuses_recordings_in_live_mode(tmp_path):
    recorder, fake = make_client(tmp_path, mode="record")
    recorder.search("amazon", k="brass planter", amazon_domain="amazon.co.uk")
    recorder.close()
    (tmp_path / "cache.sqlite").unlink()  # cache gone; only the fixture remains

    live, fake2 = make_client(tmp_path / "live", fixture_dir=tmp_path / "fixtures", prefer_fixtures=True, budget=0)
    resp = live.search("amazon", k="brass planter", amazon_domain="amazon.co.uk")
    assert resp.source == "fixture" and live.credits_used == 0 and not fake2.searches()
