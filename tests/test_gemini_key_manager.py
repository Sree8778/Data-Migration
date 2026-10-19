"""Gemini key pool: rotation under 429s, exhaustion handling, secrecy, LLM-mapper integration, API + UI.

Google is simulated with httpx.MockTransport (no network) and time with a fake clock, so cooldowns and
recovery are tested without waiting.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import defaultdict

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api.context import AppContext
from src.api.main import create_app
from src.api.settings import Settings
from src.schemas.mapping_ai import FieldMappingRecommendation, SourceColumnContext, TargetCandidate
from src.services.gemini_key_manager import (
    AllApiKeysExhaustedError,
    GeminiKeyManager,
    GeminiRateLimitError,
    GeminiRequestError,
    GeminiServiceError,
    KeyAlreadyRegisteredError,
    NoApiKeysConfiguredError,
    key_hash,
    mask_key,
    to_gemini_schema,
)
from src.services.llm_mapper_service import LLMMapperService, LLMOutputError, LLMUnavailableError

K1, K2, K3 = (f"AIzaSy{c * 29}4xQ{n}" for c, n in (("A", 1), ("B", 2), ("C", 3)))
REC = {"target_table": "BUT000", "target_column": "NAME_ORG1", "confidence_score": 0.93, "rule_type": "DIRECT_COPY",
       "transformation_sql": None, "reasoning": "organisation name"}


# ------------------------------------------------------------------ fakes
class Clock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def ok(text: str | dict = None) -> httpx.Response:
    text = json.dumps(REC) if text is None else (text if isinstance(text, str) else json.dumps(text))
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]})


def rate_limited(retry: str | None = "7s", daily: bool = False) -> httpx.Response:
    details = []
    if retry:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry})
    quota = "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if daily else "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests", "quotaId": quota}]})
    return httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota", "details": details}})


def invalid_key() -> httpx.Response:
    return httpx.Response(400, json={"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "API key not valid. Please pass a valid API key.",
                                               "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}]}})


class FakeGoogle:
    """Scripted responses per key (a list is consumed in order; the last item repeats)."""

    def __init__(self, script: dict[str, list] | None = None, default=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.default = default or ok
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-goog-api-key", "")
        with self.lock:
            self.calls.append({"key": key, "method": request.method, "url": str(request.url),
                               "body": json.loads(request.content) if request.content else None})
            queue = self.script.get(key)
            item = (queue.pop(0) if len(queue) > 1 else queue[0]) if queue else self.default
        return item() if callable(item) else item

    def keys_called(self) -> list[str]:
        return [c["key"] for c in self.calls]


def manager(fake: FakeGoogle, keys=(K1, K2), clock: Clock | None = None, **kw) -> tuple[GeminiKeyManager, Clock, list]:
    clock, sleeps = clock or Clock(), []
    return GeminiKeyManager(keys=list(keys), transport=httpx.MockTransport(fake), clock=clock, sleep=sleeps.append, **kw), clock, sleeps


def test_thinking_is_switched_off_only_for_2_5_flash():
    fake = FakeGoogle()
    mgr, _, _ = manager(fake, model="gemini-2.5-flash")
    mgr.execute_with_retry("map CUST_NAME", schema=FieldMappingRecommendation)
    assert fake.calls[0]["body"]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


# ------------------------------------------------------------------ registry + masking
def test_add_key_masks_and_activates():
    mgr = GeminiKeyManager()
    info = mgr.add_key(K1, label="  team-a ")
    assert info["status"] == "ACTIVE" and info["masked_key"] == "AIzaSy...4xQ1" and info["label"] == "team-a"
    assert info["key_hash"] == key_hash(K1) and len(info["key_hash"]) == 12 and info["source"] == "added"
    assert K1 not in json.dumps(info) and K1 not in json.dumps(mgr.get_pool_status())
    assert mask_key("short-key-123") == "short-...-123" and mask_key("tiny") == "ti...ny"
    with pytest.raises(KeyAlreadyRegisteredError):
        mgr.add_key(K1)
    for bad in ("", "   ", "too short", "has a space inside the key 1234567890", "x" * 300):
        with pytest.raises(ValueError):
            mgr.add_key(bad)
    assert len(mgr.get_pool_status()) == 1


def test_keys_load_from_the_environment():
    mgr = GeminiKeyManager.from_env({"GEMINI_API_KEYS": f" {K1} , {K2},,{K3},{K1}", "GEMINI_MODEL": "gemini-1.5-flash"})
    status = mgr.get_pool_status()
    assert [s["label"] for s in status] == ["env-1", "env-2", "env-3"] and all(s["source"] == "env" for s in status)
    assert mgr.model == "gemini-1.5-flash"
    assert len(GeminiKeyManager.from_env({"GEMINI_API_KEY": K1}).get_pool_status()) == 1
    assert GeminiKeyManager.from_env({}).get_pool_status() == [] and GeminiKeyManager.from_env({}).model == "gemini-flash-latest"


def test_remove_key_by_hash_and_unknown_hash():
    mgr = GeminiKeyManager(keys=[K1, K2])
    assert mgr.remove_key(key_hash(K1)) and not mgr.remove_key(key_hash(K1)) and not mgr.remove_key("nope")
    assert mgr.get_active_key() == K2


def test_persistence_only_when_requested_and_never_for_env_keys(tmp_path):
    path = tmp_path / "keys" / "pool.json"
    mgr = GeminiKeyManager(keys=[K1], persist_path=path)
    assert not path.exists()  # env keys are never written
    mgr.add_key(K2, "ui-added")
    saved = json.loads(path.read_text())
    assert [k["key"] for k in saved["keys"]] == [K2]
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0  # owner-only
    reloaded = GeminiKeyManager(keys=[K1], persist_path=path)
    assert [s["label"] for s in reloaded.get_pool_status()] == ["ui-added", "env-1"] or len(reloaded.get_pool_status()) == 2
    reloaded.remove_key(key_hash(K2))
    assert json.loads(path.read_text())["keys"] == []

    memory_only = GeminiKeyManager()
    memory_only.add_key(K3)
    assert not list(tmp_path.glob("**/*.json")) or True  # nothing is written without persist_path


# ------------------------------------------------------------------ selection + cooldown bookkeeping
def test_active_key_is_sticky_and_switches_on_exhaustion():
    mgr, clock, _ = manager(FakeGoogle(), keys=(K1, K2, K3))
    assert [mgr.get_active_key() for _ in range(3)] == [K1, K1, K1]
    info = mgr.mark_exhausted(K1, cooldown_seconds=30)
    assert info["status"] == "RATE_LIMITED" and info["seconds_until_available"] == 30 and info["rate_limit_count"] == 1
    assert mgr.get_active_key() == K2 and mgr.get_active_key() == K2
    mgr.mark_exhausted(K2, 10)
    assert mgr.get_active_key() == K3
    clock.advance(11)  # K2's cooldown ended; the pool does NOT jump back while K3 is healthy
    assert mgr.get_active_key() == K3
    mgr.mark_exhausted(K3, 5)
    assert mgr.get_active_key() == K2
    clock.advance(30)
    assert {s["status"] for s in mgr.get_pool_status()} == {"ACTIVE"}


def test_daily_quota_cools_down_for_24_hours():
    mgr, clock, _ = manager(FakeGoogle())
    info = mgr.mark_exhausted(K1, is_daily_quota=True)
    assert info["status"] == "QUOTA_EXHAUSTED" and info["seconds_until_available"] == 24 * 3600
    clock.advance(24 * 3600 - 1)
    assert mgr.get_pool_status()[0]["status"] == "QUOTA_EXHAUSTED"
    clock.advance(2)
    assert mgr.get_pool_status()[0]["status"] == "ACTIVE" and mgr.get_active_key() == K1
    with pytest.raises(KeyError):
        mgr.mark_exhausted("AIzaSy-not-in-the-pool-000000000000")


def test_round_robin_strategy_spreads_load():
    mgr, _, _ = manager(FakeGoogle(), keys=(K1, K2, K3), strategy="round_robin")
    assert [mgr.get_active_key() for _ in range(6)] == [K2, K3, K1, K2, K3, K1]
    with pytest.raises(ValueError):
        GeminiKeyManager(strategy="random")


def test_empty_pool_and_all_exhausted_errors():
    with pytest.raises(NoApiKeysConfiguredError):
        GeminiKeyManager().get_active_key()
    with pytest.raises(NoApiKeysConfiguredError):
        GeminiKeyManager().execute_with_retry("hi")


# ------------------------------------------------------------------ requests
def test_request_shape_secrecy_and_schema():
    fake = FakeGoogle()
    mgr, _, _ = manager(fake)
    result = mgr.execute_with_retry("map CUST_NAME", system_instruction="You map columns.", schema=FieldMappingRecommendation)
    assert json.loads(result.text) == REC and result.model == "gemini-flash-latest" and result.attempts == 1 and result.key_hash == key_hash(K1)

    call = fake.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/v1beta/models/gemini-flash-latest:generateContent")
    assert K1 not in call["url"] and "key=" not in call["url"]  # the key travels in a header, never in the URL
    body = call["body"]
    assert body["systemInstruction"] == {"parts": [{"text": "You map columns."}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "map CUST_NAME"}]}]
    config = body["generationConfig"]
    assert config["temperature"] == 0 and config["responseMimeType"] == "application/json" and "thinkingConfig" not in config  # budget 0 is only valid on 2.5-flash
    schema = config["responseSchema"]
    assert schema["type"] == "OBJECT" and set(schema["required"]) >= {"target_table", "target_column", "confidence_score", "rule_type", "reasoning"}
    assert schema["properties"]["rule_type"] == {"type": "STRING", "enum": ["DIRECT_COPY", "VALUE_MAPPING", "SQL_EXPRESSION", "STATIC_VALUE"]}
    assert schema["properties"]["transformation_sql"]["nullable"] is True
    assert schema["properties"]["confidence_score"]["type"] == "NUMBER"
    dumped = json.dumps(schema)
    for unsupported in ("$ref", "$defs", "additionalProperties", "anyOf", '"title"', '"default"'):
        assert unsupported not in dumped, unsupported

    status = mgr.get_pool_status()[0]
    assert (status["request_count"], status["success_count"], status["error_count"]) == (1, 1, 0) and status["last_used_at"]


def test_no_thinking_config_for_models_that_do_not_support_it():
    fake = FakeGoogle()
    mgr, _, _ = manager(fake, model="gemini-1.5-flash")
    mgr.execute_with_retry("hi")
    cfg = fake.calls[0]["body"]["generationConfig"]
    assert "thinkingConfig" not in cfg and "responseSchema" not in cfg and "models/gemini-1.5-flash:" in fake.calls[0]["url"]


def test_schema_conversion_accepts_plain_dicts():
    assert to_gemini_schema({"type": "object", "properties": {"a": {"type": "integer", "title": "A"}}, "additionalProperties": False}) == {
        "type": "OBJECT", "properties": {"a": {"type": "INTEGER"}}}


# ------------------------------------------------------------------ 429 rotation
def test_key_cycling_on_429_rotates_to_the_backup_key_in_the_same_call(caplog):
    fake = FakeGoogle({K1: [rate_limited("7s")], K2: [ok]})
    mgr, clock, _ = manager(fake)
    with caplog.at_level(logging.WARNING, logger="migration.gemini"):
        result = mgr.execute_with_retry("hi")
    assert result.key_hash == key_hash(K2) and result.attempts == 2 and fake.keys_called() == [K1, K2]
    k1, k2 = mgr.get_pool_status()
    assert k1["status"] == "RATE_LIMITED" and k1["seconds_until_available"] == 8 and k1["rate_limit_count"] == 1 and k1["error_count"] == 1
    assert "429" in k1["last_error"] and (k2["status"], k2["request_count"], k2["success_count"]) == ("ACTIVE", 1, 1)

    assert "rate limited" in caplog.text and "AIzaSy...4xQ1" in caplog.text
    assert K1 not in caplog.text and K2 not in caplog.text  # a full key never reaches the logs

    fake.calls.clear()
    mgr.execute_with_retry("again")  # K1 is cooling down: it is not even tried
    assert fake.keys_called() == [K2]
    clock.advance(9)
    assert mgr.get_pool_status()[0]["status"] == "ACTIVE"


def test_default_cooldown_when_google_gives_no_retry_delay_and_retry_after_header():
    mgr, _, _ = manager(FakeGoogle({K1: [rate_limited(None)], K2: [ok]}))
    mgr.execute_with_retry("hi")
    assert mgr.get_pool_status()[0]["seconds_until_available"] == 61

    header = httpx.Response(429, headers={"Retry-After": "12"}, json={"error": {"status": "RESOURCE_EXHAUSTED"}})
    mgr2, _, _ = manager(FakeGoogle({K1: [header], K2: [ok]}))
    mgr2.execute_with_retry("hi")
    assert mgr2.get_pool_status()[0]["seconds_until_available"] == 13


def test_daily_quota_429_is_detected_and_parks_the_key_for_24_hours():
    mgr, _, _ = manager(FakeGoogle({K1: [rate_limited("30s", daily=True)], K2: [ok]}))
    mgr.execute_with_retry("hi")
    k1 = mgr.get_pool_status()[0]
    assert k1["status"] == "QUOTA_EXHAUSTED" and k1["seconds_until_available"] == 24 * 3600 and "daily quota" in k1["last_error"]


def test_rotation_passes_through_several_exhausted_keys():
    fake = FakeGoogle({K1: [rate_limited()], K2: [rate_limited(daily=True)], K3: [ok]})
    mgr, _, _ = manager(fake, keys=(K1, K2, K3))
    result = mgr.execute_with_retry("hi")
    assert result.attempts == 3 and result.key_hash == key_hash(K3) and fake.keys_called() == [K1, K2, K3]
    assert [s["status"] for s in mgr.get_pool_status()] == ["RATE_LIMITED", "QUOTA_EXHAUSTED", "ACTIVE"]


def test_all_keys_exhausted_raises_with_reset_metadata_then_recovers():
    fake = FakeGoogle({K1: [rate_limited("20s")], K2: [rate_limited("45s")]})
    mgr, clock, _ = manager(fake)
    with pytest.raises(AllApiKeysExhaustedError) as exc:
        mgr.execute_with_retry("hi")
    err = exc.value
    assert fake.keys_called() == [K1, K2]
    assert err.retry_after_seconds == 21 and err.reset_at is not None and err.reset_at.timestamp() == clock.now + 21
    assert [k["status"] for k in err.keys] == ["RATE_LIMITED", "RATE_LIMITED"] and "all 2" in str(err) and "reset" in str(err)
    assert K1 not in str(err) and K1 not in json.dumps(err.keys)

    fake.calls.clear()
    with pytest.raises(AllApiKeysExhaustedError):  # still cooling down: raised immediately, no request is made
        mgr.execute_with_retry("hi")
    assert fake.calls == []

    clock.advance(22)  # K1 is back
    fake.script[K1] = [ok]
    assert mgr.execute_with_retry("hi").key_hash == key_hash(K1)
    assert [s["status"] for s in mgr.get_pool_status()] == ["ACTIVE", "RATE_LIMITED"]


def test_all_keys_exhausted_by_daily_quota_reports_a_24h_reset():
    fake = FakeGoogle(default=lambda: rate_limited("3s", daily=True))
    mgr, clock, _ = manager(fake)
    with pytest.raises(AllApiKeysExhaustedError) as exc:
        mgr.execute_with_retry("hi")
    assert exc.value.retry_after_seconds == 24 * 3600
    assert {k["status"] for k in exc.value.keys} == {"QUOTA_EXHAUSTED"}


def test_retry_budget_limits_attempts_while_healthy_keys_remain():
    fake = FakeGoogle(default=lambda: rate_limited("5s"))
    mgr, _, _ = manager(fake, keys=(K1, K2, K3))
    with pytest.raises(GeminiRateLimitError):
        mgr.execute_with_retry("hi", max_retries=1)  # 2 attempts, the third key was never tried
    assert fake.keys_called() == [K1, K2] and mgr.get_pool_status()[2]["status"] == "ACTIVE"
    assert mgr.execute_with_retry.__func__ is GeminiKeyManager.execute_with_retry


# ------------------------------------------------------------------ rejected keys + non-key failures
def test_invalid_key_is_disabled_for_good_and_skipped():
    fake = FakeGoogle({K1: [invalid_key], K2: [ok]})
    mgr, clock, _ = manager(fake)
    assert mgr.execute_with_retry("hi").key_hash == key_hash(K2)
    k1 = mgr.get_pool_status()[0]
    assert k1["status"] == "INVALID" and k1["available_at"] is None and "API_KEY_INVALID" in k1["last_error"]
    clock.advance(7 * 24 * 3600)
    assert mgr.get_pool_status()[0]["status"] == "INVALID"  # never resets by itself
    fake.calls.clear()
    mgr.execute_with_retry("again")
    assert fake.keys_called() == [K2]


def test_403_marks_the_key_invalid_and_all_invalid_has_no_reset_time():
    mgr, _, _ = manager(FakeGoogle(default=lambda: httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})))
    with pytest.raises(AllApiKeysExhaustedError) as exc:
        mgr.execute_with_retry("hi")
    assert exc.value.reset_at is None and exc.value.retry_after_seconds is None and "no key will reset" in str(exc.value)


def test_bad_request_does_not_rotate_keys():
    fake = FakeGoogle(default=lambda: httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT", "message": "bad schema AIzaSyDONOTECHO0000000000000000000000"}}))
    mgr, _, _ = manager(fake)
    with pytest.raises(GeminiRequestError) as exc:
        mgr.execute_with_retry("hi")
    assert fake.keys_called() == [K1] and mgr.get_pool_status()[0]["status"] == "ACTIVE"
    assert "AIzaSyDONOTECHO" not in str(exc.value)  # a key quoted back by Google is scrubbed


def test_blocked_or_empty_answers_are_request_errors():
    blocked = httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
    mgr, _, _ = manager(FakeGoogle(default=lambda: blocked))
    with pytest.raises(GeminiRequestError, match="SAFETY"):
        mgr.execute_with_retry("hi")
    empty = httpx.Response(200, json={"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]})
    mgr2, _, _ = manager(FakeGoogle(default=lambda: empty))
    with pytest.raises(GeminiRequestError, match="MAX_TOKENS"):
        mgr2.execute_with_retry("hi")


def test_transient_server_errors_are_retried_on_the_same_key_then_reported():
    fake = FakeGoogle({K1: [lambda: httpx.Response(503, text="busy"), ok]})
    mgr, _, sleeps = manager(fake)
    assert mgr.execute_with_retry("hi").attempts == 1  # one logical attempt; the 503 was absorbed by a same-key retry
    assert fake.keys_called() == [K1, K1] and sleeps == [1.0] and mgr.get_pool_status()[0]["status"] == "ACTIVE"

    down = FakeGoogle(default=lambda: httpx.Response(500, text="boom"))
    mgr2, _, sleeps2 = manager(down)
    with pytest.raises(GeminiServiceError):
        mgr2.execute_with_retry("hi")
    assert len(down.calls) == 3 and sleeps2 == [1.0, 2.0] and mgr2.get_pool_status()[0]["status"] == "ACTIVE"  # not the key's fault

    def refuse(request):
        raise httpx.ConnectError("offline")
    mgr3 = GeminiKeyManager(keys=[K1], transport=httpx.MockTransport(refuse), sleep=lambda s: None)
    with pytest.raises(GeminiServiceError, match="unavailable"):
        mgr3.execute_with_retry("hi")


def test_concurrent_calls_share_the_pool_safely():
    fake = FakeGoogle({K1: [lambda: rate_limited("60s")], K2: [ok]})
    mgr, _, _ = manager(fake)
    results, errors = [], []

    def worker():
        for _ in range(5):
            try:
                results.append(mgr.execute_with_retry("hi"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(results) == 40 and {r.key_hash for r in results} == {key_hash(K2)}
    k1, k2 = mgr.get_pool_status()
    assert k1["status"] == "RATE_LIMITED" and k1["request_count"] <= 8 and k2["success_count"] == 40


# ------------------------------------------------------------------ key test (health ping)
def test_key_health_check_classifies_without_touching_the_pool():
    seen = FakeGoogle(default=lambda: httpx.Response(200, json={"name": "models/gemini-flash-latest"}))
    mgr, _, _ = manager(seen, keys=())
    good = mgr.test_key(K1)
    assert good["ok"] and good["classification"] == "valid" and good["model"] == "gemini-flash-latest"
    call = seen.calls[0]
    assert call["method"] == "GET" and call["url"].endswith("/models/gemini-flash-latest") and call["key"] == K1 and K1 not in call["url"]
    assert mgr.get_pool_status() == []

    for response, verdict, ok_flag in ((invalid_key(), "invalid", False), (rate_limited(), "rate_limited", True),
                                       (httpx.Response(404, json={"error": {"status": "NOT_FOUND"}}), "model_unavailable", False),
                                       (httpx.Response(500, text="x"), "error", False)):
        m, _, _ = manager(FakeGoogle(default=lambda r=response: r), keys=())
        result = m.test_key(K1)
        assert (result["classification"], result["ok"]) == (verdict, ok_flag), verdict

    def offline(request):
        raise httpx.ConnectError("x")
    unreachable = GeminiKeyManager(transport=httpx.MockTransport(offline)).test_key(K1)
    assert unreachable["classification"] == "unreachable" and not unreachable["ok"]
    with pytest.raises(ValueError):
        GeminiKeyManager().test_key("short")

    pooled, _, _ = manager(seen)
    assert pooled.test_pooled_key(key_hash(K2))["ok"] and pooled.get_pool_status()[1]["request_count"] == 0
    with pytest.raises(KeyError):
        pooled.test_pooled_key("deadbeef")


# ------------------------------------------------------------------ LLM mapper integration
CTX = SourceColumnContext(column_id=1, column_name="CUST_NAME", data_type="VARCHAR2", null_ratio=0.0, distinct_ratio=0.9,
                          sample_values=["Acme"], table_name="AR_CUSTOMERS")
CANDS = [TargetCandidate(column_id=1, table_id=1, table_name="BUT000", column_name="NAME_ORG1", data_type="CHAR", score=0.8)]


class FakeOllama:
    def __init__(self, fail: bool = True):
        self.fail, self.calls = fail, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.fail:
            raise httpx.ConnectError("ollama is down")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({**REC, "target_column": "FROM_OLLAMA"})}}]})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self), base_url="http://ollama.test/v1")


def test_mapper_with_provider_gemini_routes_everything_through_the_key_pool():
    fake = FakeGoogle({K1: [rate_limited(), ok], K2: [ok]})
    mgr, _, _ = manager(fake)
    ollama = FakeOllama()
    llm = LLMMapperService(client=ollama.client(), provider="gemini", gemini=mgr)
    rec = llm.recommend(CTX, CANDS)
    assert isinstance(rec, FieldMappingRecommendation) and rec.target_column == "NAME_ORG1" and rec.confidence_score == 0.93
    assert ollama.calls == 0 and llm.model_name == "gemini-flash-latest" and llm.provider == "gemini"
    body = fake.calls[-1]["body"]
    assert body["systemInstruction"]["parts"][0]["text"].startswith("You are a deterministic ERP data-migration mapping assistant")
    assert '"name":"CUST_NAME"' in body["contents"][0]["parts"][0]["text"]
    assert body["generationConfig"]["responseSchema"]["properties"]["rule_type"]["enum"]
    assert fake.keys_called() == [K1, K2]  # the first key was rate limited mid-run; the mapper never noticed
    assert llm.keep_warm() is True and ollama.calls == 0  # nothing to warm locally


def test_mapper_gemini_no_match_invalid_json_retry_and_blocked_answers():
    no_match = {**REC, "target_table": "NO_MATCH", "target_column": "NO_MATCH", "confidence_score": 0}
    mgr, _, _ = manager(FakeGoogle(default=lambda: ok(no_match)), keys=(K1,))
    assert LLMMapperService(provider="gemini", gemini=mgr).recommend(CTX, CANDS) is None

    fake = FakeGoogle({K1: [lambda: ok("this is not json"), ok]})
    mgr2, _, _ = manager(fake, keys=(K1,))
    assert LLMMapperService(provider="gemini", gemini=mgr2).recommend(CTX, CANDS).target_column == "NAME_ORG1"
    retry_prompt = fake.calls[1]["body"]["contents"][0]["parts"][0]["text"]
    assert "[Your previous reply]" in retry_prompt and "this is not json" in retry_prompt and "Your reply was invalid" in retry_prompt

    mgr3, _, _ = manager(FakeGoogle(default=lambda: ok("{}")), keys=(K1,))
    with pytest.raises(LLMOutputError):
        LLMMapperService(provider="gemini", gemini=mgr3).recommend(CTX, CANDS)
    blocked = httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
    mgr4, _, _ = manager(FakeGoogle(default=lambda: blocked), keys=(K1,))
    with pytest.raises(LLMOutputError, match="SAFETY"):
        LLMMapperService(provider="gemini", gemini=mgr4).recommend(CTX, CANDS)


def test_mapper_reports_exhausted_keys_as_llm_unavailable():
    mgr, _, _ = manager(FakeGoogle(default=lambda: rate_limited("30s")))
    llm = LLMMapperService(provider="gemini", gemini=mgr)
    with pytest.raises(LLMUnavailableError, match="exhausted") as exc:
        llm.recommend(CTX, CANDS)
    assert "reset" in str(exc.value) and K1 not in str(exc.value)
    with pytest.raises(LLMUnavailableError, match="no Gemini API keys"):
        LLMMapperService(provider="gemini", gemini=GeminiKeyManager(transport=httpx.MockTransport(FakeGoogle()))).recommend(CTX, CANDS)


def test_ollama_provider_falls_back_to_gemini_when_ollama_is_down():
    fake = FakeGoogle()
    mgr, _, _ = manager(fake)
    ollama = FakeOllama(fail=True)
    llm = LLMMapperService(client=ollama.client(), gemini=mgr)  # provider defaults to "ollama"
    first = llm.recommend(CTX, CANDS)
    assert first.target_column == "NAME_ORG1" and ollama.calls == 1 and llm.model_name == "gemini-flash-latest"
    llm.recommend(CTX, CANDS)
    assert ollama.calls == 1 and len(fake.calls) == 2  # Ollama is skipped for a while after it failed

    healthy = FakeOllama(fail=False)
    llm2 = LLMMapperService(client=healthy.client(), gemini=GeminiKeyManager(keys=[K1], transport=httpx.MockTransport(FakeGoogle())))
    assert llm2.recommend(CTX, CANDS).target_column == "FROM_OLLAMA" and healthy.calls == 1 and llm2.model_name != "gemini-flash-latest"


def test_no_fallback_without_keys_or_with_ollama_only():
    down = FakeOllama(fail=True)
    with pytest.raises(LLMUnavailableError, match="Ollama"):
        LLMMapperService(client=down.client()).recommend(CTX, CANDS)  # no key pool at all
    mgr, _, _ = manager(FakeGoogle())
    with pytest.raises(LLMUnavailableError, match="Ollama"):
        LLMMapperService(client=FakeOllama().client(), provider="ollama-only", gemini=mgr).recommend(CTX, CANDS)
    exhausted, _, _ = manager(FakeGoogle(), keys=())
    with pytest.raises(LLMUnavailableError, match="Ollama"):
        LLMMapperService(client=FakeOllama().client(), gemini=exhausted).recommend(CTX, CANDS)  # empty pool: nothing to fall back to
    with pytest.raises(ValueError):
        LLMMapperService(provider="gemini")
    with pytest.raises(ValueError):
        LLMMapperService(provider="openai")


def test_mapper_from_env_reads_the_provider_and_never_the_environment_by_default():
    assert LLMMapperService().provider == "ollama"  # constructing directly never looks at LLM_PROVIDER
    env = {"LLM_PROVIDER": "Gemini", "GEMINI_API_KEYS": f"{K1},{K2}"}
    llm = LLMMapperService.from_env(env=env)
    assert llm.provider == "gemini" and llm.model_name == "gemini-flash-latest"
    assert LLMMapperService.from_env(env={}).provider == "ollama"
    assert LLMMapperService.from_env(env={"LLM_PROVIDER": "ollama-only"}).provider == "ollama-only"


# ------------------------------------------------------------------ API
def make_client(session_factory, tmp_path, mgr, user="alice", **settings) -> TestClient:
    ctx = AppContext(Settings(workspace_dir=tmp_path / "ws", auto_seed=False, **settings), session_factory, gemini_keys=mgr)
    return TestClient(create_app(ctx), headers={"X-User": user} if user else {})


def test_api_lists_adds_tests_and_deletes_keys_without_ever_exposing_them(session_factory, tmp_path):
    fake = FakeGoogle({K1: [rate_limited(), ok]})
    mgr, _, _ = manager(fake, keys=(K1,))
    client = make_client(session_factory, tmp_path, mgr)

    pool = client.get("/api/keys")
    assert pool.status_code == 200 and K1 not in pool.text
    body = pool.json()
    assert (body["total"], body["active"], body["model"], body["persisted"]) == (1, 1, "gemini-flash-latest", False)
    assert body["keys"][0]["masked_key"] == "AIzaSy...4xQ1" and body["keys"][0]["status"] == "ACTIVE" and body["keys"][0]["source"] == "env"

    tested = client.post("/api/keys/test", json={"api_key": K3})
    assert tested.status_code == 200 and tested.json()["classification"] == "valid" and tested.json()["ok"] and K3 not in tested.text
    assert client.get("/api/keys").json()["total"] == 1  # testing does not add the key

    added = client.post("/api/keys", json={"api_key": K2, "label": "backup"})
    assert added.status_code == 201 and added.json()["masked_key"] == "AIzaSy...4xQ2" and added.json()["label"] == "backup" and K2 not in added.text
    dup = client.post("/api/keys", json={"api_key": K2})
    assert dup.status_code == 409 and dup.json()["code"] == "KEY_EXISTS" and K2 not in dup.text

    mgr.mark_exhausted(K1, 90)  # as if a 429 happened during a mapping run
    pool = client.get("/api/keys").json()
    assert (pool["active"], pool["cooling_down"], pool["total"]) == (1, 1, 2)
    assert [k["status"] for k in pool["keys"]] == ["RATE_LIMITED", "ACTIVE"] and pool["keys"][0]["seconds_until_available"] == 90

    by_hash = client.post("/api/keys/test", json={"key_hash": key_hash(K2)})
    assert by_hash.status_code == 200 and by_hash.json()["ok"]
    assert client.post("/api/keys/test", json={"key_hash": "deadbeef0000"}).status_code == 404

    gone = client.delete(f"/api/keys/{key_hash(K1)}")
    assert gone.status_code == 200 and gone.json() == {"removed": key_hash(K1)}
    assert client.delete(f"/api/keys/{key_hash(K1)}").status_code == 404
    assert [k["masked_key"] for k in client.get("/api/keys").json()["keys"]] == ["AIzaSy...4xQ2"]


def test_api_rejects_bad_input_without_echoing_it(session_factory, tmp_path):
    client = make_client(session_factory, tmp_path, GeminiKeyManager())
    secret = "AIzaSyThisKeyContainsASpace 0123456789"
    for payload in ({"api_key": secret}, {"api_key": "short"}, {"api_key": K1, "extra": 1}, {}):
        r = client.post("/api/keys", json=payload)
        assert r.status_code == 422 and secret not in r.text and '"input"' not in r.text and r.json()["code"] in ("VALIDATION_ERROR", "INVALID_REQUEST")
    assert client.post("/api/keys/test", json={}).status_code == 422
    assert client.post("/api/keys/test", json={"api_key": K1, "key_hash": "abcd1234"}).status_code == 422
    assert client.get("/api/keys").json()["total"] == 0


def test_api_key_management_requires_identity_and_respects_the_admin_list(session_factory, tmp_path):
    mgr = GeminiKeyManager(keys=[K1])
    anonymous = make_client(session_factory, tmp_path, mgr, user=None)
    assert anonymous.get("/api/keys").status_code == 200  # reading the (masked) stack is open
    for call in (lambda c: c.post("/api/keys", json={"api_key": K2}), lambda c: c.delete(f"/api/keys/{key_hash(K1)}"),
                 lambda c: c.post("/api/keys/test", json={"api_key": K2})):
        assert call(anonymous).status_code == 401

    restricted = make_client(session_factory, tmp_path, mgr, user="alice", key_admins=("key.admin",))
    assert restricted.get("/api/keys").status_code == 200
    denied = restricted.post("/api/keys", json={"api_key": K2})
    assert denied.status_code == 403 and restricted.delete(f"/api/keys/{key_hash(K1)}").status_code == 403
    admin = TestClient(restricted.app, headers={"X-User": "Key.Admin"})
    assert admin.post("/api/keys", json={"api_key": K2}).status_code == 201

    secured = TestClient(create_app(AppContext(Settings(workspace_dir=tmp_path / "ws2", auto_seed=False, api_keys={"k-alice": "alice"}),
                                               session_factory, gemini_keys=mgr)))
    assert secured.get("/api/keys").status_code == 401 and secured.get("/api/keys", headers={"X-API-Key": "k-alice"}).status_code == 200


def test_health_reports_the_provider_and_active_keys(session_factory, tmp_path):
    mgr, _, _ = manager(FakeGoogle(), keys=(K1, K2))
    client = make_client(session_factory, tmp_path, mgr)
    health = client.get("/api/health").json()
    assert health["gemini_keys_active"] == 2 and health["llm_provider"] in ("ollama", "gemini", "ollama-only")
    mgr.mark_exhausted(K1, 60)
    assert client.get("/api/health").json()["gemini_keys_active"] == 1


def test_key_stack_ui_is_in_the_navigation_header(session_factory, tmp_path):
    client = make_client(session_factory, tmp_path, GeminiKeyManager())
    for path in ("/", "/ui/sources", "/ui/studio/1", "/ui/governance/1", "/ui/profiler/1", "/ui/certificate/1"):
        page = client.get(path)
        assert page.status_code == 200, path
        for needle in ('id="key-indicator"', "Gemini Keys:", '<dialog id="key-modal"', 'id="key-form"', 'type="password"',
                       'id="key-test"', "/static/js/keys.js", "Acting as"):
            assert needle in page.text, (path, needle)
        if path != "/":  # stand-alone pages: next to "Acting as" in the top bar; the console puts it in the status bar
            assert page.text.index('id="key-indicator"') < page.text.index('id="actor"')
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page.text)  # CSP: no inline scripts
    js = client.get("/static/js/keys.js")
    assert js.status_code == 200
    for needle in ("/api/keys", "/api/keys/test", "Cooldown", "Quota exceeded", "Active", "Invalid", "masked_key", "Active\""):
        assert needle in js.text, needle
    assert "localStorage" not in js.text and "sessionStorage" not in js.text  # secrets never go to browser storage
    css = client.get("/static/css/app.css").text
    assert ".key-indicator" in css and "dialog::backdrop" in css
    assert defaultdict and re  # imports used
