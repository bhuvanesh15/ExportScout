# ExportScout: submission form text

Draft from plan §14. Fill in the links before submitting.

- **Repository:** `https://github.com/bhuvanesh15/ExportScout`
- **Demo video:** `<video-url>`

## Title

ExportScout: find UK buyers, a quote range and a winning pitch from one product photo

## Track

04 · Commerce & Market Intelligence

## What it does / who it helps

Moradabad's brassware exporters ship about 75% of their goods to the US. They lost orders when US tariffs jumped to 50% in 2025, and the rate has kept changing since. Since 15 July 2026 their products enter the UK duty-free. But a 30-worker workshop can't tell what its product sells for there, what it can quote, or which UK buyers to pitch. ExportScout answers all three from one product photo.

**Who it helps:** the owner of a small metal-handicraft export unit in Moradabad (10–150 workers). Their US orders are down, and they have heard "UK is zero duty now" but know no UK buyers. It also helps export consultants and export promotion councils, since one consultant serves many units. The same pipeline carries over to other clusters by changing a config file.

**How it works:** the owner uploads a product photo and enters the unit cost (₹) and MOQ. An agent searches the UK market through SerpApi and returns:
- a FOB quote range with a Go / Tight / No-go margin verdict
- a Market Opportunity Score
- the top UK customer complaints, turned into spec fixes
- ranked UK buyers, where every point links to evidence
- a pitch email per buyer

The agent chooses follow-up searches based on what it finds, within a visible credit budget. Demo Mode replays recorded responses, so anyone can try it without keys.

## How it uses SerpApi

SerpApi is the core of the product: without it there is no product. It uses 12 SerpApi engine APIs (11 search engines plus Google Finance), each supplying a distinct signal:

- **Google Lens:** UK look-alike products and their prices, from a photo
- **Google Shopping, Amazon (amazon.co.uk) and eBay (ebay.co.uk):** the UK retail price ladder
- **Amazon Product and eBay seller location:** country of origin (how many competing products come from India vs China)
- **Amazon Product reviews:** quality gaps, turned into spec fixes and pitch lines
- **Google Trends and Google Autocomplete:** demand, seasonality, the buying window and how UK shoppers phrase the product
- **Google Search and Google Maps:** buyer discovery
- **Google Ads Transparency Center and Google News:** buyer activity (live UK ads, expansion news)
- **Google Finance:** the GBP-INR rate to compare the FOB range with the owner's ₹ cost
- **Account API:** a live "plan searches left" meter

Country-level targeting (`amazon_domain`, `ebay_domain`, `gl`, `country`, `geo`, `ll`) lets an exporter in Moradabad see the UK market as a UK shopper sees it.

Every output number traces back to a stored search result, with its engine and a timestamp. The Evidence tab lists them all. A full UK run is capped at 35 credits (our recorded runs used 24–30). A SQLite cache, a credit ledger and a per-run budget keep usage predictable.

## Existing project?

No, this is a new project.

## AI tools used

- **Claude Code:** research, planning and coding assistance.
- **Claude API (`claude-opus-5-5`)** inside the product, for four tasks only:
  - keywords from Lens titles
  - clustering review complaints and praise
  - tagging buyer type and style
  - writing the brief and pitch emails, where every claim must cite an evidence ID

All prices and scores are computed by deterministic Python.

## Judging criteria

| Criterion | How ExportScout meets it |
|---|---|
| **Idea strength** | A specific, timely, high-stakes problem: a ₹8,500–9,000 crore cluster that depends on the US, the tariff swings since August 2025, and a new zero-duty UK market since 15 July 2026 |
| **Originality** | The first supplier-side, cross-border tool we found among the 185 BuiltWithSerpApi showcase projects and the known hackathon entries. It combines a FOB ceiling worked back from UK retail prices, country-of-origin signals, and "right size" buyer fit for a small workshop |
| **Technical complexity** | 12 SerpApi engine APIs, parsing across countries and currencies, follow-up rules within a credit budget, fixed-rule scoring where every point links to evidence, structured LLM extraction, a cache with record/replay, and Demo Mode |
| **Usefulness** | It ends in actions: a quote range to put in an email, a buyer shortlist with website and phone, and a pitch per buyer. One export order can be worth lakhs |
| **Meaningful SerpApi usage** | Every output number comes from search data. The Evidence tab shows each result, its engine and when it was fetched |

## Links and sources

- Moradabad exports and US tariff impact: https://thefederal.com/category/business/us-tariff-impact-aromatic-brass-handicraft-industries-hit-204380
- India–UK CETA in force: https://www.drishtiias.com/daily-updates/daily-news-analysis/india-uk-ceta-comes-into-effect
- EPCH on CETA and handicrafts: https://tennews.in/epch-welcomes-india-uk-ceta-a-new-zero-duty-gateway-for-indian-handicrafts/
- BuiltWithSerpApi showcase: https://serpapi.github.io/BuiltWithSerpApi/
