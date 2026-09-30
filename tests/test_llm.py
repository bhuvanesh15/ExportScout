import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import anthropic
import pytest
from pydantic import BaseModel

from exportscout.llm.client import DEFAULT_MODEL, FALLBACK_BETA, LLM, LLMCacheMiss, LLMError, prompt_key


class Kw(BaseModel):
    product_type: str
    keywords: list[str]


class FakeAnthropic:
    """Stands in for anthropic.Anthropic: ``client.beta.messages.parse`` / ``create``.

    ``handler(kwargs)`` returns a dict (parse), a str (create), a ready response object,
    or raises.
    """

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse, create=self._create))

    def _record(self, kw):
        with self._lock:
            self.calls.append(kw)
        return self.handler(kw)

    def _parse(self, **kw):
        out = self._record(kw)
        if isinstance(out, SimpleNamespace):
            return out
        parsed = kw["output_format"].model_validate(out)
        return SimpleNamespace(
            stop_reason="end_turn", stop_details=None, parsed_output=parsed, content=[SimpleNamespace(type="text", text=json.dumps(out))]
        )

    def _create(self, **kw):
        out = self._record(kw)
        if isinstance(out, SimpleNamespace):
            return out
        return SimpleNamespace(
            stop_reason="end_turn",
            stop_details=None,
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=out)],
        )


def answer(kw):
    if "output_format" in kw:
        return {"product_type": "brass hurricane lantern", "keywords": ["brass hurricane lantern", "brass lantern", "gold lantern"]}
    return "A short **markdown** answer."


def make_llm(tmp_path, handler=answer, mode="live", **kwargs):
    fake = FakeAnthropic(handler)
    kwargs.setdefault("cache_path", tmp_path / "llm.sqlite")
    return LLM(mode=mode, client=fake, **kwargs), fake


ARGS = dict(task="identify", system="You name products.", user="Titles: brass lantern")


def test_record_then_replay_round_trip_without_network(tmp_path):
    rec_dir = tmp_path / "rec"
    llm, fake = make_llm(tmp_path, mode="record", record_dir=rec_dir)
    out = llm.parse(**ARGS, schema=Kw)
    text = llm.text(task="write", system="You write.", user="Say hi")
    assert out.product_type == "brass hurricane lantern"
    assert text == "A short **markdown** answer."
    assert len(fake.calls) == 2

    files = sorted(rec_dir.rglob("*.json"))
    assert [f.parent.name for f in files] == ["identify", "write"]
    rec = json.loads((rec_dir / "identify" / files[0].name).read_text(encoding="utf-8"))
    assert set(rec) == {"task", "model", "created_at", "output"}
    assert rec["model"] == DEFAULT_MODEL and rec["output"]["keywords"][0] == "brass hurricane lantern"
    key = prompt_key(DEFAULT_MODEL, "identify", ARGS["system"], ARGS["user"], Kw.model_json_schema())
    assert files[0].name == f"{key[:16]}.json"

    # Replay: fresh cache, no key, no client. Reads only the record dir.
    replay = LLM(mode="replay", record_dir=rec_dir, cache_path=tmp_path / "other.sqlite", api_key="")
    assert replay.available
    assert replay.parse(**ARGS, schema=Kw) == out
    assert replay.text(task="write", system="You write.", user="Say hi") == text
    assert len(fake.calls) == 2


def test_replay_miss_raises(tmp_path):
    replay = LLM(mode="replay", record_dir=tmp_path / "rec", cache_path=tmp_path / "llm.sqlite", api_key="")
    with pytest.raises(LLMCacheMiss) as err:
        replay.parse(**ARGS, schema=Kw)
    assert err.value.task == "identify"
    with pytest.raises(LLMCacheMiss):
        replay.text(**ARGS)


def test_replay_falls_back_to_local_cache(tmp_path):
    llm, fake = make_llm(tmp_path)
    out = llm.parse(**ARGS, schema=Kw)
    replay = LLM(mode="replay", record_dir=tmp_path / "empty", cache_path=tmp_path / "llm.sqlite", api_key="")
    assert replay.parse(**ARGS, schema=Kw) == out


def test_live_mode_caches_by_prompt(tmp_path):
    llm, fake = make_llm(tmp_path)
    llm.parse(**ARGS, schema=Kw)
    llm.parse(**ARGS, schema=Kw)
    assert len(fake.calls) == 1 and llm.api_calls == 1
    llm.parse(**{**ARGS, "user": "Titles: brass bowl"}, schema=Kw)
    llm.text(**ARGS)  # same prompt, but text is keyed apart from the schema
    assert len(fake.calls) == 3


def test_prompt_key_covers_every_input():
    base = prompt_key("m", "t", "s", "u", "text")
    assert base == prompt_key("m", "t", "s", "u", "text")
    variants = [("m2", "t", "s", "u", "text"), ("m", "t2", "s", "u", "text"), ("m", "t", "s2", "u", "text"),
                ("m", "t", "s", "u2", "text"), ("m", "t", "s", "u", Kw.model_json_schema())]  # fmt: skip
    assert len({base, *(prompt_key(*v) for v in variants)}) == 6


def test_request_params(tmp_path):
    llm, fake = make_llm(tmp_path)
    llm.parse(**ARGS, schema=Kw, effort="low")
    llm.text(task="write", system="You write.", user="Say hi", effort="medium")
    p, t = fake.calls
    assert p["model"] == "claude-opus-5-5" and p["output_format"] is Kw
    assert p["output_config"] == {"effort": "low"} and t["output_config"] == {"effort": "medium"}
    assert p["thinking"] == {"type": "adaptive"}
    assert p["system"] == [{"type": "text", "text": "You name products.", "cache_control": {"type": "ephemeral"}}]
    assert p["messages"] == [{"role": "user", "content": "Titles: brass lantern"}]
    assert p["betas"] == [FALLBACK_BETA] and p["fallbacks"] == "default"
    assert "output_format" not in t


@pytest.mark.parametrize(("stop", "message"), [("refusal", "refused"), ("max_tokens", "max_tokens"), ("pause_turn", "stop_reason")])
def test_bad_stop_reason_raises_and_is_not_cached(tmp_path, stop, message):
    bad = SimpleNamespace(
        stop_reason=stop,
        stop_details=SimpleNamespace(category="cyber") if stop == "refusal" else None,
        parsed_output=None,
        content=[],
    )
    llm, fake = make_llm(tmp_path, handler=lambda kw: bad)
    with pytest.raises(LLMError, match=message):
        llm.parse(**ARGS, schema=Kw)
    with pytest.raises(LLMError):
        llm.text(**ARGS)
    with pytest.raises(LLMError):
        llm.parse(**ARGS, schema=Kw)
    assert len(fake.calls) == 3  # errors are never cached


def test_refusal_message_names_category(tmp_path):
    bad = SimpleNamespace(stop_reason="refusal", stop_details=SimpleNamespace(category="bio"), content=[])
    llm, _ = make_llm(tmp_path, handler=lambda kw: bad)
    with pytest.raises(LLMError, match="refused.*bio"):
        llm.text(**ARGS)


def test_api_and_parse_errors_are_wrapped(tmp_path):
    def api_down(kw):
        raise anthropic.AnthropicError("connection reset")

    llm, _ = make_llm(tmp_path, handler=api_down)
    with pytest.raises(LLMError, match="connection reset"):
        llm.parse(**ARGS, schema=Kw)

    def truncated_json(kw):
        json.loads('{"product_type": "brass')

    llm, _ = make_llm(tmp_path, handler=truncated_json, cache_path=tmp_path / "b.sqlite")
    with pytest.raises(LLMError, match="unparseable"):
        llm.parse(**ARGS, schema=Kw)


def test_empty_text_is_an_error(tmp_path):
    llm, _ = make_llm(tmp_path, handler=lambda kw: "   ")
    with pytest.raises(LLMError, match="empty"):
        llm.text(**ARGS)


def test_available_and_lazy_client(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert LLM(mode="replay", cache_path=tmp_path / "a.sqlite").available
    no_key = LLM(mode="live", cache_path=tmp_path / "b.sqlite")
    assert not no_key.available
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        no_key.text(**ARGS)
    assert LLM(mode="live", cache_path=tmp_path / "c.sqlite", api_key="sk-test").available
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert LLM(mode="record", record_dir=tmp_path / "r", cache_path=tmp_path / "d.sqlite").available


def test_record_mode_needs_record_dir(tmp_path):
    with pytest.raises(ValueError, match="record_dir"):
        LLM(mode="record", cache_path=tmp_path / "llm.sqlite", api_key="x")


def test_record_file_is_stable_on_rerun(tmp_path):
    rec_dir = tmp_path / "rec"
    llm, _ = make_llm(tmp_path, mode="record", record_dir=rec_dir)
    llm.parse(**ARGS, schema=Kw)
    path = next(rec_dir.rglob("*.json"))
    before = path.read_text(encoding="utf-8")
    llm.parse(**ARGS, schema=Kw)
    assert path.read_text(encoding="utf-8") == before


def test_parallel_calls_are_thread_safe(tmp_path):
    llm, fake = make_llm(tmp_path)
    users = [f"Titles: item {i}" for i in range(16)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        outs = list(pool.map(lambda u: llm.parse(task="identify", system="s", user=u, schema=Kw), users * 2))
    assert len(outs) == 32
    assert 16 <= len(fake.calls) <= 32
    assert llm.api_calls == len(fake.calls)
