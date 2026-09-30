import time
from datetime import date, timedelta

import httpx
import pytest

from exportscout.agent import orchestrator as O
from exportscout.config import category
from exportscout.models import BuyerCandidate, BuyerSignal, ProductIdentity, RunInputs, TrendsSeries
from exportscout.serp.client import CacheMiss, SerpClient


def _weeks(n=260):
    start = date(2021, 10, 3)
    return [start + timedelta(weeks=i) for i in range(n)]


def _trends(q: str) -> dict:
    terms = q.split(",")
    rows = []
    for d in _weeks():
        values = []
        for t in terms:
            v = 0
            if t == "candle lantern":
                v = 60 if d.month in (10, 11, 12) else 25
            values.append({"query": t, "value": str(v), "extracted_value": v})
        rows.append({"date": d.isoformat(), "timestamp": str(int(time.mktime(d.timetuple()))), "values": values})
    return {"interest_over_time": {"timeline_data": rows}}


def _responses(engine: str, params: httpx.QueryParams) -> dict:
    now = int(time.time())
    if engine == "google_trends":
        if params.get("data_type") == "RELATED_QUERIES":
            return {"related_queries": {"top": [{"query": "large candle lantern", "extracted_value": 100}]}}
        if params.get("data_type") == "GEO_MAP_0":
            return {"interest_by_region": [
                {"location": name, "extracted_value": v}
                for name, v in (("United Kingdom", 68), ("Australia", 36), ("United States", 36), ("United Arab Emirates", 12))
            ]}
        return _trends(params["q"])
    if engine == "google_jobs":
        return {"jobs_results": [
            {"title": "Buyer - Home Accessories", "company_name": "Kayu Home", "location": "London", "via": "via LinkedIn",
             "description": "Grow our range with artisan suppliers in India.", "detected_extensions": {"posted_at": "3 days ago"}},
            {"title": "Senior Buyer - Homeware", "company_name": "Hays", "location": "United Kingdom",
             "description": "Our client sources from the Far East."},
            {"title": "Sales Assistant", "company_name": "Pooky", "location": "London"},
        ]}
    if engine == "google_autocomplete":
        return {"suggestions": [{"value": "brass hurricane lamp"}, {"value": "brass storm lantern"}]}
    if engine == "google_shopping":
        return {
            "shopping_results": [
                {"title": f"Brass hurricane lantern {i}", "source": ["Kayu Home", "Pooky", "Casa by JJ"][i % 3],
                 "price": f"£{30 + i}.00", "extracted_price": 30.0 + i}
                for i in range(12)
            ]
        }
    if engine == "amazon":
        return {
            "organic_results": [
                {"asin": f"B0{i:08d}", "title": f"Brass lantern {i}", "extracted_price": 35.0 + i, "price": f"£{35 + i}",
                 "reviews": 100 - i, "rating": 4.2}
                for i in range(10)
            ]
        }
    if engine == "amazon_product":
        return {
            "product_results": {"title": "Brass lantern", "brand": "Lumora"},
            "product_details": {"country_of_origin": "China"},
            "reviews_information": {"authors_reviews": [
                {"title": "Arrived broken", "text": "The glass arrived broken and the metal is thin.", "rating": 2},
                {"title": "Lovely", "text": "Beautiful lantern, looks premium.", "rating": 5},
            ]},
        }
    if engine == "ebay":
        return {
            "organic_results": [
                {"title": f"Indian brass lantern {i}", "price": {"raw": f"£{40 + i}", "extracted": 40.0 + i},
                 "location": "Located in India" if i % 2 else "Located in United Kingdom"}
                for i in range(8)
            ]
        }
    if engine == "google_finance":
        return {"summary": {"extracted_price": 127.4}}
    if engine == "google":
        return {
            "organic_results": [
                {"title": "Kayu Home | Lanterns — trade accounts welcome", "link": "https://www.kayuhome.co.uk/trade",
                 "snippet": "Handmade in India brass lanterns for UK homes."},
                {"title": "Pooky lanterns", "link": "https://www.pooky.com/lanterns", "snippet": "Antiqued brass lantern range."},
            ]
        }
    if engine == "google_maps":
        return {
            "local_results": [
                {"title": "Hurricane Homeware Wholesale", "address": "London", "phone": "020 0000 0000",
                 "website": "https://hurricanehome.co.uk", "type": "Home goods wholesaler", "reviews": 40}
            ]
        }
    if engine == "google_ads_transparency_center":
        return {"ad_creatives": [{"advertiser": "Kayu Home Ltd", "first_shown": now - 90 * 86400, "last_shown": now - 86400}]}
    if engine == "google_news":
        return {"news_results": []}
    return {"error": "Google hasn't returned any results for this query."}


class FakeSerpApi:
    def __init__(self):
        self.engines: list[str] = []
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        engine = request.url.params.get("engine")
        self.engines.append(engine)
        self.calls.append((engine, dict(request.url.params)))
        return httpx.Response(200, json=_responses(engine, request.url.params))


def _client(tmp_path, budget=O.DEFAULT_BUDGET):
    fake = FakeSerpApi()
    client = SerpClient("k" * 64, cache_path=tmp_path / "c.sqlite", budget=budget, http=httpx.Client(transport=httpx.MockTransport(fake)))
    return client, fake


INPUTS = RunInputs(description="brass hurricane lantern", unit_cost_inr=650, extra_costs_inr=60, moq=200, material="brass")


def test_full_run_end_to_end(tmp_path):
    serp, fake = _client(tmp_path)
    events = []
    brief = O.run_scout(INPUTS, serp=serp, llm=None, on_event=events.append)

    assert brief.quote is not None and brief.quote.verdict == "go"
    assert brief.demand is not None and brief.demand.term == "candle lantern" and brief.demand.is_proxy
    assert brief.demand.peak_months[0] in (10, 11, 12)
    assert brief.origin.checked > 0 and brief.origin.china == brief.origin.checked
    assert any(t.kind == "complaint" for t in brief.themes)
    assert brief.buyers and brief.pitches and brief.summary_md
    kayu = next(b for b in brief.buyers if b.name == "Kayu Home")
    assert kayu.domain == "kayuhome.co.uk" and kayu.enriched  # site found for a Shopping merchant
    assert {s.kind for s in kayu.signals} >= {"trade_page", "india_sourcing", "ads_active"}
    assert brief.credits_used == serp.credits_used <= O.DEFAULT_BUDGET
    assert {e.name for e in events} >= {"Identify", "Demand", "Prices", "Origin", "Reviews", "Buyers", "Enrich", "Markets", "Write"}
    assert fake.engines.count("google_trends") == 3  # one comparison + related queries + interest by country


def test_hiring_signal_from_google_jobs(tmp_path):
    serp, fake = _client(tmp_path)
    brief = O.run_scout(INPUTS, serp=serp, llm=None)
    queries = [c["q"] for e, c in fake.calls if e == "google_jobs"]
    assert queries == ["homeware buyer", "home accessories buyer"]
    assert all(c["location"] == "United Kingdom" and c["gl"] == "uk" for e, c in fake.calls if e == "google_jobs")
    assert {j.company for j in brief.jobs} == {"Kayu Home", "Hays"}  # the sales assistant ad is not a buying role
    assert next(j for j in brief.jobs if j.company == "Hays").is_recruiter
    kayu = next(b for b in brief.buyers if b.name == "Kayu Home")
    hiring = [s for s in kayu.signals if s.kind == "hiring"]
    assert hiring and "Buyer - Home Accessories" in hiring[0].detail
    assert "google_jobs" in kayu.sources
    assert not any(b.name == "Hays" for b in brief.buyers)


def test_market_compare_ranks_other_markets(tmp_path):
    serp, fake = _client(tmp_path)
    brief = O.run_scout(INPUTS, serp=serp, llm=None)
    assert {r.code for r in brief.markets} == {"uk", "us", "de", "ae", "au"}
    home = next(r for r in brief.markets if r.is_home)
    assert home.code == "uk" and home.quote is not None and home.trends_interest == 68
    domains = {c["amazon_domain"]: c["k"] for e, c in fake.calls if e == "amazon"}
    assert set(domains) >= {"amazon.co.uk", "amazon.com", "amazon.de", "amazon.ae", "amazon.com.au"}
    assert domains["amazon.com"] == "brass hurricane lantern"
    assert domains["amazon.de"] != "brass hurricane lantern"  # German phrase (word-map fallback without the LLM)
    fx = {c["q"] for e, c in fake.calls if e == "google_finance"}
    assert fx >= {"GBP-INR", "USD-INR", "EUR-INR", "AED-INR", "AUD-INR"}
    us = next(r for r in brief.markets if r.code == "us")
    assert us.duty_pct == pytest.approx(0.157)  # 10% Section 301 + 5.7% for brass lamps (HS 9405.50)
    totals = [r.score.total for r in brief.markets]
    assert totals == sorted(totals, reverse=True)
    assert any(e.name == "Markets" and "Margin at retailer FOB" in e.message for e in brief.steps)


def test_market_compare_can_be_switched_off(tmp_path):
    serp, fake = _client(tmp_path)
    brief = O.run_scout(INPUTS, serp=serp, llm=None, compare_markets=False)
    assert brief.markets == []
    assert fake.engines.count("google_trends") == 2
    assert {c["amazon_domain"] for e, c in fake.calls if e == "amazon"} == {"amazon.co.uk"}


def test_price_below_cost_skips_enrichment(tmp_path):
    serp, fake = _client(tmp_path)
    brief = O.run_scout(INPUTS.model_copy(update={"unit_cost_inr": 5000}), serp=serp, llm=None)
    assert brief.quote.verdict == "no_go"
    assert "google_ads_transparency_center" not in fake.engines
    assert any("below your cost" in w for w in brief.warnings)
    assert brief.markets  # Market Compare still runs, to look for another market
    assert any(e.name == "Markets" and e.status == "warning" for e in brief.steps)


def test_budget_runs_out_gracefully(tmp_path):
    serp, _ = _client(tmp_path, budget=6)
    brief = O.run_scout(INPUTS, serp=serp, llm=None)
    assert serp.credits_used <= 6
    assert brief.headline  # still a brief, built from what was found


def test_demo_mode_miss_before_any_search_raises(tmp_path):
    serp = SerpClient(mode="replay", cache_path=tmp_path / "c.sqlite", fixture_dir=tmp_path / "none")
    with pytest.raises(CacheMiss):
        O.run_scout(INPUTS, serp=serp, llm=None)


def test_needs_photo_or_description(tmp_path):
    serp, _ = _client(tmp_path)
    with pytest.raises(ValueError):
        O.run_scout(RunInputs(), serp=serp, llm=None)


def test_trend_terms_leave_out_one_word_terms():
    product = ProductIdentity(
        product_type="brass hurricane lantern",
        keywords=["brass hurricane lantern", "brass lantern", "hurricane lantern"],
        broad_term="candle lantern",
    )
    terms, generic = O._trend_terms(product, category("metal_handicrafts"))
    assert terms[0] == "brass hurricane lantern" and "candle lantern" in terms and "lantern" not in terms
    assert generic == "lantern" and len(terms) <= 5


def test_pick_term_prefers_most_specific_steady_term():
    dates = [d.isoformat() for d in _weeks(120)]
    series = TrendsSeries(
        terms=["a", "b", "c"],
        dates=dates,
        values={"a": [0] * 120, "b": [0, 30] * 60, "c": [20] * 120},
        evidence_id="t:1",
    )
    assert O._pick_term(series, ["a", "b", "c"]) == "c"  # "b" is non-zero in only half the weeks
    assert O._pick_term(None, ["a"]) is None


def test_merge_by_domain_combines_signals():
    sig = lambda k, e: BuyerSignal(kind=k, detail=k, evidence_id=e)  # noqa: E731
    a = BuyerCandidate(name="Kayu Home", domain="kayuhome.co.uk", sources=["google_shopping"], signals=[sig("category_match", "s:1")])
    b = BuyerCandidate(name="Kayu", domain="kayuhome.co.uk", sources=["google"], signals=[sig("trade_page", "g:1")], phone="1")
    c = BuyerCandidate(name="Pooky", sources=["google_shopping"])
    out = O._merge_by_domain([a, c, b])
    assert [x.name for x in out] == ["Kayu Home", "Pooky"]
    assert out[0].sources == ["google_shopping", "google"] and out[0].phone == "1"
    assert {s.kind for s in out[0].signals} == {"category_match", "trade_page"}


def test_hiring_picks_prefer_the_product_query_and_skip_giants():
    from exportscout.models import JobPosting

    jobs = [
        JobPosting(company="Robert Dyas", title="Buyer", query="homeware buyer", posted_days=2, evidence_id="j:1"),
        JobPosting(company="Dobbies", title="Garden Buyer", query="garden buyer", posted_days=9, evidence_id="j:2"),
        JobPosting(company="Big Chain", title="Buyer", query="garden buyer", posted_days=1, evidence_id="j:3"),
    ]
    hiring = lambda name, eid, giant=False: BuyerCandidate(  # noqa: E731
        name=name, is_giant=giant, signals=[BuyerSignal(kind="hiring", detail="Hiring a Buyer", evidence_id=eid)]
    )
    picks = O._hiring_picks(
        [hiring("Robert Dyas", "j:1"), hiring("Dobbies", "j:2"), hiring("Big Chain", "j:3", giant=True), BuyerCandidate(name="Other")],
        jobs,
        ["garden buyer"],
    )
    assert [c.name for c in picks] == ["Dobbies", "Robert Dyas"]
