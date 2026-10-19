"""Background AI-mapping jobs: progress, polling, cancel, failure, plus model keep-warm."""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from src.services.llm_mapper_service import KEEP_ALIVE, LLMMapperService
from src.services.metadata_service import NotFoundError
from tests.test_module7_api import (  # noqa: F401  (fixtures + helpers)
    SCRIPT,
    csv_path,
    dataset,
    encoder,
    env,
    make_client,
    make_ctx,
    subset,
    upload,
)

COLUMNS = 10


class GatedOllama:
    """httpx handler: records warm-up and chat calls; chat calls can be held open with `gate`."""

    def __init__(self, fail: bool = False, warm_status: int = 200):
        self.gate = threading.Event()
        self.gate.set()
        self.in_flight = threading.Event()
        self.fail, self.warm_status = fail, warm_status
        self.warm_calls: list[dict] = []
        self.columns: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/generate":  # native warm-up call
            self.warm_calls.append(json.loads(request.content))
            return httpx.Response(self.warm_status, json={"done": True})
        if self.fail:
            raise httpx.ConnectError("ollama is down")
        body = json.loads(request.content)
        user = next(m["content"] for m in body["messages"] if m["role"] == "user" and m["content"].startswith("{"))
        column = json.loads(user)["source_column"]["name"]
        self.columns.append(column)
        self.in_flight.set()
        assert self.gate.wait(timeout=60), "test never released the model"
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(SCRIPT[column])}}]})

    def llm(self) -> LLMMapperService:
        return LLMMapperService(client=httpx.Client(transport=httpx.MockTransport(self), base_url="http://ollama.test/v1"))


def wait_for(client, job_id, done=("succeeded", "failed", "cancelled"), timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/mapping/jobs/{job_id}").json()
        if job["status"] in done:
            return job
        time.sleep(0.2)
    raise AssertionError(f"job did not finish: {job}")


@pytest.fixture()
def profiled(service, session_factory, tmp_path, encoder, dataset):
    def build(model: GatedOllama):
        client = make_client(make_ctx(session_factory, tmp_path, encoder, llm=model.llm()))
        client.post("/api/admin/seed-sap-catalog")
        table_id = upload(client, subset(dataset)).json()["table_id"]
        assert client.post(f"/api/profile/{table_id}").status_code == 200
        return client, table_id
    return build


def test_background_job_reports_per_column_progress_and_result(profiled):
    model = GatedOllama()
    model.gate.clear()
    client, table_id = profiled(model)

    queued = client.post(f"/api/mapping/generate/{table_id}?background=true")
    assert queued.status_code == 202
    job = queued.json()
    assert job["status"] in ("queued", "running") and job["already_running"] is False and job["kind"] == "ai-mapping"

    assert model.in_flight.wait(timeout=60)  # the model is evaluating the first column right now
    running = client.get(f"/api/mapping/jobs/{job['job_id']}").json()
    assert running["status"] == "running" and running["result"] is None
    assert running["progress"]["total"] == COLUMNS and running["progress"]["current"] == 0
    assert running["progress"]["column"] == model.columns[0]
    assert running["progress"]["message"] == f"Evaluating column 1 of {COLUMNS}: {model.columns[0]}"

    model.gate.set()
    done = wait_for(client, job["job_id"])
    assert done["status"] == "succeeded" and done["error"] is None and done["finished_at"]
    assert done["progress"]["current"] == COLUMNS and done["progress"]["message"] == "Done"
    result = done["result"]
    assert result["mapping"]["mapping"]["status"] == "DRAFT" and result["mapping"]["mapping"]["rule_count"] == 7
    assert client.get(f"/api/mapping/{result['mapping_set_id']}").json()["mapping"]["rule_count"] == 7
    assert [j["job_id"] for j in client.get(f"/api/mapping/jobs?source_table_id={table_id}").json()] == [job["job_id"]]
    assert client.get("/api/mapping/jobs?source_table_id=999").json() == []


def test_model_is_warmed_with_keep_alive_before_the_first_column(profiled):
    model = GatedOllama()
    client, table_id = profiled(model)
    job = client.post(f"/api/mapping/generate/{table_id}?background=true").json()
    wait_for(client, job["job_id"])
    assert model.warm_calls == [{"model": "qwen2.5-coder:7b", "prompt": "", "stream": False, "keep_alive": KEEP_ALIVE}]
    assert KEEP_ALIVE == "60m"


def test_a_failed_warmup_does_not_fail_the_job(profiled):
    model = GatedOllama(warm_status=500)
    client, table_id = profiled(model)
    job = client.post(f"/api/mapping/generate/{table_id}?background=true").json()
    assert wait_for(client, job["job_id"])["status"] == "succeeded"


def test_second_request_follows_the_running_job(profiled):
    model = GatedOllama()
    model.gate.clear()
    client, table_id = profiled(model)
    first = client.post(f"/api/mapping/generate/{table_id}?background=true").json()
    assert model.in_flight.wait(timeout=60)
    second = client.post(f"/api/mapping/generate/{table_id}?background=true")
    assert second.status_code == 200 and second.json()["job_id"] == first["job_id"] and second.json()["already_running"]
    model.gate.set()
    assert wait_for(client, first["job_id"])["status"] == "succeeded"
    assert len(client.get("/api/mapping-sets").json()) == 1  # one draft, not two


def test_cancel_stops_the_job_and_saves_nothing(profiled):
    model = GatedOllama()
    model.gate.clear()
    client, table_id = profiled(model)
    job = client.post(f"/api/mapping/generate/{table_id}?background=true").json()
    assert model.in_flight.wait(timeout=60)

    cancelling = client.post(f"/api/mapping/jobs/{job['job_id']}/cancel")
    assert cancelling.status_code == 200 and cancelling.json()["status"] == "cancelling"
    model.gate.set()  # the in-flight column finishes, then the job stops at the next checkpoint
    done = wait_for(client, job["job_id"])
    assert done["status"] == "cancelled" and done["result"] is None and "nothing was saved" in done["progress"]["message"]
    assert len(model.columns) < COLUMNS  # it did not keep going
    assert client.get("/api/mapping-sets").json() == []

    again = client.post(f"/api/mapping/generate/{table_id}?background=true")  # a new job is allowed afterwards
    assert again.status_code == 202 and again.json()["job_id"] != job["job_id"]
    wait_for(client, again.json()["job_id"])


def test_llm_outage_fails_the_job_with_a_structured_error(profiled):
    client, table_id = profiled(GatedOllama(fail=True))
    job = client.post(f"/api/mapping/generate/{table_id}?background=true").json()
    done = wait_for(client, job["job_id"])
    assert done["status"] == "failed" and done["error"]["code"] == "LLM_UNAVAILABLE" and "Ollama" in done["error"]["detail"]
    assert done["result"] is None and client.get("/api/mapping-sets").json() == []


def test_job_endpoints_validate_input(profiled):
    client, _ = profiled(GatedOllama())
    assert client.get("/api/mapping/jobs/nope").status_code == 404
    assert client.post("/api/mapping/jobs/nope/cancel").status_code == 404
    assert client.post("/api/mapping/generate/999999?background=true").status_code == 404  # fails fast, nothing queued
    assert client.get("/api/mapping/jobs").json() == []
    anonymous = client.post("/api/mapping/generate/1?background=true", headers={"X-User": ""})
    assert anonymous.status_code == 401


def test_progress_callback_sequence_and_abort(profiled, encoder, session_factory):
    model = GatedOllama()
    client, table_id = profiled(model)
    orchestrator = client.app.state.ctx.mapping_orchestrator()

    events: list[tuple] = []
    orchestrator.generate_table_mapping_report(table_id, progress=lambda *e: events.append(e))
    assert events[0] == ("start", 0, COLUMNS, None) and events[-1] == ("persist", COLUMNS, COLUMNS, None)
    assert [e[1] for e in events if e[0] == "column"] == list(range(1, COLUMNS + 1))
    assert all(e[3] for e in events if e[0] == "column")

    before = len(client.get("/api/mapping-sets").json())

    def abort(event, position, total, column):
        if position == 3:
            raise RuntimeError("stop")

    with pytest.raises(RuntimeError):
        orchestrator.generate_table_mapping_report(table_id, progress=abort)
    assert len(client.get("/api/mapping-sets").json()) == before  # aborted runs persist nothing


# ------------------------------------------------------------------ keep_warm unit behaviour
def llm_with(handler) -> LLMMapperService:
    return LLMMapperService(client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://ollama.test/v1"))


def test_keep_warm_uses_the_native_api_and_never_raises():
    seen = []

    def ok(request):
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={})

    assert llm_with(ok).keep_warm() is True
    url, body = seen[0]
    assert url == "http://ollama.test/api/generate"  # not /v1: keep_alive is ignored there
    assert body["keep_alive"] == "60m" and body["prompt"] == "" and body["model"] == "qwen2.5-coder:7b"

    assert llm_with(lambda r: httpx.Response(404)).keep_warm() is False
    def refuse(request):
        raise httpx.ConnectError("down")
    assert llm_with(refuse).keep_warm() is False
    def explode(request):
        raise KeyError("handler bug")
    assert llm_with(explode).keep_warm() is False  # best effort, whatever goes wrong
