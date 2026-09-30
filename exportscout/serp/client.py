"""SerpApi access for ExportScout: local cache, credit ledger, budget and record/replay.

Every SerpApi request in the app goes through ``SerpClient``. It has three modes:

- ``live``: return a fresh cached response if there is one, otherwise call SerpApi.
- ``record``: same as live, and also write each response to the fixture directory.
  Demo Mode replays these files.
- ``replay``: never call SerpApi. Read the fixture directory, then the cache (any age).
  A miss raises ``CacheMiss``.

Responses are stored without the API key, so fixture files are safe to commit.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import httpx

SEARCH_URL = "https://serpapi.com/search.json"
IMAGE_URL = "https://serpapi.com/image"
ACCOUNT_URL = "https://serpapi.com/account.json"

Mode = Literal["live", "record", "replay"]
Source = Literal["network", "cache", "fixture"]

# Hours a cached response stays fresh in live/record mode. Replay ignores TTLs.
TTL_HOURS: dict[str, float] = {
    "google_finance": 6,
    "google_news": 12,
    "amazon": 72,
    "ebay": 72,
    "google_shopping": 72,
    "google_ads_transparency_center": 72,
    "google_jobs": 72,
    "amazon_product": 168,
    "google": 168,
    "google_trends": 168,
    "google_maps": 336,
    "google_autocomplete": 336,
    "google_lens": 720,
}
DEFAULT_TTL_HOURS = 72.0

# Params that don't change the result, so they are left out of the cache key.
_KEY_EXCLUDED = {"api_key", "no_cache", "async", "output"}

# Job ads can quote a recruiter's email or phone: these are removed before a response is
# cached or recorded, so only company-level data is ever stored.
SCRUB_CONTACTS_ENGINES = {"google_jobs"}
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")

# SerpApi reports "no results" as an error, but it is a valid empty answer worth caching.
_EMPTY_RESULT_MARKER = "hasn't returned any results"

MAX_UPLOAD_BYTES = 500 * 1024  # Image API limit
_UPLOAD_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}

_KEY_IN_URL = re.compile(r"(api_key=)[^&\"'\s]+")


class SerpApiError(RuntimeError):
    """SerpApi returned an error (bad key, no credits left, bad params) or was unreachable."""


class BudgetExceeded(RuntimeError):
    """The run's credit budget is used up; the caller should stop searching and use what it has."""


class CacheMiss(LookupError):
    """Replay mode has no recorded response for this request."""

    def __init__(self, engine: str, params: dict[str, Any]):
        super().__init__(f"no recorded response for {engine} {params}")
        self.engine = engine
        self.params = params


@dataclass(frozen=True)
class SerpResponse:
    engine: str
    params: dict[str, str]  # request params used for the cache key, without the API key
    data: dict[str, Any]
    fetched_at: str  # ISO-8601 UTC time of the SerpApi call
    source: Source
    cache_key: str

    @property
    def credits(self) -> int:
        return 1 if self.source == "network" else 0


def env_api_key() -> str:
    """SerpApi key from SERPAPI_API_KEY, or the SERPAPI_KEY alias."""
    return os.environ.get("SERPAPI_API_KEY") or os.environ.get("SERPAPI_KEY") or ""


def _param_str(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def normalize_params(params: dict[str, Any]) -> dict[str, str]:
    return {k: _param_str(v) for k, v in params.items() if v is not None and k not in _KEY_EXCLUDED}


def cache_key(engine: str, params: dict[str, Any]) -> str:
    blob = json.dumps({"engine": engine, "params": normalize_params(params)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def redact_text(text: str, secret: str) -> str:
    if secret:
        text = text.replace(secret, "REDACTED")
    return _KEY_IN_URL.sub(r"\1REDACTED", text)


def redact(obj: Any, secret: str) -> Any:
    """Remove the API key from any string inside a JSON-like object."""
    return json.loads(redact_text(json.dumps(obj, ensure_ascii=False), secret))


def scrub_contacts(obj: Any) -> Any:
    """Replace email addresses and phone numbers in the text of a JSON-like object.
    URLs and link/token fields are left alone: their long digit runs are IDs, not phone numbers."""
    if isinstance(obj, str):
        if obj.startswith(("http://", "https://")):
            return obj
        return _PHONE.sub("[phone]", _EMAIL.sub("[email]", obj))
    if isinstance(obj, list):
        return [scrub_contacts(x) for x in obj]
    if isinstance(obj, dict):
        return {k: v if _is_link_key(k) else scrub_contacts(v) for k, v in obj.items()}
    return obj


def _is_link_key(key: str) -> bool:
    k = key.lower()
    return k.startswith("search_") or k.endswith("_at") or any(
        part in k for part in ("link", "url", "token", "thumbnail", "_id", "serpapi")
    )


def prepare_image(raw: bytes, limit: int = MAX_UPLOAD_BYTES) -> tuple[bytes, str]:
    """Return (bytes, mime type) in a format and size the Image API accepts."""
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(raw))
    if img.format in _UPLOAD_FORMATS and len(raw) <= limit:
        return raw, _UPLOAD_FORMATS[img.format]
    img = ImageOps.exif_transpose(img).convert("RGB")
    side = 1600
    while True:
        resized = img.copy()
        resized.thumbnail((side, side))
        buf = io.BytesIO()
        resized.save(buf, "JPEG", quality=85, optimize=True)
        if buf.tell() <= limit or side <= 320:
            return buf.getvalue(), "image/jpeg"
        side = int(side * 0.8)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_hours(fetched_at: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)).total_seconds() / 3600


_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    cache_key  TEXT PRIMARY KEY,
    engine     TEXT NOT NULL,
    params     TEXT NOT NULL,
    data       TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    run_id    TEXT,
    engine    TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    source    TEXT NOT NULL,
    credits   INTEGER NOT NULL
);
"""


class SerpClient:
    """Cached SerpApi client. Thread-safe, so pipeline steps can search in parallel.

    ``budget`` caps the number of live SerpApi searches this client will make; the next
    live search raises ``BudgetExceeded``. Cache and fixture hits are free and never count.
    ``prefer_fixtures`` makes live/record mode reuse a recorded fixture (any age) before
    spending a credit, so searches already paid for are never bought twice.
    ``listener`` is called with every ``SerpResponse``; the UI uses it for the step log
    and the credit meter.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        mode: Mode = "live",
        cache_path: str | Path = ".cache/serp.sqlite",
        fixture_dir: str | Path | None = None,
        budget: int | None = None,
        run_id: str | None = None,
        listener: Callable[[SerpResponse], None] | None = None,
        ttl_hours: dict[str, float] | None = None,
        prefer_fixtures: bool = False,
        http: httpx.Client | None = None,
    ):
        self.api_key = api_key if api_key is not None else env_api_key()
        if mode != "replay" and not self.api_key:
            raise ValueError("SERPAPI_API_KEY is not set. Add it to .env, or use mode='replay' (Demo Mode).")
        if mode == "record" and fixture_dir is None:
            raise ValueError("record mode needs a fixture_dir to write to")
        self.mode: Mode = mode
        self.fixture_dir = Path(fixture_dir) if fixture_dir is not None else None
        self.budget = budget
        self.prefer_fixtures = prefer_fixtures
        self.run_id = run_id
        self.listener = listener
        self.ttl_hours = {**TTL_HOURS, **(ttl_hours or {})}
        self.credits_used = 0
        self.cache_hits = 0
        self._lock = threading.Lock()
        self._http = http or httpx.Client(timeout=httpx.Timeout(90.0, connect=15.0))
        cache_path = Path(cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(cache_path, check_same_thread=False)
        self._db.executescript(_SCHEMA)

    # -- public API ---------------------------------------------------------------------

    def search(self, engine: str, **params: Any) -> SerpResponse:
        """Run one SerpApi search, e.g. ``search("amazon", k="brass lantern", amazon_domain="amazon.co.uk")``."""
        return self._fetch(engine, params, lambda: params)

    def lens(self, image: str | Path | bytes | None = None, *, url: str | None = None, **params: Any) -> SerpResponse:
        """Google Lens by public image URL, or by local image (uploaded through the Image API).

        A local image is cached by a hash of its bytes: every upload gets a new ``image_id``
        that expires after 10 minutes, so the ID can't be part of the cache key.
        """
        if (image is None) == (url is None):
            raise ValueError("pass exactly one of image or url")
        if url is not None:
            return self.search("google_lens", url=url, **params)
        raw = image if isinstance(image, bytes) else Path(image).read_bytes()
        key_params = {**params, "image_sha256": hashlib.sha256(raw).hexdigest()}
        return self._fetch("google_lens", key_params, lambda: {**params, "image_id": self.upload_image(raw)})

    def upload_image(self, raw: bytes) -> str:
        """Upload an image to SerpApi's Image API and return its ``image_id`` (valid 10 minutes)."""
        body, mime = prepare_image(raw)
        data = self._request(
            "POST", IMAGE_URL, data={"api_key": self.api_key}, files={"image": ("upload", body, mime)}
        )
        if "image_id" not in data:
            raise SerpApiError(f"image upload failed: {data.get('error', 'no image_id in response')}")
        return data["image_id"]

    def account(self) -> dict[str, Any] | None:
        """Plan and usage from the free Account API (costs no search credit). None in replay mode."""
        if self.mode == "replay":
            return None
        data = self._request("GET", ACCOUNT_URL, params={"api_key": self.api_key})
        if "error" in data:
            raise SerpApiError(f"account: {data['error']}")
        data.pop("api_key", None)
        return redact(data, self.api_key)

    def close(self) -> None:
        self._http.close()
        self._db.close()

    def __enter__(self) -> SerpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ----------------------------------------------------------------------

    def _fetch(
        self, engine: str, key_params: dict[str, Any], build_request: Callable[[], dict[str, Any]]
    ) -> SerpResponse:
        norm = normalize_params(key_params)
        key = cache_key(engine, norm)
        if self.mode == "replay":
            resp = self._read_fixture(engine, key) or self._read_cache(key, max_age_hours=None)
            if resp is None:
                raise CacheMiss(engine, norm)
        else:
            resp = self._read_fixture(engine, key) if self.prefer_fixtures else None
            if resp is None:
                resp = self._read_cache(key, max_age_hours=self.ttl_hours.get(engine, DEFAULT_TTL_HOURS))
            if resp is None:
                resp = self._call_search(engine, norm, key, build_request())
                self._write_cache(resp)
            if self.mode == "record" and resp.source != "fixture":
                self._write_fixture(resp)
        self._log(resp)
        return resp

    def _call_search(self, engine: str, norm: dict[str, str], key: str, request: dict[str, Any]) -> SerpResponse:
        with self._lock:
            if self.budget is not None and self.credits_used >= self.budget:
                raise BudgetExceeded(f"credit budget of {self.budget} reached; skipped {engine}")
            self.credits_used += 1
        try:
            query = {**normalize_params(request), "engine": engine, "api_key": self.api_key}
            data = self._request("GET", SEARCH_URL, params=query)
            error = data.get("error")
            if error and _EMPTY_RESULT_MARKER not in error:
                raise SerpApiError(f"{engine}: {redact_text(error, self.api_key)}")
        except Exception:
            with self._lock:
                self.credits_used -= 1
            raise
        data = redact(data, self.api_key)
        if engine in SCRUB_CONTACTS_ENGINES:
            data = scrub_contacts(data)
        return SerpResponse(engine, norm, data, _now(), "network", key)

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """HTTP call with retries on network errors and 5xx. Error messages never contain the key."""
        attempts = 3
        last_error = ""
        for attempt in range(attempts):
            try:
                r = self._http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if r.status_code < 500:
                    try:
                        data = r.json()
                    except ValueError:
                        raise SerpApiError(f"HTTP {r.status_code}: response is not JSON") from None
                    if r.status_code >= 400 and "error" not in data:
                        raise SerpApiError(f"HTTP {r.status_code}")
                    return data
                last_error = f"HTTP {r.status_code}"
            if attempt < attempts - 1:
                time.sleep(2**attempt)
        raise SerpApiError(f"SerpApi unreachable after {attempts} attempts ({redact_text(last_error, self.api_key)})")

    def _read_cache(self, key: str, max_age_hours: float | None) -> SerpResponse | None:
        with self._lock:
            row = self._db.execute(
                "SELECT engine, params, data, fetched_at FROM responses WHERE cache_key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        engine, params, data, fetched_at = row
        if max_age_hours is not None and _age_hours(fetched_at) > max_age_hours:
            return None
        return SerpResponse(engine, json.loads(params), json.loads(data), fetched_at, "cache", key)

    def _write_cache(self, resp: SerpResponse) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO responses (cache_key, engine, params, data, fetched_at) VALUES (?, ?, ?, ?, ?)",
                (
                    resp.cache_key,
                    resp.engine,
                    json.dumps(resp.params, ensure_ascii=False),
                    json.dumps(resp.data, ensure_ascii=False),
                    resp.fetched_at,
                ),
            )

    def _fixture_path(self, engine: str, key: str) -> Path:
        assert self.fixture_dir is not None
        return self.fixture_dir / engine / f"{key[:16]}.json"

    def _read_fixture(self, engine: str, key: str) -> SerpResponse | None:
        if self.fixture_dir is None:
            return None
        path = self._fixture_path(engine, key)
        if not path.exists():
            return None
        rec = json.loads(path.read_text(encoding="utf-8"))
        return SerpResponse(engine, rec["params"], rec["data"], rec["fetched_at"], "fixture", key)

    def _write_fixture(self, resp: SerpResponse) -> None:
        path = self._fixture_path(resp.engine, resp.cache_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"engine": resp.engine, "params": resp.params, "fetched_at": resp.fetched_at, "data": resp.data}
        text = redact_text(json.dumps(rec, ensure_ascii=False, indent=1), self.api_key)
        path.write_text(text, encoding="utf-8")

    def _log(self, resp: SerpResponse) -> None:
        if resp.source != "network":
            with self._lock:
                self.cache_hits += 1
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO ledger (ts, run_id, engine, cache_key, source, credits) VALUES (?, ?, ?, ?, ?, ?)",
                (_now(), self.run_id, resp.engine, resp.cache_key, resp.source, resp.credits),
            )
        if self.listener is not None:
            self.listener(resp)
