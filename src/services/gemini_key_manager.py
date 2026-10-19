"""Resilient multi-key pool for the Gemini API (Google AI Studio REST, via httpx).

`GeminiKeyManager.execute_with_retry` sends a request with the current healthy key. If Google answers
429 RESOURCE_EXHAUSTED, the key is put on cooldown (per-minute limit: the server's retry delay or 60 s;
per-day quota: 24 h) and the SAME call is retried at once with the next healthy key. When no key is
usable it raises `AllApiKeysExhaustedError` carrying the earliest reset time, without making a request.

Statuses: ACTIVE, RATE_LIMITED (cooling down, resets by itself), QUOTA_EXHAUSTED (daily quota, resets
after 24 h), INVALID (rejected by Google; never resets - remove it).

Secrets: a key is sent only in the `x-goog-api-key` header (never in the URL), is masked
(`AIzaSy...4xQ1`) in every status/log/error output, and is identified externally by a short SHA-256
hash. Keys are held in memory; they are written to disk only if `persist_path` is given (owner-only
permissions, best effort) - leave it unset to keep secrets out of files.

Not covered: Google's cost / paid-tier billing. Free-tier prompts may be used by Google to improve its
products; check your data-handling terms before sending business data.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import httpx

log = logging.getLogger("migration.gemini")

DEFAULT_MODEL = "gemini-flash-latest"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
RATE_LIMIT_DEFAULT_COOLDOWN = 60
MAX_RATE_LIMIT_COOLDOWN = 3600
DAILY_COOLDOWN = 24 * 3600
TRANSIENT_RETRIES = 2

ACTIVE, RATE_LIMITED, QUOTA_EXHAUSTED, INVALID = "ACTIVE", "RATE_LIMITED", "QUOTA_EXHAUSTED", "INVALID"


# ---------------------------------------------------------------------- errors
class GeminiError(Exception):
    pass


class NoApiKeysConfiguredError(GeminiError):
    """The pool has no keys at all."""


class KeyAlreadyRegisteredError(GeminiError, ValueError):
    pass


class GeminiRequestError(GeminiError):
    """Google rejected the request itself (400 etc.): rotating keys would not help."""


class GeminiServiceError(GeminiError):
    """Google answered 5xx / the network failed, after retries on the same key."""


class GeminiRateLimitError(GeminiError):
    """The retry budget (`max_retries`) ran out while keys were still rate limited."""


class AllApiKeysExhaustedError(GeminiError):
    """Every key is cooling down, out of quota, or invalid."""

    def __init__(self, reset_at: Optional[datetime], retry_after_seconds: Optional[int], keys: list[dict[str, Any]]) -> None:
        when = f"earliest reset {reset_at.isoformat()} (in {retry_after_seconds}s)" if reset_at else "no key will reset by itself"
        super().__init__(f"all {len(keys)} Gemini API key(s) are exhausted; {when}")
        self.reset_at = reset_at
        self.retry_after_seconds = retry_after_seconds
        self.keys = keys


class _KeyRateLimited(Exception):
    def __init__(self, retry_after: Optional[float], daily: bool, detail: str) -> None:
        self.retry_after, self.daily, self.detail = retry_after, daily, detail


class _KeyRejected(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail


# ---------------------------------------------------------------------- helpers
def mask_key(api_key: str) -> str:
    """AIzaSyAbc...4xQ1 style: first 6 and last 4 characters only."""
    if len(api_key) <= 12:
        return api_key[:2] + "..." + api_key[-2:]
    return f"{api_key[:6]}...{api_key[-4:]}"


def key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


_JSON_TYPES = {"object": "OBJECT", "string": "STRING", "number": "NUMBER", "integer": "INTEGER",
               "boolean": "BOOLEAN", "array": "ARRAY"}
_DROP = {"title", "default", "additionalProperties", "$defs", "examples", "$schema", "const"}


def to_gemini_schema(schema: Any) -> dict[str, Any]:
    """Pydantic model / JSON Schema -> the OpenAPI subset Gemini accepts as `responseSchema`."""
    raw = schema.model_json_schema() if hasattr(schema, "model_json_schema") else dict(schema)
    defs = raw.get("$defs", {})

    def convert(node: Any) -> Any:
        if isinstance(node, list):
            return [convert(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return convert(defs[node["$ref"].rsplit("/", 1)[-1]])
        if "anyOf" in node:  # Optional[X] -> X + nullable
            options = [convert(o) for o in node["anyOf"] if o.get("type") != "null"]
            out = dict(options[0]) if len(options) == 1 else {"anyOf": options}
            if len(options) != len(node["anyOf"]):
                out["nullable"] = True
            return out
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _DROP:
                continue
            if key == "type":
                out[key] = _JSON_TYPES.get(value, str(value).upper())
            elif key == "properties":
                out[key] = {name: convert(sub) for name, sub in value.items()}
            elif key in ("items", "anyOf"):
                out[key] = convert(value)
            else:
                out[key] = value
        return out

    return convert(raw)


@dataclass
class _Key:
    key: str
    label: str
    source: str  # "env" | "added"
    added_at: float
    status: str = ACTIVE
    available_at: Optional[float] = None
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    rate_limit_count: int = 0
    last_error: Optional[str] = None
    last_used_at: Optional[float] = None
    hash: str = field(init=False)

    def __post_init__(self) -> None:
        self.hash = key_hash(self.key)


@dataclass(frozen=True)
class GeminiResponse:
    text: str
    model: str
    key_hash: str
    key_label: str
    attempts: int  # requests sent for this call (1 = first key worked)
    resolved_model: Optional[str] = None  # the concrete model version Google says answered (an alias such as "...-latest" resolves to one)


class GeminiKeyManager:
    def __init__(
        self,
        keys: Optional[list[str]] = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        client: Optional[httpx.Client] = None,
        transport: Optional[httpx.BaseTransport] = None,
        persist_path: Optional[str | Path] = None,
        strategy: str = "sticky",
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 60.0,
    ) -> None:
        if strategy not in ("sticky", "round_robin"):
            raise ValueError("strategy must be 'sticky' or 'round_robin'")
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout, transport=transport)
        self._persist_path = Path(persist_path) if persist_path else None
        self._strategy = strategy
        self._clock, self._sleep = clock, sleep
        self._keys: list[_Key] = []
        self._current = 0
        self._lock = threading.RLock()
        if self._persist_path and self._persist_path.is_file():
            for entry in json.loads(self._persist_path.read_text(encoding="utf-8")).get("keys", []):
                self._register(entry["key"], entry.get("label", ""), "added", entry.get("added_at"))
        for index, api_key in enumerate(keys or [], start=1):
            try:
                self._register(api_key, f"env-{index}", "env")
            except KeyAlreadyRegisteredError:
                pass

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, **kwargs: Any) -> "GeminiKeyManager":
        """GEMINI_API_KEYS="k1,k2,k3" (or a single GEMINI_API_KEY); GEMINI_MODEL overrides the model."""
        env = os.environ if env is None else env
        raw = env.get("GEMINI_API_KEYS") or env.get("GEMINI_API_KEY") or ""
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        kwargs.setdefault("model", env.get("GEMINI_MODEL") or DEFAULT_MODEL)
        return cls(keys=keys, **kwargs)

    # ------------------------------------------------------------------ registry
    @staticmethod
    def _validate(api_key: str) -> str:
        key = (api_key or "").strip()
        if not 16 <= len(key) <= 256 or re.search(r"\s", key):
            raise ValueError("that does not look like an API key (16-256 characters, no spaces)")
        return key

    def _register(self, api_key: str, label: str, source: str, added_at: Optional[float] = None) -> _Key:
        key = self._validate(api_key)
        with self._lock:
            if any(k.key == key for k in self._keys):
                raise KeyAlreadyRegisteredError("this API key is already in the pool")
            record = _Key(key=key, label=(label or "").strip()[:60], source=source, added_at=added_at or self._clock())
            self._keys.append(record)
            return record

    def add_key(self, api_key: str, label: str = "") -> dict[str, Any]:
        """Register a key as ACTIVE. Returns its masked description (never the key itself)."""
        record = self._register(api_key, label, "added")
        self._persist()
        return self._describe(record)

    def remove_key(self, hash_id: str) -> bool:
        with self._lock:
            for i, record in enumerate(self._keys):
                if record.hash == hash_id:
                    del self._keys[i]
                    self._current = 0 if not self._keys else min(self._current, len(self._keys) - 1)
                    break
            else:
                return False
        self._persist()
        return True

    def _persist(self) -> None:
        if self._persist_path is None:
            return
        with self._lock:
            payload = {"keys": [{"key": k.key, "label": k.label, "added_at": k.added_at} for k in self._keys if k.source == "added"]}
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._persist_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)  # owner-only where the OS supports it
        except OSError:
            pass
        os.replace(tmp, self._persist_path)

    # ------------------------------------------------------------------ health / selection
    def _refresh(self) -> None:
        now = self._clock()
        for k in self._keys:
            if k.status in (RATE_LIMITED, QUOTA_EXHAUSTED) and k.available_at is not None and now >= k.available_at:
                k.status, k.available_at = ACTIVE, None

    def get_active_key(self) -> str:
        """The next healthy key (sticky: stays on one key until it is exhausted).

        Raises NoApiKeysConfiguredError for an empty pool and AllApiKeysExhaustedError when nothing is usable.
        """
        return self._select().key

    def _select(self) -> _Key:
        with self._lock:
            if not self._keys:
                raise NoApiKeysConfiguredError("no Gemini API keys are configured (set GEMINI_API_KEYS or add one in the UI)")
            self._refresh()
            n = len(self._keys)
            start = self._current if self._strategy == "sticky" else (self._current + 1) % n
            for offset in range(n):
                index = (start + offset) % n
                if self._keys[index].status == ACTIVE:
                    self._current = index
                    return self._keys[index]
            raise self._exhausted_error()

    def _exhausted_error(self) -> AllApiKeysExhaustedError:
        now = self._clock()
        resets = [k.available_at for k in self._keys if k.status in (RATE_LIMITED, QUOTA_EXHAUSTED) and k.available_at]
        earliest = min(resets) if resets else None
        return AllApiKeysExhaustedError(
            datetime.fromtimestamp(earliest, timezone.utc) if earliest else None,
            max(0, int(earliest - now + 0.999)) if earliest else None, [self._describe(k) for k in self._keys])

    def has_usable_keys(self) -> bool:
        with self._lock:
            self._refresh()
            return any(k.status == ACTIVE for k in self._keys)

    def mark_exhausted(self, api_key: str, cooldown_seconds: int = RATE_LIMIT_DEFAULT_COOLDOWN, is_daily_quota: bool = False) -> dict[str, Any]:
        """RATE_LIMITED for `cooldown_seconds`, or QUOTA_EXHAUSTED for 24 h when `is_daily_quota`."""
        with self._lock:
            record = self._find(api_key)
            wait = DAILY_COOLDOWN if is_daily_quota else max(1, int(cooldown_seconds))
            record.status = QUOTA_EXHAUSTED if is_daily_quota else RATE_LIMITED
            record.available_at = self._clock() + wait
            record.rate_limit_count += 1
            return self._describe(record)

    def _mark_invalid(self, record: _Key, detail: str) -> None:
        with self._lock:
            record.status, record.available_at, record.last_error = INVALID, None, detail

    def _find(self, api_key: str) -> _Key:
        for record in self._keys:
            if record.key == api_key:
                return record
        raise KeyError("API key is not in the pool")

    def _describe(self, k: _Key) -> dict[str, Any]:
        now = self._clock()
        remaining = max(0, int(k.available_at - now + 0.999)) if k.available_at else 0
        return {
            "key_hash": k.hash, "masked_key": mask_key(k.key), "label": k.label, "source": k.source, "status": k.status,
            "available_at": datetime.fromtimestamp(k.available_at, timezone.utc).isoformat() if k.available_at else None,
            "seconds_until_available": remaining, "request_count": k.request_count, "success_count": k.success_count,
            "error_count": k.error_count, "rate_limit_count": k.rate_limit_count, "last_error": k.last_error,
            "last_used_at": datetime.fromtimestamp(k.last_used_at, timezone.utc).isoformat() if k.last_used_at else None,
            "added_at": datetime.fromtimestamp(k.added_at, timezone.utc).isoformat(),
        }

    def get_pool_status(self) -> list[dict[str, Any]]:
        """Every key, masked, with status, reset time and request / error counters."""
        with self._lock:
            self._refresh()
            return [self._describe(k) for k in self._keys]

    # ------------------------------------------------------------------ requests
    def _url(self, suffix: str) -> str:
        return f"{self._base_url}/models/{self.model}{suffix}"

    def _body(self, prompt: str, system_instruction: Optional[str], schema: Any) -> dict[str, Any]:
        config: dict[str, Any] = {"temperature": 0}
        if schema is not None:
            config["responseMimeType"] = "application/json"
            config["responseSchema"] = to_gemini_schema(schema)
        if "2.5-flash" in self.model:
            config["thinkingConfig"] = {"thinkingBudget": 0}  # no hidden reasoning tokens: faster, cheaper JSON
        body: dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": config}
        if system_instruction:
            body["systemInstruction"] = {"parts": [{"text": system_instruction}]}
        return body

    def execute_with_retry(
        self, prompt: str, system_instruction: Optional[str] = None, schema: Any = None, max_retries: Optional[int] = None
    ) -> GeminiResponse:
        """Generate content, rotating to the next key whenever the current one is rate limited.

        `max_retries` = extra attempts after the first (default: one per key in the pool).
        """
        with self._lock:
            budget = (len(self._keys) if max_retries is None else max_retries) + 1
        body = self._body(prompt, system_instruction, schema)
        attempts = 0
        last_limit: Optional[_KeyRateLimited] = None
        while attempts < budget:
            record = self._select()  # raises when nothing is usable
            attempts += 1
            try:
                text, resolved = self._send(record, body)
            except _KeyRateLimited as exc:
                last_limit = exc
                wait = DAILY_COOLDOWN if exc.daily else int(min(MAX_RATE_LIMIT_COOLDOWN, (exc.retry_after or RATE_LIMIT_DEFAULT_COOLDOWN)) + 1)
                self.mark_exhausted(record.key, wait, exc.daily)
                with self._lock:
                    record.last_error = exc.detail
                log.warning("Gemini key %s (%s) %s; cooling down %ss and switching to the next key",
                            mask_key(record.key), record.label or "unlabelled",
                            "hit its DAILY quota" if exc.daily else "is rate limited", wait)
                continue
            except _KeyRejected as exc:
                self._mark_invalid(record, exc.detail)
                log.warning("Gemini key %s (%s) was rejected by Google (%s); disabling it and switching",
                            mask_key(record.key), record.label or "unlabelled", exc.detail)
                continue
            return GeminiResponse(text=text, model=self.model, key_hash=record.hash, key_label=record.label, attempts=attempts, resolved_model=resolved)
        # retry budget used up while healthy keys may remain
        try:
            self._select()
        except AllApiKeysExhaustedError:
            raise
        raise GeminiRateLimitError(f"still rate limited after {attempts} attempt(s): {last_limit.detail if last_limit else 'unknown'}")

    def _send(self, record: _Key, body: dict[str, Any]) -> tuple[str, Optional[str]]:
        """One request with one key -> (answer text, the model version Google reports). Transient failures are retried on the same key."""
        headers = {"x-goog-api-key": record.key, "Content-Type": "application/json"}  # never in the URL
        last_exc: Optional[Exception] = None
        for transient in range(TRANSIENT_RETRIES + 1):
            with self._lock:
                record.request_count += 1
                record.last_used_at = self._clock()
            try:
                response = self._client.post(self._url(":generateContent"), json=body, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                self._count_error(record, f"network error: {type(exc).__name__}")
            else:
                status = response.status_code
                if status == 200:
                    text = self._extract(response)
                    with self._lock:
                        record.success_count += 1
                    version = self._json(response).get("modelVersion")
                    return text, str(version) if version else None
                self._count_error(record, f"HTTP {status}")
                if status == 429:
                    daily, retry_after, detail = self._classify_429(response)
                    raise _KeyRateLimited(retry_after, daily, detail)
                if self._is_key_rejection(response):
                    raise _KeyRejected(f"HTTP {status} {self._error_reason(response)}")
                if status < 500:
                    raise GeminiRequestError(f"Gemini rejected the request (HTTP {status}): {self._error_message(response)}")
                last_exc = GeminiServiceError(f"Gemini server error HTTP {status}")
            if transient < TRANSIENT_RETRIES:
                self._sleep(min(2.0 ** transient, 4.0))
        raise GeminiServiceError(f"Gemini is unavailable after {TRANSIENT_RETRIES + 1} attempts: {last_exc}")

    def _count_error(self, record: _Key, detail: str) -> None:
        with self._lock:
            record.error_count += 1
            record.last_error = detail

    # ---- response parsing
    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def _error_status(self, response: httpx.Response) -> str:
        return str((self._json(response).get("error") or {}).get("status", ""))

    def _error_reason(self, response: httpx.Response) -> str:
        """Google's machine-readable reason (e.g. API_KEY_INVALID), falling back to the status."""
        error = self._json(response).get("error") or {}
        for detail in error.get("details") or []:
            if detail.get("reason"):
                return str(detail["reason"])
        return str(error.get("status", ""))

    def _error_message(self, response: httpx.Response) -> str:
        message = str((self._json(response).get("error") or {}).get("message", response.text))[:300]
        return re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "AIza***", message)  # never echo a key Google quoted back

    def _is_key_rejection(self, response: httpx.Response) -> bool:
        error = self._json(response).get("error") or {}
        text = json.dumps(error).upper()
        return (response.status_code in (401, 403)
                or "API_KEY_INVALID" in text or "API KEY NOT VALID" in text or "API KEY EXPIRED" in text)

    def _classify_429(self, response: httpx.Response) -> tuple[bool, Optional[float], str]:
        error = self._json(response).get("error") or {}
        retry_after: Optional[float] = None
        daily = False
        for detail in error.get("details") or []:
            kind = str(detail.get("@type", ""))
            if kind.endswith("RetryInfo"):
                match = re.match(r"([0-9.]+)s", str(detail.get("retryDelay", "")))
                retry_after = float(match.group(1)) if match else retry_after
            if kind.endswith("QuotaFailure"):
                for violation in detail.get("violations") or []:
                    if "perday" in (str(violation.get("quotaId", "")) + str(violation.get("quotaMetric", ""))).lower().replace("_", ""):
                        daily = True
        if "per day" in str(error.get("message", "")).lower():
            daily = True
        header = response.headers.get("retry-after")
        if retry_after is None and header and header.replace(".", "", 1).isdigit():
            retry_after = float(header)
        return daily, retry_after, f"429 {error.get('status') or 'RESOURCE_EXHAUSTED'}" + (" (daily quota)" if daily else "")

    def _extract(self, response: httpx.Response) -> str:
        data = self._json(response)
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates returned")
            raise GeminiRequestError(f"Gemini returned no answer ({reason})")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        if not text:
            raise GeminiRequestError(f"Gemini returned an empty answer (finishReason={candidates[0].get('finishReason')})")
        return text

    # ------------------------------------------------------------------ health check
    def test_pooled_key(self, hash_id: str) -> dict[str, Any]:
        with self._lock:
            record = next((k for k in self._keys if k.hash == hash_id), None)
            secret = record.key if record else None
        if secret is None:
            raise KeyError("API key is not in the pool")
        return self.test_key(secret)

    def test_key(self, api_key: str) -> dict[str, Any]:
        """Verify a key without touching pool state or consuming generation quota (models.get)."""
        key = self._validate(api_key)
        started = self._clock()
        try:
            response = self._client.get(self._url(""), headers={"x-goog-api-key": key})
        except httpx.HTTPError as exc:
            return {"ok": False, "classification": "unreachable", "status_code": None, "detail": f"cannot reach Google: {type(exc).__name__}",
                    "latency_ms": int((self._clock() - started) * 1000), "model": self.model}
        status = response.status_code
        if status == 200:
            verdict, detail = "valid", "key accepted"
        elif status == 429:
            verdict, detail = "rate_limited", "key is valid but currently rate limited"
        elif self._is_key_rejection(response):
            verdict, detail = "invalid", "Google rejected this key"
        elif status == 404:
            verdict, detail = "model_unavailable", f"key accepted but model '{self.model}' was not found"
        else:
            verdict, detail = "error", f"HTTP {status}: {self._error_message(response)}"
        return {"ok": verdict in ("valid", "rate_limited"), "classification": verdict, "status_code": status, "detail": detail,
                "latency_ms": int((self._clock() - started) * 1000), "model": self.model}
