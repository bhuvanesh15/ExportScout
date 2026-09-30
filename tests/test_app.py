"""Smoke tests for the Streamlit app, rendered headlessly from the fictional sample brief."""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import app as app_module
from exportscout.agent import orchestrator
from exportscout.models import Brief, Evidence, StepEvent
from exportscout.serp.client import CacheMiss, SerpApiError

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests" / "fixtures" / "sample_brief.json"


@pytest.fixture
def sample() -> Brief:
    return Brief.model_validate_json(SAMPLE.read_text(encoding="utf-8"))


@pytest.fixture
def at(monkeypatch) -> AppTest:
    monkeypatch.setenv("EXPORTSCOUT_SAMPLE_BRIEF", str(SAMPLE))
    monkeypatch.setenv("SERPAPI_API_KEY", "")
    monkeypatch.setenv("SERPAPI_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.chdir(ROOT)
    test = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    test.run()
    return test


def _markdown(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


def test_sample_brief_renders_all_tabs(at: AppTest, sample: Brief):
    assert not at.exception
    assert [t.label for t in at.tabs] == app_module.TABS
    text = _markdown(at)
    assert "Brass hurricane lantern" in text
    assert "](https://example.com/sample/" in text  # summary citations became links
    assert "[ev:" not in text
    assert any("Opportunity score" in m.label for m in at.metric)
    assert any(app_module.DISCLAIMER in c.value for c in at.caption)
    assert at.text_area(key="pitch_body_0").value == sample.pitches[0].body


def test_markets_tab_renders_table_chart_and_details(at: AppTest, sample: Brief):
    assert not at.exception
    tab = at.tabs[app_module.TABS.index("Markets")]
    table = tab.dataframe[0].value
    assert list(table["market"]) == [app_module.market_name(m) for m in sample.markets]
    assert "United Kingdom (deep dive)" in set(table["market"])
    assert "Thin data" in " ".join(table["note"])
    us = table[table["market"] == "United States"].iloc[0]
    assert us["duty"] == 15.7 and us["verdict"] == "TIGHT" and us["retail"].startswith("$")
    assert table[table["market"] == "Australia"].iloc[0]["fob"].startswith("A$")
    charts = tab.get("vega_lite_chart")
    assert len(charts) == 1 and "Australia" in charts[0].proto.spec
    assert [e.label.split(". ", 1)[1].split(" · ")[0] for e in tab.expander] == list(table["market"])
    text = "\n".join(m.value for m in tab.markdown)
    assert "**Best other market: Australia**" in text
    assert "](https://example.com/sample/duty/us)" in text  # duty source links
    assert "A\\$" in text  # dollar amounts are escaped, not LaTeX
    best = next(m for m in at.metric if m.label == "Best other market")
    assert best.value == "Australia · 54% margin"


def test_hiring_signals_show_in_brief_and_buyers(at: AppTest, sample: Brief):
    assert not at.exception
    hiring = next(m for m in at.metric if m.label == "Companies hiring buyers")
    assert hiring.value == "3"  # the recruitment agency's ad doesn't count
    tab = at.tabs[app_module.TABS.index("Buyers")]
    buyers = tab.dataframe[0].value
    assert buyers["hiring"].tolist()[0] == "Lighting Buyer (5 days ago)"
    assert set(buyers["hiring"].tolist()[1:]) == {""}
    assert any(m.value == "**UK companies hiring buyers now**" for m in tab.markdown)
    jobs = tab.dataframe[1].value
    assert list(jobs["company"]) == ["Example Home Group", "Sample Lantern House", "Placeholder Living",
                                     "Imaginary Recruitment Partners"]  # fmt: skip
    assert list(jobs.iloc[0]["flags"]) == ["mentions India", "overseas sourcing"]
    assert list(jobs.iloc[-1]["flags"]) == ["via agency"]
    assert app_module.ENGINE_LABELS["google_jobs"] == "Google Jobs"


def test_market_helpers_handle_missing_data(sample: Brief):
    brief = sample.model_copy(update={"markets": [m.model_copy(update={"quote": None, "ladder": None})
                                                  for m in sample.markets]})
    assert app_module.best_other_market(brief) is None
    table = app_module.market_table(brief.markets)
    assert set(table["fob"]) == {"n/a"} and set(table["verdict"]) == {"n/a"}
    assert app_module.best_other_market(sample.model_copy(update={"markets": []})) is None
    assert app_module.rate(0.157) == "15.7%" and app_module.rate(0.2) == "20%" and app_module.rate(0.0) == "0%"
    assert app_module.money(12.5, "AUD") == "A$12.50" and app_module.money(12.5, "AED") == "AED 12.50"


def test_demo_mode_is_forced_without_a_key(at: AppTest):
    toggle = at.toggle[0]
    assert toggle.value is True
    assert toggle.disabled


def test_switching_pitch_and_filtering_evidence(at: AppTest, sample: Brief):
    at.selectbox(key="pitch_buyer").set_value(1).run()
    assert not at.exception
    assert at.text_area(key="pitch_body_1").value == sample.pitches[1].body
    at.multiselect(key="evidence_engines").set_value(["google_maps"]).run()
    assert not at.exception


def test_render_citations_numbers_by_first_use():
    ev = [
        Evidence(id="amazon:abcd1234:0", engine="amazon", fetched_at="2026-09-30T00:00:00+00:00", title="A",
                 url="https://example.com/a"),
        Evidence(id="google:ffff0000:2", engine="google", fetched_at="2026-09-30T00:00:00+00:00", title="B",
                 url="https://example.com/b (x)"),
        Evidence(id="ebay:00000000:1", engine="ebay", fetched_at="2026-09-30T00:00:00+00:00", title="C"),
    ]
    md = "Median £42 [ev:google:ffff0000:2]. Origin [ev:amazon:abcd1234:0, ev:google:ffff0000:2] [ev:nope] [ev:ebay:00000000:1]"
    out = app_module.render_citations(md, ev)
    assert out == (
        "Median £42 [[1]](https://example.com/b%20%28x%29). "
        "Origin [[2]](https://example.com/a) [[1]](https://example.com/b%20%28x%29)  [3]"
    )
    assert [e.title for e in app_module.cited_evidence(md, ev)] == ["B", "A", "C"]


def test_friendly_errors_hide_keys(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "secretkey123")
    assert app_module.friendly_error(CacheMiss("amazon", {"k": "x"})) == app_module.DEMO_MISS
    msg = app_module.friendly_error(SerpApiError("bad request api_key=secretkey123"))
    assert "secretkey123" not in msg
    msg = app_module.friendly_error(RuntimeError("GET https://serpapi.com/search?api_key=secretkey123&q=x"))
    assert "secretkey123" not in msg and "RuntimeError" in msg


def test_small_helpers():
    assert app_module.upload_path("Photo.JPEG", b"abc").name.endswith(".jpg")
    assert app_module.upload_path("p.webp", b"abc").parent.name == "uploads"
    line = app_module.step_line(StepEvent(step=3, name="Prices", message="Median £42", credits_used=9))
    assert "3. Prices" in line and "9 credits" in line
    assert app_module.export_or_none("no_such_export", None) is None
    assert app_module.month_span([12, 11, 10]) == "Oct–Dec"
    assert app_module.month_span([1, 12, 11]) == "Nov–Jan"
    assert app_module.month_span([3, 7]) == "Mar, Jul"


class FakeSerp:
    credits_used = 0
    cache_hits = 0

    def close(self):
        pass


def _run_app(monkeypatch, scout, *, compare_markets: bool = True) -> AppTest:
    monkeypatch.delenv("EXPORTSCOUT_SAMPLE_BRIEF", raising=False)
    monkeypatch.setenv("SERPAPI_API_KEY", "")
    monkeypatch.setenv("SERPAPI_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(orchestrator, "make_clients", lambda **kw: (FakeSerp(), None))
    monkeypatch.setattr(orchestrator, "run_scout", scout)
    test = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    test.run()
    assert not test.tabs
    if not compare_markets:
        test.checkbox(key="compare_markets").uncheck().run()
    next(b for b in test.button if b.label == "Scout the UK market").click().run()
    return test


def test_scout_button_runs_the_agent_and_shows_the_brief(monkeypatch, sample: Brief):
    seen = {}

    def scout(inputs, *, serp, llm, on_event=None, compare_markets=True):
        seen["inputs"] = inputs
        seen["compare_markets"] = compare_markets
        serp.cache_hits = 2
        on_event(StepEvent(step=1, name="Identify", message="Found 14 UK look-alikes", credits_used=0))
        on_event(StepEvent(step=9, name="Write", message="Brief ready", credits_used=0))
        return sample

    at = _run_app(monkeypatch, scout)
    assert not at.exception
    assert seen["inputs"].assumption_overrides["retailer_markup"] == 2.2
    assert seen["inputs"].unit_cost_inr == 650
    assert seen["compare_markets"] is True
    assert [t.label for t in at.tabs] == app_module.TABS
    assert any("1. Identify" in m.value for m in at.markdown)


def test_compare_markets_checkbox_is_passed_to_the_agent(monkeypatch, sample: Brief):
    seen = {}

    def scout(inputs, *, serp, llm, on_event=None, compare_markets=True):
        seen["compare_markets"] = compare_markets
        return sample.model_copy(update={"markets": []})

    at = _run_app(monkeypatch, scout, compare_markets=False)
    assert not at.exception
    assert seen["compare_markets"] is False
    tab = at.tabs[app_module.TABS.index("Markets")]
    assert "Compare other markets" in tab.info[0].value
    assert next(m for m in at.metric if m.label == "Best other market").value == "—"


def test_demo_cache_miss_shows_a_friendly_message(monkeypatch):
    def scout(inputs, *, serp, llm, on_event=None, compare_markets=True):
        raise CacheMiss("google_lens", {"q": "x"})

    at = _run_app(monkeypatch, scout)
    assert not at.exception
    assert [e.value for e in at.error] == [app_module.DEMO_MISS]
    assert not at.tabs
