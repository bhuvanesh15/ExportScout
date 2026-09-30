"""Thin Claude wrapper with record/replay, mirroring SerpClient's modes.

- ``live``: return a cached response for the same prompt if there is one, otherwise call
  the Anthropic API and cache the result locally (SQLite).
- ``record``: same as live, and also write each response to ``record_dir`` (committed as
  Demo Mode data).
- ``replay``: never call the API. Read ``record_dir``, then the local cache. A miss raises
  ``LLMCacheMiss``.

Responses are keyed by a hash of (model, task, system, user, schema), so Demo Mode is
deterministic and needs no ANTHROPIC_API_KEY. The Anthropic client is created on first
use, so replay mode never imports or configures it.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ValidationError

Mode = Literal["live", "record", "replay"]
Effort = Literal["low", "medium", "high"]
T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5-5"
# Server-side refusal fallback: a declined request is re-run on Anthropic's recommended model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
TIMEOUT_S = 180.0
_OK_STOPS = {"end_turn", "stop_sequence"}


class LLMError(RuntimeError):
    """The model refused, hit max_tokens, or returned something unusable (or the API failed)."""


class LLMCacheMiss(LookupError):
    """Replay mode has no recorded response for this prompt."""

    def __init__(self, task: str, key: str):
        super().__init__(f"no recorded LLM response for task {task!r} ({key[:16]})")
        self.task = task
        self.key = key


def prompt_key(model: str, task: str, system: str, user: str, schema: dict[str, Any] | str) -> str:
    blob = json.dumps(
        {"model": model, "task": task, "system": system, "user": user, "schema": schema},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_responses (
    cache_key  TEXT PRIMARY KEY,
    task       TEXT NOT NULL,
    model      TEXT NOT NULL,
    output     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class LLM:
    """Cached Claude client. Thread-safe.

    ``client`` injects a ready Anthropic-compatible client (tests use a fake one).
    """

    def __init__(
        self,
        *,
        mode: Mode = "live",
        record_dir: str | Path | None = None,
        cache_path: str | Path = ".cache/llm.sqlite",
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        client: Any | None = None,
    ):
        if mode == "record" and record_dir is None:
            raise ValueError("record mode needs a record_dir to write to")
        self.mode: Mode = mode
        self.model = model
        self.record_dir = Path(record_dir) if record_dir is not None else None
        self._api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = client
        self.api_calls = 0
        self.replay_misses = 0  # replay lookups with no recording (the task then uses its fallback)
        self._lock = threading.Lock()
        cache_path = Path(cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(cache_path, check_same_thread=False)
        self._db.executescript(_SCHEMA)

    @property
    def available(self) -> bool:
        """False when live/record mode has no API key; callers then use deterministic fallbacks."""
        if self.mode == "replay":
            return True
        return self._client is not None or bool(self._api_key)

    # -- public API ---------------------------------------------------------------------

    def parse(
        self, *, task: str, system: str, user: str, schema: type[T], effort: Effort = "low", max_tokens: int = 16000
    ) -> T:
        """Structured output validated against ``schema``."""
        key = prompt_key(self.model, task, system, user, schema.model_json_schema())

        def call() -> dict[str, Any]:
            msg = self._send("parse", system=system, user=user, effort=effort, max_tokens=max_tokens, output_format=schema)
            parsed = getattr(msg, "parsed_output", None)
            if parsed is None:
                raise LLMError(f"{task}: no structured output in the response")
            return parsed.model_dump(mode="json")

        output = self._run(task, key, call)
        try:
            return schema.model_validate(output)
        except ValidationError as exc:
            raise LLMError(f"{task}: stored output does not match {schema.__name__}: {exc}") from exc

    def text(
        self, *, task: str, system: str, user: str, effort: Effort = "medium", max_tokens: int = 16000
    ) -> str:
        """Free-text output (Markdown)."""
        key = prompt_key(self.model, task, system, user, "text")

        def call() -> str:
            msg = self._send("create", system=system, user=user, effort=effort, max_tokens=max_tokens)
            out = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
            if not out:
                raise LLMError(f"{task}: empty text response")
            return out

        output = self._run(task, key, call)
        if not isinstance(output, str):
            raise LLMError(f"{task}: stored output is not text")
        return output

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> LLM:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ----------------------------------------------------------------------

    def _run(self, task: str, key: str, call: Any) -> Any:
        if self.mode == "replay":
            output = self._read_record(task, key)
            if output is None:
                output = self._read_cache(key)
            if output is None:
                self.replay_misses += 1
                raise LLMCacheMiss(task, key)
            return output
        output = self._read_cache(key)
        if output is None:
            # A committed recording counts as a cache hit, so re-recording on a fresh machine
            # doesn't re-ask the model: a new `identify` answer would mean new paid searches.
            output = self._read_record(task, key)
            if output is None:
                output = call()
            self._write_cache(task, key, output)
        if self.mode == "record":
            self._write_record(task, key, output)
        return output

    def _get_client(self) -> Any:
        with self._lock:
            if self._client is None:
                if not self._api_key:
                    raise LLMError("ANTHROPIC_API_KEY is not set")
                import anthropic

                self._client = anthropic.Anthropic(api_key=self._api_key, timeout=TIMEOUT_S, max_retries=2)
            return self._client

    def _send(self, method: Literal["parse", "create"], *, system: str, user: str, effort: Effort, max_tokens: int, **extra: Any) -> Any:
        client = self._get_client()
        try:
            import anthropic

            sdk_errors: tuple[type[BaseException], ...] = (anthropic.AnthropicError,)
        except ImportError:  # only reachable with an injected client
            sdk_errors = ()
        params: dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            betas=[FALLBACK_BETA],
            fallbacks="default",
            **extra,
        )
        endpoint = client.beta.messages
        try:
            msg = endpoint.parse(**params) if method == "parse" else endpoint.create(**params)
        except sdk_errors as exc:
            raise LLMError(f"Anthropic API error: {type(exc).__name__}: {exc}") from exc
        except ValueError as exc:  # structured output failed JSON/schema validation (e.g. cut off)
            raise LLMError(f"unparseable model output: {exc}") from exc
        with self._lock:
            self.api_calls += 1
        stop = getattr(msg, "stop_reason", None)
        if stop == "refusal":
            details = getattr(msg, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            raise LLMError(f"model refused (category: {category})")
        if stop == "max_tokens":
            raise LLMError(f"response hit max_tokens ({max_tokens})")
        if stop not in _OK_STOPS:
            raise LLMError(f"unexpected stop_reason {stop!r}")
        return msg

    def _read_cache(self, key: str) -> Any:
        with self._lock:
            row = self._db.execute("SELECT output FROM llm_responses WHERE cache_key = ?", (key,)).fetchone()
        return None if row is None else json.loads(row[0])

    def _write_cache(self, task: str, key: str, output: Any) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO llm_responses (cache_key, task, model, output, created_at) VALUES (?, ?, ?, ?, ?)",
                (key, task, self.model, json.dumps(output, ensure_ascii=False), _now()),
            )

    def _record_path(self, task: str, key: str) -> Path:
        assert self.record_dir is not None
        return self.record_dir / task / f"{key[:16]}.json"

    def _read_record(self, task: str, key: str) -> Any:
        if self.record_dir is None:
            return None
        path = self._record_path(task, key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["output"]

    def _write_record(self, task: str, key: str, output: Any) -> None:
        path = self._record_path(task, key)
        if path.exists() and self._read_record(task, key) == output:
            return  # keep committed fixtures stable
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"task": task, "model": self.model, "created_at": _now(), "output": output}
        path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
