"""Hiring signal: Google Jobs ads for buying roles -> buyer discovery, activity score and India evidence."""
import re

import httpx
import pytest

from exportscout.config import category, market
from exportscout.models import BuyerCandidate, BuyerSignal, EvidenceStore, JobPosting, Listing, ProductIdentity, WebResult
from exportscout.pipeline import buyers as B
from exportscout.pipeline.scoring import buyer_fit
from exportscout.serp import engines as E
from exportscout.serp.client import SerpClient

CAT = category("metal_handicrafts")
UK = market("uk")
PRODUCT = ProductIdentity(
    product_type="hurricane lantern",
    keywords=["brass hurricane lantern", "gold metal lantern"],
    broad_term="lantern",
    material="brass",
)
NO_RESULTS = {"error": "Google hasn't returned any results for this query."}


class FakeSerpApi:
    """Canned JSON per engine (the pattern from test_engines.py)."""

    def __init__(self, responses):
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = dict(request.url.params)
        return httpx.Response(200, json=self.responses.get(params["engine"], NO_RESULTS))

    def params(self, engine):
        return [dict(r.url.params) for r in self.requests if r.url.params.get("engine") == engine]


@pytest.fixture
def serp(tmp_path):
    """serp(responses) -> (client, ev, fake)."""
    clients = []

    def make(responses):
        fake = FakeSerpApi(responses)
        client = SerpClient(
            api_key="test",
            mode="live",
            cache_path=tmp_path / f"serp{len(clients)}.sqlite",
            http=httpx.Client(transport=httpx.MockTransport(fake)),
        )
        clients.append(client)
        return client, EvidenceStore(), fake

    yield make
    for c in clients:
        c.close()


def job(company, title="Buyer", eid="google_jobs:aaaa0000:0", **kw):
    return JobPosting(company=company, title=title, evidence_id=eid, **kw)


def shop(merchant, title, eid, price=None):
    return Listing(
        channel="google_shopping",
        title=title,
        merchant=merchant,
        price=price,
        currency="GBP" if price is not None else None,
        evidence_id=eid,
    )


def discover(listings=(), webs=(), jobs=(), product=PRODUCT):
    return B.discover_candidates(
        listings=list(listings), web=list(webs), places=[], category=CAT, product=product, jobs=jobs
    )


def kinds(candidate):
    return [s.kind for s in candidate.signals]


# --------------------------------------------------------------------------- engine normaliser

KAYU_LINK = "https://www.google.com/search?q=lighting+buyer&ibp=htl;jobs#htidocid=kayu1"
JOBS = {
    "jobs_results": [
        {
            "title": "Lighting Buyer",
            "company_name": "Kayu Home",
            "location": "London, UK",
            "via": "via LinkedIn",
            "share_link": KAYU_LINK,
            "extensions": ["3 days ago", "Full-time"],
            "detected_extensions": {"posted_at": "3 days ago", "schedule_type": "Full-time"},
            "description": (
                "Kayu Home is an independent homeware brand. You will build the lighting range with our makers "
                "in India. Apply to jobs@kayuhome.co.uk or call 020 7946 0123."
            ),
            "job_highlights": [{"title": "Responsibilities", "items": ["Plan sourcing trips", "Own the lantern range"]}],
            "apply_options": [{"title": "LinkedIn", "link": "https://uk.linkedin.com/jobs/view/1"}],
        },
        {  # not a buying role
            "title": "Sales Assistant",
            "company_name": "Kayu Home",
            "via": "via Indeed",
            "detected_extensions": {"posted_at": "1 day ago"},
            "description": "Help customers in our Islington shop. Pieces made in India.",
        },
        {"title": "lighting buyer", "company_name": "KAYU HOME", "via": "via Indeed"},  # duplicate
        {  # India only in job_highlights; link from apply_options
            "title": "Assistant Merchandiser",
            "company_name": "Oka Direct",
            "location": "Manchester",
            "via": "Totaljobs",
            "detected_extensions": {"posted_at": "30+ days ago"},
            "description": "Support the home accessories team.",
            "job_highlights": [{"title": "Qualifications", "items": ["Experience with Indian suppliers"]}],
            "apply_options": [{"title": "Totaljobs", "link": "https://www.totaljobs.com/job/2"}],
        },
        {  # a recruiter; overseas sourcing only; posting age from extensions
            "title": "Senior Buyer - Home",
            "company_name": "Retail Recruitment Partners",
            "extensions": ["21 hours ago", "Full-time"],
            "description": "Our client, a UK retailer, needs a buyer to work with factories in the Far East.",
        },
        {"title": "Buyer"},  # no company
        "garbage",
    ]
}


def test_job_postings_params_filtering_and_fields(serp):
    client, ev, fake = serp({"google_jobs": JOBS})
    jobs = E.job_postings(client, ev, UK, "lighting buyer")

    sent = fake.params("google_jobs")
    assert len(sent) == 1
    assert (sent[0]["engine"], sent[0]["q"], sent[0]["location"], sent[0]["gl"], sent[0]["hl"]) == (
        "google_jobs",
        "lighting buyer",
        "United Kingdom",
        "uk",
        "en",
    )
    # Sales Assistant (not a buying role), the duplicate and the ad without a company are dropped.
    assert [(j.company, j.title, j.via, j.posted, j.posted_days) for j in jobs] == [
        ("Kayu Home", "Lighting Buyer", "LinkedIn", "3 days ago", 3.0),
        ("Oka Direct", "Assistant Merchandiser", "Totaljobs", "30+ days ago", 30.0),
        ("Retail Recruitment Partners", "Senior Buyer - Home", None, "21 hours ago", 0.9),
    ]
    kayu, oka, agency = jobs
    assert (kayu.location, kayu.link, kayu.query) == ("London, UK", KAYU_LINK, "lighting buyer")
    assert oka.link == "https://www.totaljobs.com/job/2" and oka.query == "lighting buyer"


def test_job_postings_flags_and_snippet(serp):
    client, ev, _ = serp({"google_jobs": JOBS})
    kayu, oka, agency = E.job_postings(client, ev, UK, "lighting buyer")

    assert (kayu.mentions_india, kayu.mentions_overseas, kayu.is_recruiter) == (True, True, False)
    assert (oka.mentions_india, oka.mentions_overseas, oka.is_recruiter) == (True, False, False)  # "Indian suppliers" in highlights
    assert (agency.mentions_india, agency.mentions_overseas, agency.is_recruiter) == (False, True, True)
    # The snippet is a window around the India mention, with contact details replaced.
    assert "makers in India" in kayu.snippet and "[email]" in kayu.snippet and "[phone]" in kayu.snippet
    assert "@" not in kayu.snippet and "7946" not in kayu.snippet
    assert oka.snippet == "Support the home accessories team. Qualifications Experience with Indian suppliers"
    assert "Far East" in agency.snippet  # no India: the window is around the overseas mention


def test_job_postings_evidence(serp):
    client, ev, _ = serp({"google_jobs": JOBS})
    jobs = E.job_postings(client, ev, UK, "lighting buyer")
    assert all(re.fullmatch(r"google_jobs:[0-9a-f]{8}:\d+", j.evidence_id) for j in jobs)
    assert [j.evidence_id.rsplit(":", 1)[1] for j in jobs] == ["0", "3", "4"]  # index in jobs_results
    assert len(ev) == 3
    rec = ev.get(jobs[0].evidence_id)
    assert rec.engine == "google_jobs" and rec.title == "Kayu Home is hiring: Lighting Buyer"
    assert rec.url == KAYU_LINK and rec.snippet == "London, UK · 3 days ago · via LinkedIn"
    assert 'q="lighting buyer"' in rec.query and 'location="United Kingdom"' in rec.query
    assert ev.get(jobs[2].evidence_id).title == "Retail Recruitment Partners is hiring: Senior Buyer - Home"


def test_job_postings_no_results_or_odd_shapes(serp):
    client, ev, _ = serp({})  # SerpApi's "hasn't returned any results" answer
    assert E.job_postings(client, ev, UK, "homeware buyer") == [] and len(ev) == 0
    client, ev, _ = serp({"google_jobs": {"jobs_results": {"not": "a list"}}})
    assert E.job_postings(client, ev, UK, "homeware buyer") == [] and len(ev) == 0


# --------------------------------------------------------------------------- mark_recruiters


def test_mark_recruiters_matches_whole_words_from_the_category_list():
    names = [
        "Hays Specialist Recruitment",  # "hays"
        "Reed",
        "Retail Choice Ltd",
        "MICHAEL PAGE International",  # any case
        "Hunter and Harvey",  # config says "hunter & harvey"
        "Reedmace Homeware",  # "reed" only as a prefix
        "Hayes Garden World",
        "Kayu Home",
    ]
    jobs = [job(n) for n in names]
    out = B.mark_recruiters(jobs, CAT)
    assert [j.is_recruiter for j in out] == [True, True, True, True, True, False, False, False]
    assert [j.company for j in out] == names
    assert not any(j.is_recruiter for j in jobs)  # copies: the input is not mutated


def test_mark_recruiters_keeps_engine_flags_and_tolerates_no_list():
    flagged = job("Talent Hub", is_recruiter=True)
    out = B.mark_recruiters([flagged, job("Reed")], {})
    assert [j.is_recruiter for j in out] == [True, False]
    assert out[0] is not flagged
    assert B.mark_recruiters([], CAT) == []


# --------------------------------------------------------------------------- discovery


def test_job_merges_with_the_shopping_merchant_of_the_same_name():
    kayu = job(
        "Kayu Home Ltd",
        "Lighting Buyer",
        "google_jobs:aaaa0000:0",
        posted="3 days ago",
        posted_days=3.0,
        mentions_india=True,
        snippet="…build the lighting range with our makers in India. Apply to [email]…",
    )
    listing = shop("Kayu Home", "Brass hurricane lantern", "google_shopping:bbbb0000:0", price=38.0)
    cands = discover(listings=[listing], jobs=[kayu])

    assert len(cands) == 1
    c = cands[0]
    assert (c.name, c.domain, c.kind, c.sources) == ("Kayu Home", None, "retailer", ["google_shopping", "google_jobs"])
    assert c.listing_prices == [38.0]
    signals = {s.kind: s for s in c.signals}
    assert set(signals) == {"multi_engine", "category_match", "hiring", "india_sourcing"}
    assert signals["multi_engine"].detail == "Seen in Google Shopping, Google Jobs"
    assert signals["category_match"].evidence_id == "google_shopping:bbbb0000:0"
    assert signals["hiring"].detail == "Hiring a Lighting Buyer (3 days ago)"
    # The engine's cut words ("build", "[email]") may be partial, so they are dropped.
    assert signals["india_sourcing"].detail == "Their job ad mentions India: “…the lighting range with our makers in India. Apply to…”"
    assert signals["hiring"].evidence_id == signals["india_sourcing"].evidence_id == "google_jobs:aaaa0000:0"
    assert "Lighting Buyer" in c.seen_texts and "" not in c.seen_texts


def test_job_merges_into_a_domain_candidate():
    page = WebResult(
        title="Brass Lanterns | Graham & Green",
        link="https://www.grahamandgreen.co.uk/lanterns",
        domain="grahamandgreen.co.uk",
        evidence_id="google:dddd0000:0",
    )
    cands = discover(webs=[page], jobs=[job("Graham and Green", "Homewares Buyer", "google_jobs:aaaa0000:1")])
    assert len(cands) == 1
    c = cands[0]
    assert c.domain == "grahamandgreen.co.uk" and c.sources == ["google", "google_jobs"]
    assert "hiring" in kinds(c) and "multi_engine" in kinds(c)


def test_job_only_candidate():
    oka = job("Oka Direct", "Assistant Merchandiser", "google_jobs:aaaa0000:3", posted="30+ days ago", posted_days=30.0)
    [c] = discover(jobs=[oka])
    assert (c.name, c.domain, c.website, c.kind, c.sources, c.is_giant) == (
        "Oka Direct",
        None,
        None,
        "unknown",
        ["google_jobs"],
        False,
    )
    assert [(s.kind, s.detail, s.evidence_id) for s in c.signals] == [
        ("hiring", "Hiring an Assistant Merchandiser (30+ days ago)", "google_jobs:aaaa0000:3")
    ]
    assert c.seen_texts == ["Assistant Merchandiser"]


def test_recruiters_never_become_candidates():
    jobs = [
        job("Hays Specialist Recruitment", "Lighting Buyer", "google_jobs:aaaa0000:5"),  # on the category list
        job("Buyers Direct", "Homewares Buyer", "google_jobs:aaaa0000:6", is_recruiter=True),  # flagged by the engine
        job("Kayu Home", "Lighting Buyer", "google_jobs:aaaa0000:0"),
    ]
    assert [c.name for c in discover(jobs=jobs)] == ["Kayu Home"]
    assert [c.name for c in discover(jobs=B.mark_recruiters(jobs, CAT))] == ["Kayu Home"]


def test_undisclosed_employers_and_marketplaces_are_skipped_giants_flagged():
    jobs = [job("Confidential"), job("Amazon", "Home Buyer"), job("Dunelm", "Lighting Buyer")]
    got = {c.name: c for c in discover(jobs=jobs)}
    assert set(got) == {"Dunelm"} and got["Dunelm"].is_giant


def test_hiring_signal_uses_the_most_recent_ad():
    jobs = [
        job("Kayu Home", "Homewares Buyer", "google_jobs:aaaa0000:1"),  # no posting date: last
        job("Kayu Home", "Lighting Buyer", "google_jobs:aaaa0000:2", posted="30+ days ago", posted_days=30.0, mentions_india=True),
        job("Kayu Home", "Assistant Buyer", "google_jobs:bbbb0000:0", posted="2 days ago", posted_days=2.0),
    ]
    [c] = discover(jobs=jobs)
    assert [(s.kind, s.detail, s.evidence_id) for s in c.signals] == [
        ("hiring", "Hiring an Assistant Buyer (2 days ago)", "google_jobs:bbbb0000:0"),
        ("india_sourcing", "Their job ad for a Lighting Buyer mentions India", "google_jobs:aaaa0000:2"),
    ]
    assert c.sources == ["google_jobs"]  # one engine, so no multi_engine signal
    [undated] = discover(jobs=jobs[:1])
    assert [(s.kind, s.detail) for s in undated.signals] == [("hiring", "Hiring a Homewares Buyer")]


def test_india_quote_is_trimmed_around_the_mention():
    snippet = (
        "…we work with " + "family workshops and small makers, " * 3
        + "mostly in India, and we visit them twice a year to agree new ranges and finishes…"
    )
    [c] = discover(jobs=[job("Kayu Home", "Lighting Buyer", mentions_india=True, snippet=snippet)])
    detail = next(s.detail for s in c.signals if s.kind == "india_sourcing")
    assert detail.startswith("Their job ad mentions India: “…") and detail.endswith("…”")
    quote = detail.removeprefix("Their job ad mentions India: “").removesuffix("”")
    assert "mostly in India" in quote and len(quote) <= B.JOB_QUOTE_CHARS + 2


def test_engine_to_discovery_to_fit(serp):
    client, ev, _ = serp({"google_jobs": JOBS})
    jobs = B.mark_recruiters(E.job_postings(client, ev, UK, "lighting buyer"), CAT)
    listing = shop("Kayu Home", "Brass hurricane lantern", "google_shopping:bbbb0000:0", price=45.0)
    got = {c.name: c for c in discover(listings=[listing], jobs=jobs)}
    assert set(got) == {"Kayu Home", "Oka Direct"}  # the recruiter's ad is not a buyer

    kayu_job = jobs[0]
    kayu = buyer_fit(got["Kayu Home"], PRODUCT, ladder=None, quote=None)
    assert kayu.fit["india"].points == 20 and kayu.fit["india"].evidence_ids == [kayu_job.evidence_id]
    india_detail = kayu.fit["india"].detail
    assert india_detail.startswith("Their job ad mentions India: “…") and "makers in India. Apply to [email]" in india_detail
    assert kayu.fit["activity"].points == 5 and kayu.fit["activity"].detail == "Hiring a Lighting Buyer (3 days ago)"
    assert kayu.fit["activity"].evidence_ids == [kayu_job.evidence_id]
    oka = got["Oka Direct"]
    india = next(s for s in oka.signals if s.kind == "india_sourcing")
    assert india.evidence_id == jobs[1].evidence_id and "Indian suppliers" in india.detail
    assert all(s.evidence_id in ev for c in got.values() for s in c.signals if s.kind in ("hiring", "india_sourcing"))


# --------------------------------------------------------------------------- scoring and ranking

HIRING = BuyerSignal(kind="hiring", detail="Hiring a Lighting Buyer (3 days ago)", evidence_id="google_jobs:aaaa0000:0")


def sig(kind):
    return BuyerSignal(kind=kind, detail=f"{kind} seen", evidence_id=f"google:cccc3333:{kind}")


def fit(signals=(), **kw):
    return buyer_fit(BuyerCandidate(name="Kayu Home", signals=list(signals), **kw), PRODUCT, ladder=None, quote=None)


@pytest.mark.parametrize(
    "signal_kinds, creatives, expected",
    [
        ([], None, 0),
        (["hiring"], None, 5),
        (["hiring"], 3, 9),  # 4 for older ads on record + 5
        (["news", "hiring"], None, 10),
        (["ads_active", "hiring"], 12, 15),
        (["ads_active", "news", "hiring"], 12, 15),  # 20, capped at 15
    ],
)
def test_activity_counts_hiring(signal_kinds, creatives, expected):
    signals = [HIRING if k == "hiring" else sig(k) for k in signal_kinds]
    assert fit(signals, ad_creatives=creatives).fit["activity"].points == expected


def test_activity_detail_and_evidence_include_hiring():
    activity = fit([sig("ads_active"), sig("news"), HIRING], ad_creatives=12).fit["activity"]
    assert (activity.points, activity.max_points) == (15, 15)
    assert activity.detail == "ads_active seen; news seen; Hiring a Lighting Buyer (3 days ago)"
    assert activity.evidence_ids == ["google:cccc3333:ads_active", "google:cccc3333:news", "google_jobs:aaaa0000:0"]
    assert fit().fit["activity"].detail.startswith("No recent ads")


def test_hiring_adds_5_to_the_fit_score_up_to_the_cap():
    assert fit([sig("news"), HIRING]).fit_score - fit([sig("news")]).fit_score == 5
    assert fit([HIRING]).fit_score - fit().fit_score == 5
    at_cap = [sig("ads_active"), sig("news")]
    assert fit([*at_cap, HIRING]).fit_score == fit(at_cap).fit_score  # activity is already 15


def test_prior_score_hiring_bonus():
    plain = BuyerCandidate(name="Kayu Home", sources=["google_jobs"])
    hiring = plain.model_copy(update={"signals": [HIRING]})
    assert B.prior_score(hiring) == B.prior_score(plain) + 1.5
    web_only = BuyerCandidate(name="Other", domain="other.co.uk", sources=["google"])
    assert [c.name for c in B.rank_for_enrichment([web_only, hiring], 2)] == ["Kayu Home", "Other"]


def test_group_companies_merges_name_variants():
    from exportscout.pipeline.buyers import group_companies

    names = ["QVC, Inc.", "QVC", "Dobbies Central Support Office", "Dobbies", "Dobbies Garden Centres",
             "Online Home Shop", "Online Lighting"]
    jobs = [JobPosting(company=n, title="Buyer", evidence_id=f"google_jobs:x:{i}") for i, n in enumerate(names)]
    assert [j.company for j in group_companies(jobs)] == [
        "QVC", "QVC", "Dobbies", "Dobbies", "Dobbies", "Online Home Shop", "Online Lighting"
    ]
