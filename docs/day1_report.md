# Day-1 data check

- Run: 2026-09-30T10:30:25+00:00
- Verdict: **GO** (0 of checks 1-7 failed; RETHINK at 3)
- Credits used: 12
- plan_searches_left: 245 before, 234 after

| # | Check | Status | Detail |
|---|---|---|---|
| 1 | Lens | SKIP | not run: no --image given |
| 2 | Amazon "brass hurricane lantern" | PASS | 59/60 priced; bought_last_month on 8 (total 950); 12 sponsored |
| 2 | Amazon "brass planter" | PASS | 57/60 priced; bought_last_month on 2 (total 100); 12 sponsored |
| 3 | Amazon product origin | PASS | origin field on 3/4 (China×3); text signal on 0; 41 reviews parsed |
| 4 | eBay "brass lantern" | PASS | location on 11/60; 8 from India; 60 priced |
| 5 | Trends TIMESERIES | PASS | "brass lantern" mean 0.0, non-zero 0/262; "lantern" mean 7.0, non-zero 262/262 |
| 6 | Maps "home accessories wholesaler" London | PASS | 20/20 with website or phone (17 website, 18 phone) |
| 7 | Ads Transparency grahamandgreen.co.uk | PASS | 400 creatives (Graham And Green Limited); first 2025-11-06, last 2026-09-30; 40 in last 30 days |
| 8 | Finance GBP-INR | PASS | rate 127.437903 |
| 9 | Shopping "brass hurricane lantern" | PASS | 40/40 with merchant + price; 28 merchants; currencies: GBP×40 |

Pass thresholds (plan §11.1): Lens ≥5 priced per photo; Amazon ≥20 priced per search; origin field on ≥2 of 4 products; eBay location present; Trends broader term non-zero; Maps ≥10 with website or phone; Ads creatives with first/last shown; FX 50-300; Shopping ≥10 with merchant + price.
