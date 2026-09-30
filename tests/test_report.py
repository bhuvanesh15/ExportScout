import csv
import io

from exportscout.models import (
    Brief,
    BuyerCandidate,
    BuyerSignal,
    DemandCard,
    Evidence,
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
from exportscout.report.brief import CSV_COLUMNS, FOOTER, brief_markdown, buyers_csv

FETCHED = "2026-09-30T09:00:00+00:00"


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
        BuyerCandidate(name="Home Supplies, Leeds", kind="wholesaler"),
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
        market_score=MarketScore(total=74.4, components={"demand": ScoreComponent(points=18.5, max_points=25, detail="mean 54, +18%")}),
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
    order = ["## Summary", "## Quote range (estimate)", "## Demand (estimate)", "## Competition and origin", "## What UK customers say",
             "## Market Opportunity Score", "## Buyers", "## Pitches", "## Spec improvements", "## Compliance notes", "## Assumptions", "## Evidence"]  # fmt: skip
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


def test_brief_markdown_handles_empty_brief():
    brief = Brief(inputs=RunInputs(description="brass bowl", category="no_such_category"), market="uk", market_label="United Kingdom")
    md = brief_markdown(brief)
    assert md.startswith("# ExportScout brief → United Kingdom")
    assert "Not enough UK prices" in md and "No demand data." in md and "No buyers found." in md
    assert "## Evidence" not in md and "## Compliance notes" not in md
    assert md.rstrip().endswith(f"_{FOOTER}_")


def test_buyers_csv():
    text = buyers_csv(make_brief())
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0]) == CSV_COLUMNS
    first, second = rows
    assert first["rank"] == "1" and first["name"] == "Lantern | Co" and first["kind"] == "online_brand"
    assert first["fit_score"] == "81.3" and first["domain"] == "lanternco.co.uk"
    assert first["top_signals"] == 'Sells brass lanterns; Site says "handmade in India"; 9 UK ads'
    assert first["why"] == "Sells brass lanterns, made in India"
    assert second == {"rank": "2", "name": "Home Supplies, Leeds", "kind": "wholesaler", "city": "", "fit_score": "",
                      "website": "", "phone": "", "domain": "", "top_signals": "", "why": ""}  # fmt: skip


def test_buyers_csv_empty():
    brief = Brief(inputs=RunInputs(), market="uk", market_label="United Kingdom")
    assert buyers_csv(brief) == ",".join(CSV_COLUMNS) + "\n"
