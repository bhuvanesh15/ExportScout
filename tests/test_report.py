import csv
import io

from exportscout.models import (
    Brief,
    BuyerCandidate,
    BuyerSignal,
    DemandCard,
    Evidence,
    JobPosting,
    MarketRow,
    MarketScore,
    OriginShare,
    Pitch,
    PriceLadder,
    ProductIdentity,
    QuoteRange,
    ReviewTheme,
    RunInputs,
    ScoreComponent,
)
from exportscout.report.brief import CSV_COLUMNS, FOOTER, MARKETS_CAVEAT, brief_markdown, buyers_csv

FETCHED = "2026-09-30T09:00:00+00:00"
UK_ASSUMPTIONS = {"vat_rate": 0.2, "retailer_markup": 2.2, "importer_markup": 1.8, "freight_ins_pct": 0.15, "duty_pct": 0.0}


def make_markets() -> list[MarketRow]:
    def fit(total: float) -> MarketScore:
        return MarketScore(total=total, components={"margin": ScoreComponent(points=total / 2, max_points=40, detail="margin")})

    uk_quote = QuoteRange(currency="GBP", retail_median=38.0, retail_ex_vat=31.67, fob_importer=6.95, fob_retailer=12.52,
                          fx_rate=112.5, fob_importer_inr=781.9, fob_retailer_inr=1408.6, total_cost_inr=960.0,
                          margin_retailer=0.318, verdict="go", assumptions=UK_ASSUMPTIONS)  # fmt: skip
    au_quote = QuoteRange(currency="AUD", retail_median=75.97, retail_ex_vat=69.06, fob_importer=14.78, fob_retailer=26.6,
                          fx_rate=57.8, fob_importer_inr=854.3, fob_retailer_inr=1537.6, total_cost_inr=960.0,
                          margin_retailer=0.376, verdict="go", assumptions={**UK_ASSUMPTIONS, "vat_rate": 0.1})  # fmt: skip
    return [
        MarketRow(code="au", label="Australia", short_label="AU", currency="AUD", keyword="brass hurricane lantern",
                  listings_n=12, quote=au_quote, fx_rate=57.8, hs_code="9405.50", duty_detail="0% under the India–Australia ECTA",
                  duty_note="Needs a certificate of origin", duty_sources=["https://example.com/duty/au"],
                  last_verified="2026-10-01", trends_interest=62, review_depth=2480, score=fit(77.3)),  # fmt: skip
        MarketRow(code="uk", label="United Kingdom", short_label="UK", currency="GBP", keyword="brass hurricane lantern",
                  is_home=True, listings_n=12, quote=uk_quote, fx_rate=112.5, duty_detail="0% under the India–UK CETA",
                  last_verified="2026-09-30", trends_interest=100, review_depth=6145, score=fit(71.0)),  # fmt: skip
        MarketRow(code="us", label="United States", short_label="US", currency="USD", keyword="brass lantern", listings_n=2,
                  duty_pct=0.157, duty_detail="10% Section 301 + 5.7% normal duty", hs_code="9405.50",
                  duty_sources=["https://example.com/duty/us-hts", "https://www.example.com/duty/us-301/"],
                  last_verified="2026-10-01", score=fit(21.6), thin_data=True),  # fmt: skip
    ]


def make_jobs() -> list[JobPosting]:
    return [
        JobPosting(company="Retail | Recruiters", title="Senior Buyer", location="London", posted="1 day ago", posted_days=1,
                   is_recruiter=True, evidence_id="google_jobs:eeee5555:0"),  # fmt: skip
        JobPosting(company="Lantern | Co", title="Lighting Buyer", location="London", posted="3 weeks ago", posted_days=21,
                   link="https://example.com/jobs/2", mentions_overseas=True, evidence_id="google_jobs:eeee5555:2"),  # fmt: skip
        JobPosting(company="Home Supplies, Leeds", title="Buying Assistant", location="Leeds", posted="2 days ago",
                   posted_days=2, link="https://example.com/jobs/1", mentions_india=True, evidence_id="google_jobs:eeee5555:1"),  # fmt: skip
    ]


def make_brief() -> Brief:
    evidence = [
        Evidence(id="amazon:aaaa1111:0", engine="amazon", fetched_at=FETCHED, title="Antique | brass lantern", url="https://www.amazon.co.uk/dp/B0TEST"),
        Evidence(id="google:dddd4444:2", engine="google", fetched_at=FETCHED, title="Lantern Co stockists"),
    ]
    buyers = [
        BuyerCandidate(
            name="Lantern | Co",
            domain="lanternco.co.uk",
            kind="online_brand",
            city="London",
            website="https://lanternco.co.uk",
            phone="+44 20 7946 0000",
            fit_score=81.26,
            why="Sells brass lanterns, made in India",
            signals=[
                BuyerSignal(kind="phone", detail="Phone listed", evidence_id="google:dddd4444:2"),
                BuyerSignal(kind="category_match", detail="Sells brass lanterns", evidence_id="google:dddd4444:2"),
                BuyerSignal(kind="india_sourcing", detail='Site says "handmade in India"', evidence_id="google:dddd4444:2"),
                BuyerSignal(kind="ads_active", detail="9 UK ads", evidence_id="google:dddd4444:2"),
            ],
        ),
        BuyerCandidate(
            name="Home Supplies, Leeds",
            kind="wholesaler",
            signals=[BuyerSignal(kind="hiring", detail="Hiring a Buying Assistant (2 days ago)", evidence_id="google_jobs:eeee5555:1")],
        ),
    ]
    return Brief(
        inputs=RunInputs(description="brass hurricane lantern", unit_cost_inr=800, extra_costs_inr=160, moq=100),
        market="uk",
        market_label="United Kingdom",
        product=ProductIdentity(product_type="brass hurricane lantern", keywords=["brass hurricane lantern", "brass lantern"], broad_term="lantern"),
        ladder=PriceLadder(currency="GBP", n=37, min=24.0, p25=33.0, p50=42.0, p75=55.0, max=89.0),
        quote=QuoteRange(
            currency="GBP", retail_median=42.0, retail_ex_vat=35.0, fob_importer=7.69, fob_retailer=13.83, fx_rate=112.5,
            fob_importer_inr=865.1, fob_retailer_inr=1555.9, unit_cost_inr=800.0, total_cost_inr=960.0,
            margin_retailer=0.383, margin_importer=-0.11, verdict="go",
            assumptions={"vat_rate": 0.2, "retailer_markup": 2.2, "importer_markup": 1.8, "freight_ins_pct": 0.15, "duty_pct": 0.0},
        ),  # fmt: skip
        demand=DemandCard(term="brass lantern", mean_12m=54.2, yoy_change=0.18, peak_months=[11, 12], top_regions=["London"],
                          buying_window_note="Buyers pick Oct–Dec ranges around Apr: pitch now"),  # fmt: skip
        origin=OriginShare(checked=6, india=2, china=3, unknown=1, ebay_total=40, ebay_from_india=3),
        themes=[
            ReviewTheme(label="Tarnishing", kind="complaint", count=6, share=0.31, quotes=["went black"], fix="Offer anti-tarnish lacquer."),
            ReviewTheme(label="Looks premium", kind="praise", count=8, share=0.4),
        ],
        buyers=buyers,
        jobs=make_jobs(),
        market_score=MarketScore(total=74.4, components={"demand": ScoreComponent(points=18.5, max_points=25, detail="mean 54, +18%")}),
        markets=make_markets(),
        headline="Brass hurricane lantern → United Kingdom: GO (74/100)",
        summary_md="Estimated quote £7.69–£13.83 [ev:amazon:aaaa1111:0].",
        spec_improvements=["Offer anti-tarnish lacquer.", "Use double-wall cartons."],
        pitches=[Pitch(buyer_name="Lantern | Co", subject="Brass lanterns", body="Dear team,\n\nHello.\n\n[Your name], [Company], Moradabad")],
        evidence=evidence,
        credits_used=31,
    )


def test_brief_markdown_sections_in_order():
    md = brief_markdown(make_brief())
    headings = [line for line in md.splitlines() if line.startswith("#")]
    assert headings[0] == "# Brass hurricane lantern → United Kingdom: GO (74/100)"
    order = ["## Summary", "## Quote range (estimate)", "## Demand (estimate)", "## Other markets (quick scan)",
             "## Competition and origin", "## What UK customers say", "## Market Opportunity Score", "## Buyers",
             "## Hiring now", "## Pitches", "## Spec improvements", "## Compliance notes", "## Assumptions", "## Evidence"]  # fmt: skip
    positions = [md.index(h) for h in order]
    assert positions == sorted(positions)
    assert md.rstrip().endswith(f"_{FOOTER}_")


def test_brief_markdown_content():
    md = brief_markdown(make_brief())
    assert "| FOB price | £7.69 | £13.83 |" in md
    assert "| FOB in ₹ | ₹865 | ₹1,556 |" in md
    assert "| Your margin | -11% | 38% |" in md
    assert "**Verdict: GO**" in md and "FX 1 GBP = ₹112.50" in md
    assert "- Peak months: Nov, Dec" in md and "- Year on year: +18%" in md
    assert "India 40% of known origins" in md and "3 of 40 listings located in India" in md
    assert "| Tarnishing | 31% | 6 | “went black” | Offer anti-tarnish lacquer. |" in md
    assert "| 1 | Lantern \\| Co | online brand | London | 81 | [lanternco.co.uk](https://lanternco.co.uk) | +44 20 7946 0000 |" in md
    assert "| 2 | Home Supplies, Leeds | wholesaler | — | — | — | — | — |" in md
    assert "### Lantern \\| Co" not in md and "### Lantern | Co" in md
    assert "**Subject:** Brass lanterns" in md and "[Your name], [Company], Moradabad" in md
    assert "1. Offer anti-tarnish lacquer." in md
    assert "CETA zero duty needs a valid proof of origin" in md
    assert "| UK VAT | 20% |" in md and "| Retailer markup | 2.2× |" in md and "| Import duty from India | 0% |" in md
    assert "| Exchange rate (GBP→INR, live) | 112.50 |" in md
    assert "| `amazon:aaaa1111:0` | amazon | Antique \\| brass lantern | [link](https://www.amazon.co.uk/dp/B0TEST) | 2026-09-30T09:00:00+00:00 |" in md
    assert "| `google:dddd4444:2` | google | Lantern Co stockists | — |" in md


def test_brief_markdown_other_markets():
    md = brief_markdown(make_brief())
    assert "**Best other market: Australia** · margin 38% · FOB A$14.78–A$26.60 · duty 0% · Market Fit 77/100" in md
    assert ("| # | Market | Duty | Retail median | FOB range | FOB ceiling (₹) | Margin | Verdict | Market Fit | Listings | Trends "
            "| Amazon reviews | Note |") in md  # fmt: skip
    assert "| 1 | Australia | 0% | A$75.97 | A$14.78–A$26.60 | ₹1,538 | 38% | GO | 77 | 12 | 62 | 2,480 | — |" in md
    assert "| 2 | United Kingdom (deep dive) | 0% | £38.00 | £6.95–£12.52 | ₹1,409 | 32% | GO | 71 | 12 | 100 | 6,145 | — |" in md
    assert "| 3 | United States | 15.7% | — | — | — | — | — | 22 | 2 | — | — | thin data (2 listings); no quote |" in md
    assert (
        "- **United States**: duty 15.7% (10% Section 301 + 5.7% normal duty, HS 9405.50). Sources: "
        "[example.com/duty/us-hts](https://example.com/duty/us-hts), [example.com/duty/us-301](https://www.example.com/duty/us-301/). "
        "Verified 2026-10-01. Amazon search “brass lantern”."
    ) in md
    assert ("- **Australia**: duty 0% (0% under the India–Australia ECTA, HS 9405.50). Needs a certificate of origin. "
            "Sources: [example.com/duty/au](https://example.com/duty/au). Verified 2026-10-01. "
            "Amazon search “brass hurricane lantern”; FX 1 AUD = ₹57.80.") in md  # fmt: skip
    assert f"_{MARKETS_CAVEAT}_" in md and "Amazon-only quick scan" in MARKETS_CAVEAT and "verify duty" in MARKETS_CAVEAT


def test_brief_markdown_hiring_now():
    md = brief_markdown(make_brief())
    section = md[md.index("## Hiring now"):md.index("## Pitches")]
    assert "| Company | Role | Where | Posted | Mentions India | Ad |" in section
    rows = [line for line in section.splitlines() if line.startswith("| ") and "---" not in line][1:]
    assert rows == [
        "| Home Supplies, Leeds | Buying Assistant | Leeds | 2 days ago | yes | [ad](https://example.com/jobs/1) |",
        "| Lantern \\| Co | Lighting Buyer | London | 3 weeks ago | no | [ad](https://example.com/jobs/2) |",
        "| Retail \\| Recruiters (agency) | Senior Buyer | London | 1 day ago | no | — |",
    ]


def test_brief_markdown_is_deterministic():
    first, second = make_brief(), make_brief()
    second.created_at = first.created_at
    assert brief_markdown(first) == brief_markdown(second)


def test_brief_markdown_handles_empty_brief():
    brief = Brief(inputs=RunInputs(description="brass bowl", category="no_such_category"), market="uk", market_label="United Kingdom")
    md = brief_markdown(brief)
    assert md.startswith("# ExportScout brief → United Kingdom")
    assert "Not enough UK prices" in md and "No demand data." in md and "No buyers found." in md
    assert "## Evidence" not in md and "## Compliance notes" not in md
    assert "## Other markets" not in md and "## Hiring now" not in md
    assert md.rstrip().endswith(f"_{FOOTER}_")


def test_buyers_csv():
    text = buyers_csv(make_brief())
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0]) == CSV_COLUMNS
    first, second = rows
    assert first["rank"] == "1" and first["name"] == "Lantern | Co" and first["kind"] == "online_brand"
    assert first["fit_score"] == "81.3" and first["domain"] == "lanternco.co.uk"
    assert first["top_signals"] == 'Sells brass lanterns; Site says "handmade in India"; 9 UK ads'
    assert first["hiring"] == "" and first["why"] == "Sells brass lanterns, made in India"
    hiring = "Hiring a Buying Assistant (2 days ago)"
    assert second == {"rank": "2", "name": "Home Supplies, Leeds", "kind": "wholesaler", "city": "", "fit_score": "",
                      "website": "", "phone": "", "domain": "", "top_signals": hiring, "hiring": hiring, "why": ""}  # fmt: skip


def test_buyers_csv_empty():
    brief = Brief(inputs=RunInputs(), market="uk", market_label="United Kingdom")
    assert buyers_csv(brief) == ",".join(CSV_COLUMNS) + "\n"
