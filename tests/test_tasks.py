import json
import re
from types import SimpleNamespace

import pytest

from exportscout.config import category
from exportscout.llm import prompts
from exportscout.llm.client import LLM
from exportscout.llm.tasks import SIGN_OFF, BriefText, clean_citations, tag_buyers, write_brief, write_pitches
from exportscout.models import (
    Brief,
    BuyerCandidate,
    BuyerSignal,
    DemandCard,
    Evidence,
    Listing,
    OriginShare,
    PriceLadder,
    ProductIdentity,
    QuoteRange,
    ReviewSnippet,
    ReviewTheme,
    RunInputs,
)
from exportscout.pipeline.identify import identify_product
from exportscout.pipeline.reviews import MAX_COMPLAINTS, MAX_PRAISE, review_themes

CAT = category("metal_handicrafts")
PRODUCT = ProductIdentity(
    product_type="brass hurricane lantern", keywords=["brass hurricane lantern", "brass lantern", "gold lantern"],
    broad_term="lantern", material="brass",
)  # fmt: skip
PROMPT_NAMES = {
    prompts.IDENTIFY: "identify",
    prompts.REVIEWS: "reviews",
    prompts.TAG_BUYERS: "tag_buyers",
    prompts.BRIEF: "brief",
    prompts.PITCHES: "pitches",
}


class FakeAnthropic:
    """Anthropic stand-in: routes each call by system prompt to ``responses[name]``."""

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[str] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse, create=self._parse))

    def _parse(self, **kw):
        name = PROMPT_NAMES[kw["system"][0]["text"]]
        self.calls.append(name)
        out = self.responses[name]
        out = out(kw) if callable(out) else out
        return SimpleNamespace(
            stop_reason="end_turn",
            stop_details=None,
            parsed_output=kw["output_format"].model_validate(out),
            content=[SimpleNamespace(type="text", text=json.dumps(out))],
        )


def fake_llm(tmp_path, **responses):
    fake = FakeAnthropic(responses)
    return LLM(mode="live", client=fake, cache_path=tmp_path / "llm.sqlite"), fake


def boom(kw):
    raise RuntimeError("model exploded")


def lens(*titles):
    return [Listing(channel="lens", title=t, evidence_id=f"google_lens:aaaa0000:{i}") for i, t in enumerate(titles)]


TITLES = lens(
    "Antique Brass Hurricane Lantern 30cm with Glass",
    "Gold Metal Candle Lantern Moroccan style",
    "Hurricane lamp brass finish large",
    "Brass lanterns set of 2",
)


# --------------------------------------------------------------------------- identify


def test_identify_fallback_from_titles():
    p = identify_product(None, TITLES, hint=None, material=None, category=CAT)
    assert p.product_type == "brass hurricane lantern"
    assert p.keywords[:2] == ["brass hurricane lantern", "brass lantern"]
    assert 3 <= len(p.keywords) <= 5 and "brass bar set" not in p.keywords
    assert all(k.endswith("lantern") for k in p.keywords)
    assert p.broad_term == "candle lantern" and p.material == "brass"
    assert "antique" in p.style_tags


def test_identify_fallback_puts_hint_first_and_pads():
    p = identify_product(None, [], hint="Hammered Copper Bowl", material="copper", category=CAT)
    assert p.product_type == "hammered copper bowl"
    assert p.keywords[0] == "hammered copper bowl" and len(p.keywords) >= 3
    assert p.broad_term == "decorative bowl" and p.material == "copper"


def test_identify_llm_output_is_cleaned_and_padded(tmp_path):
    llm, fake = fake_llm(
        tmp_path,
        identify={
            "product_type": " Brass Hurricane Lantern ",
            "keywords": ["Brass Hurricane Lantern", "brass hurricane lantern "],
            "broad_term": "",
            "material": "gold",
            "style_tags": ["Antique"],
        },
    )
    p = identify_product(llm, TITLES, hint=None, material="Brass", category=CAT)
    assert fake.calls == ["identify"]
    assert p.product_type == "brass hurricane lantern"
    assert p.keywords[0] == "brass hurricane lantern" and len(p.keywords) == 3 and len(set(p.keywords)) == 3
    assert p.broad_term == "candle lantern"
    assert p.material == "brass"  # the owner's stated material wins
    assert p.style_tags == ["antique"]


def test_identify_falls_back_when_llm_fails_or_is_unavailable(tmp_path):
    expected = identify_product(None, TITLES, hint=None, material=None, category=CAT)
    llm, _ = fake_llm(tmp_path, identify=boom)
    assert identify_product(llm, TITLES, hint=None, material=None, category=CAT) == expected
    no_key = LLM(mode="live", api_key="", cache_path=tmp_path / "b.sqlite")
    assert identify_product(no_key, TITLES, hint=None, material=None, category=CAT) == expected
    replay = LLM(mode="replay", record_dir=tmp_path / "none", cache_path=tmp_path / "c.sqlite")
    assert identify_product(replay, TITLES, hint=None, material=None, category=CAT) == expected


# --------------------------------------------------------------------------- reviews

REVIEWS = [
    ReviewSnippet(text="Lovely lantern but it went black after a month.", rating=3, evidence_id="amazon_product:11111111:r0"),
    ReviewSnippet(text="Beautiful piece, looks expensive.", rating=5, evidence_id="amazon_product:11111111:r1"),
    ReviewSnippet(text="Tarnished within weeks. Disappointed.", title="Tarnish", rating=2, evidence_id="amazon_product:11111111:r2"),
    ReviewSnippet(text="Arrived broken, glass smashed.", rating=1, evidence_id="amazon_product:22222222:r0"),
    ReviewSnippet(text="Started to discolour quickly.", rating=2, evidence_id="amazon_product:22222222:r1"),
]


def test_review_counts_come_from_valid_indices_only(tmp_path):
    llm, _ = fake_llm(
        tmp_path,
        reviews={
            "themes": [
                {"label": "Beautiful look", "kind": "praise", "review_indices": [1], "quotes": ["looks expensive"], "fix": "ignored"},
                {
                    "label": "Tarnishing",
                    "kind": "complaint",
                    "review_indices": [0, 2, 2, -1, 99, 4],
                    "quotes": ["it went black after a month", "an invented quote"],
                    "fix": "Offer anti-tarnish lacquer.",
                },
                {"label": "Ghost theme", "kind": "complaint", "review_indices": [42], "quotes": [], "fix": "x"},
                {"label": "Broken glass", "kind": "complaint", "review_indices": [3], "quotes": ["nothing verbatim"], "fix": "Better cartons."},
            ]
        },
    )
    themes = review_themes(llm, REVIEWS, CAT)
    assert [t.label for t in themes] == ["Tarnishing", "Broken glass", "Beautiful look"]
    tarnish = themes[0]
    assert tarnish.count == 3 and tarnish.share == pytest.approx(3 / 5)
    assert tarnish.evidence_ids == ["amazon_product:11111111:r0", "amazon_product:11111111:r2", "amazon_product:22222222:r1"]
    assert tarnish.quotes == ["it went black after a month"]
    assert tarnish.fix == "Offer anti-tarnish lacquer."
    assert themes[1].quotes == ["Arrived broken, glass smashed."]  # non-verbatim quote replaced by an excerpt
    assert themes[2].kind == "praise" and themes[2].fix is None


def test_review_themes_are_capped(tmp_path):
    many = [ReviewSnippet(text=f"Review number {i}.", evidence_id=f"amazon_product:33333333:{i}") for i in range(12)]
    themes = [{"label": f"C{i}", "kind": "complaint", "review_indices": list(range(i + 1)), "quotes": []} for i in range(7)]
    themes += [{"label": f"P{i}", "kind": "praise", "review_indices": list(range(i + 1)), "quotes": []} for i in range(4)]
    llm, _ = fake_llm(tmp_path, reviews={"themes": themes})
    out = review_themes(llm, many, CAT)
    assert [t.label for t in out] == ["C6", "C5", "C4", "C3", "C2", "P3", "P2", "P1"]
    assert len(out) == MAX_COMPLAINTS + MAX_PRAISE


def test_review_themes_lexicon_fallback():
    extra = ReviewSnippet(text="Beautiful but the glass was smashed.", rating=1, evidence_id="amazon_product:22222222:r2")
    themes = review_themes(None, REVIEWS + [extra], CAT)
    by_label = {t.label: t for t in themes}
    tarnish = by_label["Tarnishing / discolouration"]
    assert tarnish.count == 3 and tarnish.kind == "complaint"
    assert tarnish.fix.startswith("Offer anti-tarnish")
    assert tarnish.quotes and all(len(q) <= 160 for q in tarnish.quotes)
    assert by_label["Broken or dented in transit"].count == 2
    praise = by_label["Looks premium / beautiful"]
    assert praise.count == 2 and praise.fix is None  # the 1-star review is not counted as praise
    kinds = [t.kind for t in themes]
    assert kinds == sorted(kinds, key=lambda k: k != "complaint")
    assert review_themes(None, [], CAT) == []


def test_review_themes_fall_back_when_llm_fails(tmp_path):
    llm, _ = fake_llm(tmp_path, reviews=boom)
    assert review_themes(llm, REVIEWS, CAT) == review_themes(None, REVIEWS, CAT)


# --------------------------------------------------------------------------- tag_buyers


def sig(kind, detail, eid):
    return BuyerSignal(kind=kind, detail=detail, evidence_id=eid)


def buyer(name, **kw):
    return BuyerCandidate(name=name, **kw)


@pytest.mark.parametrize(
    ("cand", "kind"),
    [
        (buyer("Brass Wholesale Ltd", domain="brasswholesale.co.uk"), "wholesaler"),
        (buyer("Acme", domain="acmetrade.co.uk"), "wholesaler"),
        (buyer("Global Imports UK", domain="globalimports.co.uk"), "importer"),
        (buyer("Home Supplies", seen_texts=["Home goods wholesaler · Leeds"], sources=["google_maps"]), "wholesaler"),
        (buyer("Corner Shop", sources=["google_maps"], address="1 High St"), "retailer"),
        (buyer("Lantern Co", domain="lanternco.co.uk", sources=["google"]), "online_brand"),
        (buyer("Known Kind", domain="k.co.uk", kind="importer"), "importer"),
    ],
)
def test_tag_buyers_fallback_kind(cand, kind):
    (out,) = tag_buyers(None, [cand], PRODUCT)
    assert out.kind == kind
    assert out.why


def test_tag_buyers_fallback_why_uses_top_signals():
    c = buyer(
        "Lantern Co",
        domain="lanternco.co.uk",
        signals=[
            sig("phone", "Phone listed", "google:1:0"),
            sig("india_sourcing", "Site says handmade in India", "google:1:1"),
            sig("category_match", "Sells brass lanterns", "google:1:2"),
        ],
    )
    (out,) = tag_buyers(None, [c], PRODUCT)
    assert out.why == "Sells brass lanterns; Site says handmade in India"
    assert c.why is None  # inputs are not mutated


def test_tag_buyers_llm_by_index(tmp_path):
    cands = [buyer("A", domain="a.co.uk"), buyer("B", domain="b.co.uk", sources=["google_maps"]), buyer("C", domain="c.co.uk")]
    long_why = " ".join(f"w{i}" for i in range(30))
    llm, _ = fake_llm(
        tmp_path,
        tag_buyers={
            "buyers": [
                {"index": 0, "kind": "importer", "why": long_why},
                {"index": 1, "kind": "unknown", "why": "Stocks lanterns."},
                {"index": 7, "kind": "retailer", "why": "out of range"},
            ]
        },
    )
    out = tag_buyers(llm, cands, PRODUCT)
    assert [c.name for c in out] == ["A", "B", "C"]
    assert out[0].kind == "importer" and len(out[0].why.split()) == 20
    assert out[1].kind == "retailer" and out[1].why == "Stocks lanterns."  # unknown -> heuristic kind
    assert out[2].kind == "online_brand" and out[2].why  # missing index -> fallback
    assert tag_buyers(llm, [], PRODUCT) == []


# --------------------------------------------------------------------------- brief + pitches

EVIDENCE = [
    Evidence(id="amazon:aaaa1111:0", engine="amazon", fetched_at="2026-09-30T09:00:00+00:00", title="Brass lantern listing"),
    Evidence(id="google_trends:bbbb2222:0", engine="google_trends", fetched_at="2026-09-30T09:00:00+00:00", title="Trends"),
    Evidence(id="amazon_product:cccc3333:r0", engine="amazon_product", fetched_at="2026-09-30T09:00:00+00:00", title="Review"),
    Evidence(id="google:dddd4444:2", engine="google", fetched_at="2026-09-30T09:00:00+00:00", title="Lantern Co stockists"),
]


def make_brief(with_quote=True):
    buyers = [
        BuyerCandidate(
            name="Lantern Co",
            domain="lanternco.co.uk",
            kind="online_brand",
            city="London",
            fit_score=81.0,
            why="Sells brass lanterns",
            signals=[
                sig("category_match", "Sells brass lanterns", "google:dddd4444:2"),
                sig("india_sourcing", "Site says handmade in India", "google:dddd4444:2"),
            ],
        ),
        BuyerCandidate(name="Home Supplies", kind="wholesaler", city="Leeds", fit_score=64.0),
    ]
    themes = [
        ReviewTheme(label="Tarnishing", kind="complaint", count=6, share=0.31, quotes=["went black"],
                    fix="Offer anti-tarnish lacquer coating and include care instructions.", evidence_ids=["amazon_product:cccc3333:r0"]),
        ReviewTheme(label="Broken in transit", kind="complaint", count=4, share=0.22,
                    fix="Use double-wall cartons with moulded pulp inserts.", evidence_ids=["amazon_product:cccc3333:r0"]),
        ReviewTheme(label="Looks premium", kind="praise", count=8, share=0.4),
    ]  # fmt: skip
    quote = QuoteRange(
        currency="GBP", retail_median=42.0, retail_ex_vat=35.0, fob_importer=7.69, fob_retailer=13.83, fx_rate=112.5,
        fob_importer_inr=865.1, fob_retailer_inr=1555.9, unit_cost_inr=800.0, total_cost_inr=960.0,
        margin_retailer=0.38, margin_importer=-0.11, verdict="go",
        assumptions={"vat_rate": 0.2, "retailer_markup": 2.2, "importer_markup": 1.8, "freight_ins_pct": 0.15, "duty_pct": 0.0},
    )  # fmt: skip
    return Brief(
        inputs=RunInputs(description="brass hurricane lantern", unit_cost_inr=800, moq=100, material="brass"),
        market="uk",
        market_label="United Kingdom",
        product=PRODUCT,
        ladder=PriceLadder(currency="GBP", n=37, min=24.0, p25=33.0, p50=42.0, p75=55.0, max=89.0, evidence_ids=["amazon:aaaa1111:0"]),
        quote=quote if with_quote else None,
        demand=DemandCard(term="brass lantern", mean_12m=54.2, yoy_change=0.18, peak_months=[11, 12, 10],
                          top_regions=["London", "South East"], evidence_ids=["google_trends:bbbb2222:0"]),  # fmt: skip
        origin=OriginShare(checked=6, india=2, china=3, unknown=1, evidence_ids=["amazon:aaaa1111:0"]),
        themes=themes,
        buyers=buyers,
        headline="Brass hurricane lantern → United Kingdom: GO (74/100)",
        evidence=EVIDENCE,
    )


def citations(text):
    return re.findall(r"\[ev:([^\]]+)\]", text)


def test_clean_citations():
    valid = {"amazon:aaaa1111:0", "google:dddd4444:2"}
    text = "Median £42 [ev:amazon:aaaa1111:0]. Invented [ev:amazon:ffffffff:9]. Two [ev:amazon:aaaa1111:0, ev:bogus:1:2; google:dddd4444:2]."
    assert clean_citations(text, valid) == (
        "Median £42 [ev:amazon:aaaa1111:0]. Invented. Two [ev:amazon:aaaa1111:0][ev:google:dddd4444:2]."
    )
    assert clean_citations("No cites here.", valid) == "No cites here."


def test_write_brief_fallback_uses_computed_facts():
    brief = make_brief()
    out = write_brief(None, brief)
    assert isinstance(out, BriefText)
    s = out.summary_md
    assert s.startswith("- **Estimated FOB quote range")
    for needle in ["£7.69", "£13.83", "₹865", "₹1,556", "£42.00", "37 listings", "38%", "+18%", "Nov", "2 were made in India", "CETA", "tarnishing (31%)", "Lantern Co (fit 81/100)"]:
        assert needle in s, needle
    assert set(citations(s)) <= {e.id for e in EVIDENCE} and citations(s)
    assert out.spec_improvements == [
        "Offer anti-tarnish lacquer coating and include care instructions.",
        "Use double-wall cartons with moulded pulp inserts.",
    ]


def test_write_brief_fallback_without_quote_or_data():
    brief = Brief(inputs=RunInputs(description="brass bowl"), market="uk", market_label="United Kingdom")
    out = write_brief(None, brief)
    assert "Not enough UK prices" in out.summary_md and out.spec_improvements == []


def test_write_brief_llm_citations_cleaned_and_specs_padded(tmp_path):
    captured = {}

    def respond(kw):
        captured["user"] = kw["messages"][0]["content"]
        return {
            "summary_md": "Estimated quote £7.69–£13.83 [ev:amazon:aaaa1111:0]. Made-up claim [ev:amazon:deadbeef:1].",
            "spec_improvements": ["Add anti-tarnish lacquer [ev:amazon_product:cccc3333:r0]"],
        }

    llm, _ = fake_llm(tmp_path, brief=respond)
    out = write_brief(llm, make_brief())
    assert out.summary_md == "Estimated quote £7.69–£13.83 [ev:amazon:aaaa1111:0]. Made-up claim."
    assert out.spec_improvements[0] == "Add anti-tarnish lacquer"
    assert len(out.spec_improvements) == 3
    facts = json.loads(captured["user"].split("<data>\n", 1)[1].rsplit("\n</data>", 1)[0])
    assert facts["quote_estimate"]["fob_direct_to_retailer"] == 13.83
    assert facts["quote_estimate"]["margin_at_direct_price"] == "38%"
    assert len(facts["top_buyers"]) == 2
    assert set(facts["evidence_titles"]) == {e.id for e in EVIDENCE}


def test_write_brief_falls_back_when_llm_fails(tmp_path):
    llm, _ = fake_llm(tmp_path, brief=boom)
    assert write_brief(llm, make_brief()) == write_brief(None, make_brief())


def test_write_pitches_fallback_template():
    brief = make_brief()
    pitches = write_pitches(None, brief, brief.buyers)
    assert [p.buyer_name for p in pitches] == ["Lantern Co", "Home Supplies"]
    first = pitches[0]
    body = first.body
    assert "your brass hurricane lantern range" in body and "handmade pieces from India" in body
    assert "zero import duty" in body and "CETA" in body
    assert "minimum order is 100 pieces" in body
    assert "around £13.83 FOB per piece" in body and "£7.69" not in body  # importer end loses money
    assert "offer anti-tarnish lacquer coating" in body
    assert body.rstrip().endswith(SIGN_OFF)
    assert len(body.split()) <= 150
    assert "google:dddd4444:2" in first.evidence_ids and "amazon_product:cccc3333:r0" in first.evidence_ids
    assert "while looking for UK homeware stockists" in pitches[1].body


def with_margins(brief, importer, retailer):
    return brief.model_copy(update={"quote": brief.quote.model_copy(update={"margin_importer": importer, "margin_retailer": retailer})})


def test_write_pitches_quote_range_skips_loss_making_ends():
    brief = make_brief()
    (both,) = write_pitches(None, with_margins(brief, 0.05, 0.38), brief.buyers[:1])
    assert "£7.69–£13.83 FOB per piece" in both.body
    (none,) = write_pitches(None, with_margins(brief, -0.4, -0.1), brief.buyers[:1])
    assert "£" not in none.body


def test_write_pitches_without_quote_mentions_no_price():
    brief = make_brief(with_quote=False)
    (p,) = write_pitches(None, brief, brief.buyers[:1])
    assert "£" not in p.body and "₹" not in p.body


def test_write_pitches_llm_guards(tmp_path):
    good = "Dear Lantern Co team,\n\nI loved your lantern range [ev:google:dddd4444:2]. Zero duty under CETA. MOQ 100.\n\nBest,\n[Your name]"
    invented_price = "Dear team, our lanterns cost £25 each. [Your name], [Company], Moradabad"
    below_cost = "Dear team, from £7.69 FOB. [Your name], [Company], Moradabad"
    llm, _ = fake_llm(
        tmp_path,
        pitches={
            "pitches": [
                {"index": 0, "subject": "Brass lanterns for Lantern Co", "body": good},
                {"index": 1, "subject": "Lanterns", "body": invented_price},
                {"index": 2, "subject": "Lanterns", "body": below_cost},
            ]
        },
    )
    brief = make_brief()
    buyers = brief.buyers + [BuyerCandidate(name="Third Shop"), BuyerCandidate(name="Fourth Shop")]
    fallback = write_pitches(None, brief, buyers)
    out = write_pitches(llm, brief, buyers)
    assert out[0].subject == "Brass lanterns for Lantern Co"
    assert "[ev:" not in out[0].body and out[0].body.endswith(SIGN_OFF)
    assert out[0].evidence_ids == fallback[0].evidence_ids
    assert out[1] == fallback[1]  # invented £25 price -> template
    assert out[2] == fallback[2]  # loss-making importer price -> template
    assert out[3] == fallback[3]  # missing from the LLM output -> template


def test_write_pitches_allows_the_quote_range(tmp_path):
    body = "Dear team, indicative FOB £7.69–£13.83 (₹866–₹1,556) per piece.\n\n[Your name], [Company], Moradabad"
    llm, _ = fake_llm(tmp_path, pitches={"pitches": [{"index": 0, "subject": "Hi", "body": body}]})
    brief = with_margins(make_brief(), 0.05, 0.38)
    (p,) = write_pitches(llm, brief, brief.buyers[:1])
    assert p.body == body
