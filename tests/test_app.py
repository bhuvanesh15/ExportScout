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


def _run_app(monkeypatch, scout) -> AppTest:
    monkeypatch.delenv("EXPORTSCOUT_SAMPLE_BRIEF", raising=False)
    monkeypatch.setenv("SERPAPI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(orchestrator, "make_clients", lambda **kw: (FakeSerp(), None))
    monkeypatch.setattr(orchestrator, "run_scout", scout)
    test = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    test.run()
    assert not test.tabs
    next(b for b in test.button if b.label == "Scout the UK market").click().run()
    return test


def test_scout_button_runs_the_agent_and_shows_the_brief(monkeypatch, sample: Brief):
    seen = {}

    def scout(inputs, *, serp, llm, on_event=None):
        seen["inputs"] = inputs
        serp.cache_hits = 2
        on_event(StepEvent(step=1, name="Identify", message="Found 14 UK look-alikes", credits_used=0))
        on_event(StepEvent(step=8, name="Write", message="Brief ready", credits_used=0))
        return sample

    at = _run_app(monkeypatch, scout)
    assert not at.exception
    assert seen["inputs"].assumption_overrides["retailer_markup"] == 2.2
    assert seen["inputs"].unit_cost_inr == 650
    assert [t.label for t in at.tabs] == app_module.TABS
    assert any("1. Identify" in m.value for m in at.markdown)


def test_demo_cache_miss_shows_a_friendly_message(monkeypatch):
    def scout(inputs, *, serp, llm, on_event=None):
        raise CacheMiss("google_lens", {"q": "x"})

    at = _run_app(monkeypatch, scout)
    assert not at.exception
    assert [e.value for e in at.error] == [app_module.DEMO_MISS]
    assert not at.tabs
