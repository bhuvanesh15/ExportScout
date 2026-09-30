"""Record Demo Mode data: run the full agent live for each demo product and save every
SerpApi and LLM response to demo_cache/ (API keys removed), then replay it offline to
prove Demo Mode works with no keys.

    python scripts/record_demo.py                   # dry run: what would be recorded
    python scripts/record_demo.py --live            # up to the run budget (45) per product; recorded searches are free
    python scripts/record_demo.py --live --only lantern
    python scripts/record_demo.py --verify          # replay only, no network
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from exportscout.agent.orchestrator import (  # noqa: E402
    DEFAULT_BUDGET,
    DEMO_DIR,
    REPO_ROOT,
    load_demo_products,
    make_clients,
    run_scout,
)
from exportscout.models import StepEvent  # noqa: E402

RESERVE = 20  # searches always left untouched for live testing


def _print_event(e: StepEvent) -> None:
    mark = {"running": "…", "done": "✓", "skipped": "–", "warning": "!"}[e.status]
    print(f"  {mark} [{e.step}] {e.name:<8} {e.message}  ({e.credits_used} cr)")


def record(product: dict, budget: int) -> None:
    serp, llm = make_clients(demo_mode=False, record=True, budget=budget)
    before = serp.account() or {}
    print(f"\n● {product['label']}: recording (searches left: {before.get('plan_searches_left', '?')})")
    brief = run_scout(product["inputs"], serp=serp, llm=llm, on_event=_print_event)
    out = DEMO_DIR / "briefs" / f"{product['id']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(brief.model_dump_json(indent=1), encoding="utf-8")
    after = serp.account() or {}
    print(f"  → {brief.headline}")
    print(f"  → {len(brief.buyers)} buyers, {len(brief.evidence)} evidence items, {serp.credits_used} credits "
          f"(searches left: {after.get('plan_searches_left', '?')}); saved {out.relative_to(REPO_ROOT)}")
    serp.close()


def verify(product: dict) -> bool:
    """Replay with keys hidden and the network unused; fail on any warning caused by a cache miss."""
    saved = {k: os.environ.pop(k) for k in ("SERPAPI_API_KEY", "SERPAPI_KEY", "ANTHROPIC_API_KEY") if k in os.environ}
    try:
        serp, llm = make_clients(demo_mode=True)
        brief = run_scout(product["inputs"], serp=serp, llm=llm)
        misses = [w for w in brief.warnings if "no recorded response" in w]
        ok = serp.credits_used == 0 and not misses and not llm.replay_misses and bool(brief.buyers) and bool(brief.markets)
        print(f"  replay {product['id']}: {'OK' if ok else 'FAILED'} · {brief.headline} · "
              f"{len(brief.buyers)} buyers · {len(brief.markets)} markets · {len(brief.jobs)} job ads · "
              f"{serp.credits_used} credits · {len(misses)} search misses · {llm.replay_misses} LLM misses")
        for w in misses:
            print(f"    miss: {w}")
        serp.close()
        return ok
    finally:
        os.environ.update(saved)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="spend credits and record")
    parser.add_argument("--verify", action="store_true", help="only replay the recordings")
    parser.add_argument("--only", help="demo product id")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    args = parser.parse_args()
    load_dotenv(REPO_ROOT / ".env")

    products = [p for p in load_demo_products() if not args.only or p["id"] == args.only]
    if not products:
        print("No demo products found in demo_cache/demo_products.yaml")
        return 1
    missing = [p["id"] for p in products if p["inputs"].image_path and not Path(p["inputs"].image_path).exists()]
    if missing:
        print(f"Missing demo images for: {', '.join(missing)} (put them in demo_cache/images/)")
        return 1

    if not args.live and not args.verify:
        for p in products:
            print(f"would record {p['id']}: {p['label']} (≤{args.budget} credits)")
        print(f"estimated total ≤{args.budget * len(products)} credits; run with --live to record")
        return 0

    if args.live:
        serp, _ = make_clients(demo_mode=False, budget=0)
        left = (serp.account() or {}).get("plan_searches_left", 0)
        serp.close()
        worst = args.budget * len(products)
        if left < worst + RESERVE:
            print(f"Only {left} searches left; recording could use up to {worst}. "
                  f"Keeping a reserve of {RESERVE}, so not recording. Use --budget or --only to spend less.")
            return 1
        for p in products:
            record(p, args.budget)
    print("\nVerifying Demo Mode replay (no keys, no network):")
    results = [verify(p) for p in products]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
