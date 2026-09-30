"""Day-1 data check (plan §11.1): do the SerpApi fields ExportScout relies on exist for UK brassware?

Without ``--live`` it only prints the planned requests and the credit estimate (no key needed).
With ``--live`` it spends about 14 credits, records fixtures to demo_cache/serp/ and writes
docs/day1_report.md with a GO / RETHINK verdict.

    python scripts/probe.py --image demo_cache/images/lantern.jpg --image https://example.com/planter.jpg
    python scripts/probe.py --image demo_cache/images/lantern.jpg --image demo_cache/images/planter.jpg --live
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from exportscout.config import market as load_market  # noqa: E402
from exportscout.models import AdsActivity, Listing, Place, ProductDetail, TrendsSeries  # noqa: E402

REPORT_PATH = REPO_ROOT / "docs" / "day1_report.md"
BUDGET = 16
MIN_SEARCHES_LEFT = 20
MAX_IMAGES = 2
AMAZON_QUERIES = ("brass hurricane lantern", "brass planter")
ASINS_PER_SEARCH = 2
EBAY_QUERY = "brass lantern"
TRENDS_TERMS = ("brass lantern", "lantern")  # the last one is the broader term
MAPS_QUERY = "home accessories wholesaler"
MAPS_CITY = "London"
SHOPPING_QUERY = "brass hurricane lantern"
DEFAULT_ADS_DOMAIN = "grahamandgreen.co.uk"
CORE_CHECKS = range(1, 8)  # checks 1-7 decide the verdict
RETHINK_AT = 3  # this many failed core checks -> RETHINK

Status = Literal["pass", "fail", "error", "skip"]


@dataclass(frozen=True)
class PlannedRequest:
    check: int
    engine: str
    request: str
    credits: int = 1


@dataclass(frozen=True)
class CheckResult:
    number: int
    name: str
    status: Status
    detail: str


# --------------------------------------------------------------------------- plan


def is_url(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def plan_requests(images: list[str], ads_domain: str, market: dict[str, Any]) -> list[PlannedRequest]:
    """Every SerpApi search the live check makes, in order (one credit each unless cached)."""
    lens, g = market["lens"], market["google"]
    out = [
        PlannedRequest(1, "google_lens", f"{'url' if is_url(img) else 'image'}={img} country={lens['country']} hl={lens['hl']}")
        for img in images[:MAX_IMAGES]
    ]
    out += [PlannedRequest(2, "amazon", f'k="{q}" amazon_domain={market["amazon_domain"]}') for q in AMAZON_QUERIES]
    out += [
        PlannedRequest(3, "amazon_product", f"asin=<top {ASINS_PER_SEARCH} non-sponsored from '{q}'>", ASINS_PER_SEARCH)
        for q in AMAZON_QUERIES
    ]
    london = next((c for c in market.get("maps_cities", []) if c["name"] == MAPS_CITY), {"ll": "?"})
    out += [
        PlannedRequest(4, "ebay", f'_nkw="{EBAY_QUERY}" ebay_domain={market["ebay_domain"]}'),
        PlannedRequest(5, "google_trends", f'q="{",".join(TRENDS_TERMS)}" geo={market["trends_geo"]} data_type=TIMESERIES'),
        PlannedRequest(6, "google_maps", f'q="{MAPS_QUERY}" ll={london["ll"]} ({MAPS_CITY})'),
        PlannedRequest(7, "google_ads_transparency_center", f"text={ads_domain} region={market['ads_region']}"),
        PlannedRequest(8, "google_finance", f"q={market.get('fx_pair', 'GBP-INR')}"),
        PlannedRequest(9, "google_shopping", f'q="{SHOPPING_QUERY}" gl={g["gl"]} location="{g["location"]}"'),
    ]
    return out


def estimated_credits(plan: list[PlannedRequest]) -> int:
    return sum(r.credits for r in plan)


# --------------------------------------------------------------------------- metrics (pure)


def _status(ok: bool) -> Status:
    return "pass" if ok else "fail"


def _currencies(listings: list[Listing]) -> str:
    counts = Counter(l.currency or "?" for l in listings if l.price is not None)
    return ", ".join(f"{c}×{n}" for c, n in counts.most_common()) or "none"


def lens_check(listings: list[Listing], label: str) -> CheckResult:
    priced = [l for l in listings if l.price is not None]
    detail = f"{len(priced)}/{len(listings)} priced look-alikes; currencies: {_currencies(listings)}"
    return CheckResult(1, f"Lens {label}", _status(len(priced) >= 5), detail)


def amazon_check(query: str, listings: list[Listing]) -> CheckResult:
    priced = [l for l in listings if l.price is not None]
    bought = [l for l in listings if l.bought_last_month is not None]
    sponsored = sum(l.sponsored for l in listings)
    detail = (
        f"{len(priced)}/{len(listings)} priced; bought_last_month on {len(bought)}"
        f" (total {sum(l.bought_last_month or 0 for l in bought):,}); {sponsored} sponsored"
    )
    return CheckResult(2, f'Amazon "{query}"', _status(len(priced) >= 20), detail)


def top_asins(listings: list[Listing], n: int = ASINS_PER_SEARCH) -> list[str]:
    """The first ``n`` non-sponsored ASINs, in result order."""
    out: list[str] = []
    for l in listings:
        if l.asin and not l.sponsored and l.asin not in out:
            out.append(l.asin)
        if len(out) >= n:
            break
    return out


def origin_check(products: list[ProductDetail]) -> CheckResult:
    with_field = [p for p in products if p.origin]
    with_text = [p for p in products if p.origin_text_signal]
    origins = Counter(p.origin for p in with_field)
    reviews = sum(len(p.reviews) for p in products)
    detail = (
        f"origin field on {len(with_field)}/{len(products)}"
        f" ({', '.join(f'{o}×{n}' for o, n in origins.most_common()) or 'none'});"
        f" text signal on {len(with_text)}; {reviews} reviews parsed"
    )
    return CheckResult(3, "Amazon product origin", _status(len(with_field) >= 2), detail)


def ebay_check(listings: list[Listing]) -> CheckResult:
    located = [l for l in listings if l.location]
    india = [l for l in located if "india" in l.location.lower()]
    priced = sum(l.price is not None for l in listings)
    detail = f"location on {len(located)}/{len(listings)}; {len(india)} from India; {priced} priced"
    return CheckResult(4, f'eBay "{EBAY_QUERY}"', _status(bool(located)), detail)


def trends_check(series: TrendsSeries | None, broad: str = TRENDS_TERMS[-1]) -> CheckResult:
    if series is None:
        return CheckResult(5, "Trends TIMESERIES", "fail", "no timeline returned")
    parts = []
    for term in series.terms:
        vals = series.values.get(term, [])
        mean = sum(vals) / len(vals) if vals else 0
        parts.append(f'"{term}" mean {mean:.1f}, non-zero {sum(v > 0 for v in vals)}/{len(vals)}')
    ok = any(v > 0 for v in series.values.get(broad, []))
    return CheckResult(5, "Trends TIMESERIES", _status(ok), "; ".join(parts))


def maps_check(places: list[Place]) -> CheckResult:
    reachable = [p for p in places if p.website or p.phone]
    detail = (
        f"{len(reachable)}/{len(places)} with website or phone"
        f" ({sum(bool(p.website) for p in places)} website, {sum(bool(p.phone) for p in places)} phone)"
    )
    return CheckResult(6, f'Maps "{MAPS_QUERY}" {MAPS_CITY}', _status(len(reachable) >= 10), detail)


def ads_check(ads: AdsActivity | None, domain: str) -> CheckResult:
    if ads is None:
        return CheckResult(7, f"Ads Transparency {domain}", "fail", "no ad creatives")
    ok = ads.total_creatives > 0 and bool(ads.first_shown) and bool(ads.last_shown)
    detail = (
        f"{ads.total_creatives} creatives ({ads.advertiser or '?'}); first {ads.first_shown or '?'},"
        f" last {ads.last_shown or '?'}; {ads.active_last_30d} in last 30 days"
    )
    return CheckResult(7, f"Ads Transparency {domain}", _status(ok), detail)


def fx_check(rate: float | None, pair: str) -> CheckResult:
    ok = rate is not None and 50 < rate < 300
    return CheckResult(8, f"Finance {pair}", _status(ok), f"rate {rate}" if rate is not None else "no rate")


def shopping_check(listings: list[Listing]) -> CheckResult:
    good = [l for l in listings if l.merchant and l.price is not None]
    merchants = len({l.merchant for l in good})
    detail = f"{len(good)}/{len(listings)} with merchant + price; {merchants} merchants; currencies: {_currencies(listings)}"
    return CheckResult(9, f'Shopping "{SHOPPING_QUERY}"', _status(len(good) >= 10), detail)


def failed_core_checks(results: list[CheckResult]) -> list[int]:
    """Core checks (1-7) with any failed or errored part. Skipped / not-run checks don't count."""
    return sorted({r.number for r in results if r.number in CORE_CHECKS and r.status in ("fail", "error")})


def verdict(results: list[CheckResult]) -> str:
    return "RETHINK" if len(failed_core_checks(results)) >= RETHINK_AT else "GO"


# --------------------------------------------------------------------------- output


def format_table(results: list[CheckResult]) -> str:
    rows = [("#", "check", "status", "detail")] + [(str(r.number), r.name, r.status.upper(), r.detail) for r in results]
    w0, w1, w2 = (max(len(row[i]) for row in rows) for i in range(3))
    lines = [f"{a:>{w0}}  {b:<{w1}}  {c:<{w2}}  {d}" for a, b, c, d in rows]
    lines.insert(1, "-" * min(max(len(line) for line in lines), 120))
    return "\n".join(lines)


def render_report(
    results: list[CheckResult],
    *,
    started: str,
    credits_used: int,
    searches_before: Any,
    searches_after: Any,
    note: str | None = None,
) -> str:
    failed = failed_core_checks(results)
    lines = [
        "# Day-1 data check",
        "",
        f"- Run: {started}",
        f"- Verdict: **{verdict(results)}** ({len(failed)} of checks 1-7 failed"
        + (f": {', '.join(map(str, failed))}" if failed else "")
        + f"; RETHINK at {RETHINK_AT})",
        f"- Credits used: {credits_used}",
        f"- plan_searches_left: {searches_before} before, {searches_after} after",
    ]
    if note:
        lines.append(f"- Note: {note}")
    lines += ["", "| # | Check | Status | Detail |", "|---|---|---|---|"]
    for r in results:
        detail = r.detail.replace("|", "\\|")
        lines.append(f"| {r.number} | {r.name} | {r.status.upper()} | {detail} |")
    lines += [
        "",
        "Pass thresholds (plan §11.1): Lens ≥5 priced per photo; Amazon ≥20 priced per search; origin field on ≥2"
        " of 4 products; eBay location present; Trends broader term non-zero; Maps ≥10 with website or phone;"
        " Ads creatives with first/last shown; FX 50-300; Shopping ≥10 with merchant + price.",
        "",
    ]
    return "\n".join(lines)


def print_plan(plan: list[PlannedRequest], images: list[str]) -> None:
    print("ExportScout Day-1 data check: dry run (no searches made)\n")
    print(f"{'check':>5}  {'engine':<31}  {'credits':>7}  request")
    for r in plan:
        print(f"{r.check:>5}  {r.engine:<31}  {r.credits:>7}  {r.request}")
    if not images:
        print("\nLens is skipped: pass --image PATH_OR_URL (up to 2).")
    elif len(images) > MAX_IMAGES:
        print(f"\nOnly the first {MAX_IMAGES} images are used.")
    print(f"\nEstimated credits: {estimated_credits(plan)} (budget {BUDGET}; cached responses are free).")
    print("Run again with --live to spend them.")


# --------------------------------------------------------------------------- live run


def make_live_client() -> Any:
    """Record-mode client: loads .env, writes fixtures to demo_cache/serp/, caps spend at BUDGET."""
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    from exportscout.agent.orchestrator import CACHE_DIR, DEMO_DIR
    from exportscout.serp.client import SerpClient

    return SerpClient(mode="record", fixture_dir=DEMO_DIR / "serp", cache_path=CACHE_DIR / "serp.sqlite", budget=BUDGET)


def run_live(images: list[str], ads_domain: str, *, client: Any = None, report_path: Path = REPORT_PATH) -> int:
    """Run checks 1-9, print the table and write the report. ``client`` defaults to make_live_client()."""
    from exportscout.models import EvidenceStore
    from exportscout.serp import engines as E
    from exportscout.serp.client import BudgetExceeded

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    client = client if client is not None else make_live_client()
    account = client.account() or {}
    before = account.get("plan_searches_left")
    print(f"plan_searches_left: {before}")
    if not isinstance(before, int) or before < MIN_SEARCHES_LEFT:
        print(f"Aborting: need at least {MIN_SEARCHES_LEFT} searches left.")
        client.close()
        return 2

    mk = load_market("uk")
    ev = EvidenceStore()
    amazon: dict[str, list[Listing]] = {}

    def lens(img: str) -> list[CheckResult]:
        found = E.lens_matches(client, ev, mk, url=img) if is_url(img) else E.lens_matches(client, ev, mk, image=img)
        return [lens_check(found, Path(img).name if not is_url(img) else img[:60])]

    def amazon_searches() -> list[CheckResult]:
        out = []
        for q in AMAZON_QUERIES:
            amazon[q] = E.amazon_listings(client, ev, mk, q)
            out.append(amazon_check(q, amazon[q]))
        return out

    def origin() -> list[CheckResult]:
        asins = [a for q in AMAZON_QUERIES for a in top_asins(amazon.get(q, []))]
        if not asins:
            return [CheckResult(3, "Amazon product origin", "skip", "no ASINs from check 2")]
        return [origin_check([E.amazon_product(client, ev, mk, a) for a in asins])]

    pair = mk.get("fx_pair", "GBP-INR")
    london = next(c for c in mk["maps_cities"] if c["name"] == MAPS_CITY)
    steps: list[tuple[int, str, Callable[[], list[CheckResult]]]] = []
    if images:
        steps += [(1, f"Lens {img}", lambda img=img: lens(img)) for img in images[:MAX_IMAGES]]
    steps += [
        (2, "Amazon search", amazon_searches),
        (3, "Amazon product origin", origin),
        (4, f'eBay "{EBAY_QUERY}"', lambda: [ebay_check(E.ebay_listings(client, ev, mk, EBAY_QUERY))]),
        (5, "Trends TIMESERIES", lambda: [trends_check(E.trends_timeseries(client, ev, mk, list(TRENDS_TERMS)))]),
        (
            6,
            f'Maps "{MAPS_QUERY}" {MAPS_CITY}',
            lambda: [maps_check(E.maps_places(client, ev, mk, MAPS_QUERY, ll=london["ll"], city=MAPS_CITY))],
        ),
        (7, f"Ads Transparency {ads_domain}", lambda: [ads_check(E.ads_activity(client, ev, mk, ads_domain), ads_domain)]),
        (8, f"Finance {pair}", lambda: [fx_check(E.fx_rate(client, ev, pair), pair)]),
        (9, f'Shopping "{SHOPPING_QUERY}"', lambda: [shopping_check(E.shopping_listings(client, ev, mk, SHOPPING_QUERY))]),
    ]

    results: list[CheckResult] = []
    if not images:
        results.append(CheckResult(1, "Lens", "skip", "not run: no --image given"))
    note = None
    for i, (number, name, run) in enumerate(steps):
        try:
            results += run()
        except BudgetExceeded as exc:
            note = f"stopped early: {exc}"
            results += [CheckResult(n, nm, "skip", "not run: credit budget reached") for n, nm, _ in steps[i:]]
            break
        except Exception as exc:  # one broken engine must not hide the others
            results.append(CheckResult(number, name, "error", f"{type(exc).__name__}: {exc}"[:300]))
        print(f"  done: {name} (credits so far: {client.credits_used})")

    try:
        after = (client.account() or {}).get("plan_searches_left")
    except Exception as exc:
        after = f"unknown ({type(exc).__name__})"
    client.close()

    print()
    print(format_table(results))
    print(f"\nVerdict: {verdict(results)}  ·  credits used: {client.credits_used}  ·  searches left: {before} -> {after}")
    if note:
        print(f"Note: {note}")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(
            results,
            started=started,
            credits_used=client.credits_used,
            searches_before=before,
            searches_after=after,
            note=note,
        ),
        encoding="utf-8",
    )
    print(f"Report written to {report_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="ExportScout Day-1 SerpApi data check (plan §11.1).")
    parser.add_argument("--image", action="append", default=[], metavar="PATH_OR_URL", help="product photo for Lens (up to 2)")
    parser.add_argument("--live", action="store_true", help=f"spend real SerpApi credits (about 14; budget {BUDGET})")
    parser.add_argument("--ads-domain", default=DEFAULT_ADS_DOMAIN, help="UK homeware brand domain for Ads Transparency")
    args = parser.parse_args(argv)

    images = list(args.image)
    missing = [img for img in images if not is_url(img) and not Path(img).is_file()]
    if missing:
        parser.error(f"image not found: {', '.join(missing)}")
    if not args.live:
        print_plan(plan_requests(images, args.ads_domain, load_market("uk")), images)
        return 0
    return run_live(images, args.ads_domain)


if __name__ == "__main__":
    sys.exit(main())
