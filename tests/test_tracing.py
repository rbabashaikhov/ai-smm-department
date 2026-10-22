"""SMM-035C: Langfuse tracing is on only when deliberately configured.

The generation worker's compose file sets the Langfuse keys to "" to mean
"no tracing", but the SDK took "" for a key and exported spans to
cloud.langfuse.com (401). The unit tests replace the SDK's client, handler
and decorator; the end-to-end ones run the real SDK -- and the two legacy
scripts, with their graph and knowledge base replaced -- in a fresh
interpreter with every name lookup and connection recorded, and the only
server they reach is one this test starts on 127.0.0.1.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import json
import logging
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

import ai_smm.tracing as tracing


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

PUBLIC_KEY = "pk-lf-test-0000-not-a-real-key"
SECRET_KEY = "sk-lf-test-0000-not-a-real-key"
CONFIGURED = {
    "LANGFUSE_PUBLIC_KEY": PUBLIC_KEY,
    "LANGFUSE_SECRET_KEY": SECRET_KEY,
    "LANGFUSE_BASE_URL": "http://127.0.0.1:9",
}


@pytest.fixture(autouse=True)
def fresh_decision(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name in (*tracing.KEY_ENV, *tracing.ENDPOINT_ENV, tracing.FLAG_ENV):
        monkeypatch.delenv(name, raising=False)

    tracing._handler.cache_clear()
    yield
    tracing._handler.cache_clear()


# --- the decision -----------------------------------------------------------


@pytest.mark.parametrize(
    "environ",
    [
        {},
        # compose.generation.yml before SMM-035C: the 401 case.
        {"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""},
        {"LANGFUSE_PUBLIC_KEY": " ", "LANGFUSE_SECRET_KEY": "\t"},
        {**CONFIGURED, "LANGFUSE_TRACING_ENABLED": "false"},
        {**CONFIGURED, "LANGFUSE_TRACING_ENABLED": "FALSE"},
        {
            "LANGFUSE_PUBLIC_KEY": PUBLIC_KEY,
            "LANGFUSE_SECRET_KEY": SECRET_KEY,
            "LANGFUSE_HOST": "http://127.0.0.1:9",
            "LANGFUSE_TRACING_ENABLED": "false",
        },
    ],
)
def test_off_without_a_word_when_not_configured_or_turned_off(
    environ: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="ai_smm.tracing"):
        assert tracing.tracing_config(environ) is None

    assert caplog.records == []


@pytest.mark.parametrize("flag", [None, "true", "True"])
def test_on_with_both_keys_and_the_base_url(flag: str | None) -> None:
    environ = dict(CONFIGURED)

    if flag is not None:
        environ["LANGFUSE_TRACING_ENABLED"] = flag

    assert tracing.tracing_config(environ) == {
        "public_key": PUBLIC_KEY,
        "secret_key": SECRET_KEY,
        "base_url": "http://127.0.0.1:9",
    }


KEYS = {"LANGFUSE_PUBLIC_KEY": PUBLIC_KEY, "LANGFUSE_SECRET_KEY": SECRET_KEY}
NO_ENDPOINT = "LANGFUSE_BASE_URL or LANGFUSE_HOST"


@pytest.mark.parametrize(
    ("endpoints", "expected"),
    [
        # LANGFUSE_BASE_URL first, then the SDK's older LANGFUSE_HOST.
        (
            {"LANGFUSE_BASE_URL": "http://base:1", "LANGFUSE_HOST": "http://host:2"},
            "http://base:1",
        ),
        ({"LANGFUSE_HOST": "http://host:2"}, "http://host:2"),
        ({"LANGFUSE_BASE_URL": "", "LANGFUSE_HOST": "http://host:2"}, "http://host:2"),
        ({"LANGFUSE_BASE_URL": " ", "LANGFUSE_HOST": " http://host:2 "}, "http://host:2"),
    ],
)
def test_the_endpoint_is_the_base_url_else_the_host(
    endpoints: dict[str, str], expected: str
) -> None:
    config = tracing.tracing_config({**KEYS, **endpoints})

    assert config is not None
    assert config["base_url"] == expected


@pytest.mark.parametrize(
    ("environ", "missing"),
    [
        # Keys but no endpoint: the SDK would fall back to cloud.langfuse.com.
        (KEYS, NO_ENDPOINT),
        ({**KEYS, "LANGFUSE_BASE_URL": "", "LANGFUSE_HOST": ""}, NO_ENDPOINT),
        ({**KEYS, "LANGFUSE_BASE_URL": " ", "LANGFUSE_HOST": "\t"}, NO_ENDPOINT),
        ({**CONFIGURED, "LANGFUSE_SECRET_KEY": ""}, "LANGFUSE_SECRET_KEY"),
        ({**CONFIGURED, "LANGFUSE_PUBLIC_KEY": "  "}, "LANGFUSE_PUBLIC_KEY"),
        (
            {"LANGFUSE_SECRET_KEY": SECRET_KEY, "LANGFUSE_HOST": "http://host:2"},
            "LANGFUSE_PUBLIC_KEY",
        ),
        (
            {"LANGFUSE_PUBLIC_KEY": PUBLIC_KEY},
            f"LANGFUSE_SECRET_KEY, {NO_ENDPOINT}",
        ),
        (
            {"LANGFUSE_TRACING_ENABLED": "true"},
            f"LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, {NO_ENDPOINT}",
        ),
    ],
)
def test_a_partial_configuration_is_off_and_reported_by_name_only(
    environ: dict[str, str], missing: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="ai_smm.tracing"):
        assert tracing.tracing_config(environ) is None

    assert [r.getMessage() for r in caplog.records] == [
        f"Langfuse tracing is off: {missing} not set"
    ]
    assert PUBLIC_KEY not in caplog.text
    assert SECRET_KEY not in caplog.text


@pytest.mark.parametrize("flag", ["1", "yes", "on", "no", "0"])
def test_any_other_flag_value_is_off_and_reported(
    flag: str, caplog: pytest.LogCaptureFixture
) -> None:
    environ = {**CONFIGURED, "LANGFUSE_TRACING_ENABLED": flag}

    with caplog.at_level(logging.WARNING, logger="ai_smm.tracing"):
        assert tracing.tracing_config(environ) is None

    assert "LANGFUSE_TRACING_ENABLED must be 'true' or 'false'" in caplog.text


# --- initialisation, with the SDK replaced ----------------------------------


class FakeSDK:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import langfuse
        import langfuse.langchain

        self.clients: list[dict[str, Any]] = []
        self.handlers: list[object] = []
        self.traced: list[str] = []
        self.flushed = 0
        self.propagated: list[dict[str, Any]] = []

        def client(**kwargs: Any) -> object:
            self.clients.append(kwargs)
            return object()

        def handler() -> object:
            self.handlers.append(object())
            return self.handlers[-1]

        def observe(*, name: str, as_type: str) -> Any:
            def decorate(func: Any) -> Any:
                def wrapper(*args: Any, **kwargs: Any) -> Any:
                    self.traced.append(name)
                    return func(*args, **kwargs)

                return wrapper

            return decorate

        sdk = self

        class Client:
            def flush(self) -> None:
                sdk.flushed += 1

        def get_client() -> Client:
            if not self.clients:
                raise AssertionError("get_client() would build a client from env")
            return Client()

        @contextlib.contextmanager
        def propagate_attributes(**attributes: Any) -> Any:
            self.propagated.append(attributes)
            yield

        monkeypatch.setattr(langfuse, "get_client", get_client)
        monkeypatch.setattr(langfuse, "propagate_attributes", propagate_attributes)
        monkeypatch.setattr(langfuse, "Langfuse", client)
        monkeypatch.setattr(langfuse.langchain, "CallbackHandler", handler)
        monkeypatch.setattr(langfuse, "observe", observe)


def test_off_creates_no_client_no_handler_and_calls_the_plain_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = FakeSDK(monkeypatch)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")

    @tracing.observe(name="node", as_type="agent")
    def node(x: int) -> int:
        return x + 1

    assert node(1) == 2
    assert tracing.langchain_callbacks() == []
    assert tracing.tracing_enabled() is False

    with tracing.propagate_attributes(trace_name="t", tags=["a"]):
        pass

    tracing.flush()

    assert (sdk.clients, sdk.handlers, sdk.traced) == ([], [], [])
    assert (sdk.flushed, sdk.propagated) == (0, [])


def test_on_builds_one_explicit_client_and_one_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = FakeSDK(monkeypatch)

    for name, value in CONFIGURED.items():
        monkeypatch.setenv(name, value)

    @tracing.observe(name="node", as_type="agent")
    def node(x: int) -> int:
        return x + 1

    assert node(1) == 2
    assert node(2) == 3
    first = tracing.langchain_callbacks()

    assert first == sdk.handlers and len(first) == 1
    assert tracing.langchain_callbacks() == first
    assert sdk.clients == [
        {
            "public_key": PUBLIC_KEY,
            "secret_key": SECRET_KEY,
            "base_url": "http://127.0.0.1:9",
        }
    ]
    assert sdk.traced == ["node", "node"]

    with tracing.propagate_attributes(trace_name="t", tags=["a"]):
        pass

    tracing.flush()

    assert sdk.propagated == [{"trace_name": "t", "tags": ["a"]}]
    assert sdk.flushed == 1
    assert len(sdk.clients) == 1


def test_the_decision_waits_for_the_first_call_not_the_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agents are imported before an entry point loads its .env."""

    sdk = FakeSDK(monkeypatch)

    @tracing.observe(name="node", as_type="agent")
    def node() -> None:
        return None

    for name, value in CONFIGURED.items():
        monkeypatch.setenv(name, value)

    node()

    assert len(sdk.clients) == 1
    assert sdk.traced == ["node"]


def test_no_agent_or_script_uses_langfuse_directly() -> None:
    paths = [
        *sorted((SRC / "ai_smm" / "agents").glob("*.py")),
        *sorted((ROOT / "scripts").glob("*.py")),
    ]

    for path in paths:
        if path.name == "test_langfuse.py":
            continue  # the manual connectivity check, nothing else

        text = path.read_text(encoding="utf-8")

        assert "from langfuse" not in text, path.name
        assert "import langfuse" not in text, path.name
        assert "CallbackHandler" not in text, path.name


# --- end to end: the real SDK, in a fresh interpreter -----------------------

#: Every name lookup and connection is recorded; only 127.0.0.1 is allowed.
_GUARD = """
import json, os, socket, sys

attempts = []
real_getaddrinfo, real_connect = socket.getaddrinfo, socket.socket.connect

def getaddrinfo(host, *args, **kwargs):
    attempts.append(["lookup", str(host)])
    if host not in ("127.0.0.1", "localhost"):
        raise OSError("outbound lookup refused by the test")
    return real_getaddrinfo(host, *args, **kwargs)

def connect(self, address):
    attempts.append(["connect", str(address[0])])
    if address[0] != "127.0.0.1":
        raise OSError("outbound connection refused by the test")
    return real_connect(self, address)

socket.getaddrinfo = getaddrinfo
socket.socket.connect = connect
"""

_REPORT = """
from langfuse._client.resource_manager import LangfuseResourceManager
from opentelemetry import trace

report = {
    "instances": list(LangfuseResourceManager._instances),
    "attempts": attempts,
    "provider": type(trace.get_tracer_provider()).__name__,
    **extra,
}
print(json.dumps(report))
sys.stdout.flush()
# No atexit export: only what was flushed before this line reaches a server.
os._exit(0)
"""

_SCRIPT = (
    _GUARD
    + """
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from ai_smm.tracing import flush, langchain_callbacks, observe

@observe(name="node", as_type="agent")
def node():
    model = FakeListChatModel(responses=["ok"])
    return model.invoke("hi", config={"callbacks": langchain_callbacks()}).content

assert node() == "ok"
flush()
extra = {}
"""
    + _REPORT
)

KB_MARKER = "KB-MARKER-7f3e"
PROMPT_MARKER = "PROMPT-MARKER-91ac"

#: The legacy scripts with their graph and knowledge base replaced: one
#: traced node calls a fake chat model (with token usage) through the
#: callbacks, so a trace has the scripts' root, a node and a generation.
_ENTRYPOINT = (
    _GUARD
    + f"""
import runpy, types

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from ai_smm.tracing import langchain_callbacks, observe

states, loaded = [], []

@observe(name="strategist", as_type="agent")
def strategist(state):
    model = GenericFakeChatModel(messages=iter([AIMessage(
        content="plan",
        usage_metadata={{"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}},
    )]))
    prompt = "{PROMPT_MARKER} " + state["knowledge"]
    return model.invoke(prompt, config={{"callbacks": langchain_callbacks()}}).content

class Graph:
    def invoke(self, state):
        states.append(dict(state))
        plan = strategist(state)
        return {{
            "content_plan": plan,
            "draft": {{"posts": ["d"]}},
            "editor_approved": True,
            "editor_score": 9.0,
            "editor_feedback": "ok",
            "revision_count": 0,
            "publication": {{"status": "fake", "live": state["publish_live"]}},
        }}

graph = types.ModuleType("ai_smm.graph")
graph.build_graph = Graph
sys.modules["ai_smm.graph"] = graph

import ai_smm.knowledge.loader as loader

def load_project_knowledge(project_id):
    loaded.append(project_id)
    return "{KB_MARKER} " + project_id

loader.load_project_knowledge = load_project_knowledge

runpy.run_path(sys.argv[1], run_name="__main__")
extra = {{"states": states, "loaded": loaded}}
"""
    + _REPORT
)


def _run(
    environ: dict[str, str], code: str = _SCRIPT, *args: str
) -> tuple[dict[str, Any], str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LANGFUSE_", "OTEL_"))
        and key.lower() not in ("http_proxy", "https_proxy", "all_proxy")
    }
    env.update(
        {
            "PYTHONPATH": str(SRC),
            "PYTHON_DOTENV_DISABLED": "1",
            "OPENAI_API_KEY": "test-not-a-real-key",
            # No background export: spans leave only on an explicit flush.
            "LANGFUSE_FLUSH_INTERVAL": "60",
            **environ,
        }
    )

    proc = subprocess.run(
        [sys.executable, "-c", code, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr

    lines = proc.stdout.strip().splitlines()

    return json.loads(lines[-1]), "\n".join(lines[:-1])


#: Off, each in its own way; the first two are exactly the generation
#: worker's compose literals, before and after SMM-035C.
OFF = [
    {"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""},
    {
        "LANGFUSE_PUBLIC_KEY": "",
        "LANGFUSE_SECRET_KEY": "",
        "LANGFUSE_TRACING_ENABLED": "false",
    },
    KEYS,
    {**KEYS, "LANGFUSE_BASE_URL": "", "LANGFUSE_HOST": ""},
    {**CONFIGURED, "LANGFUSE_TRACING_ENABLED": "false"},
    {},
]
NOTHING_SENT = {"instances": [], "attempts": [], "provider": "ProxyTracerProvider"}


@pytest.mark.parametrize("environ", OFF)
def test_off_means_no_client_and_no_outbound_request(
    environ: dict[str, str],
) -> None:
    result, _ = _run(environ)

    assert result == NOTHING_SENT


class _Recorder(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, str]] = []
    spans: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 -- http.server's name
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))

        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)

        type(self).requests.append(
            (self.command, self.path, self.headers.get("Authorization", ""))
        )
        export = ExportTraceServiceRequest()
        export.ParseFromString(body)

        for resource in export.resource_spans:
            for scope in resource.scope_spans:
                for span in scope.spans:
                    type(self).spans.append(
                        {
                            "name": span.name,
                            "id": span.span_id.hex(),
                            "parent": span.parent_span_id.hex(),
                            "trace": span.trace_id.hex(),
                            "attributes": {
                                a.key: a.value.string_value for a in span.attributes
                            },
                        }
                    )

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: Any) -> None:
        return None


@contextlib.contextmanager
def _langfuse_stand_in() -> Any:
    """A local server that records what an OTLP exporter sends it."""

    _Recorder.requests, _Recorder.spans = [], []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


EXPECTED_AUTH = (
    "Basic " + base64.b64encode(f"{PUBLIC_KEY}:{SECRET_KEY}".encode()).decode()
)


@pytest.mark.parametrize("endpoint_env", ["LANGFUSE_BASE_URL", "LANGFUSE_HOST"])
def test_on_exports_only_to_the_configured_endpoint_with_its_keys(
    endpoint_env: str,
) -> None:
    with _langfuse_stand_in() as url:
        result, _ = _run({**KEYS, endpoint_env: url})

    assert result["instances"] == [PUBLIC_KEY]
    assert {host for _, host in result["attempts"]} == {"127.0.0.1"}
    assert _Recorder.requests
    assert set(_Recorder.requests) == {
        ("POST", "/api/public/otel/v1/traces", EXPECTED_AUTH)
    }


# --- the legacy entry points: scripts/run_demo.py, scripts/publish_live.py ---

ENTRYPOINTS = {
    "run_demo.py": ("ai-smm-pipeline", False, "=== PUBLICATION ==="),
    "publish_live.py": ("ai-smm-live-publication", True, "=== LIVE PUBLICATION ==="),
}


def _expected_state(publish_live: bool) -> dict[str, Any]:
    return {
        "project_id": "ai-catalog-consultant",
        "knowledge": f"{KB_MARKER} ai-catalog-consultant",
        "revision_count": 0,
        "max_revisions": 3,
        "errors": [],
        "publish_live": publish_live,
    }


@pytest.mark.parametrize("script", sorted(ENTRYPOINTS))
@pytest.mark.parametrize("environ", OFF)
def test_a_legacy_script_off_sends_nothing_and_runs_as_before(
    script: str, environ: dict[str, str]
) -> None:
    _, publish_live, heading = ENTRYPOINTS[script]

    result, stdout = _run(environ, _ENTRYPOINT, str(ROOT / "scripts" / script))

    assert {k: result[k] for k in NOTHING_SENT} == NOTHING_SENT
    assert result["loaded"] == ["ai-catalog-consultant"]
    assert result["states"] == [_expected_state(publish_live)]
    assert heading in stdout
    assert f"{{'live': {publish_live}, 'status': 'fake'}}" in stdout


@pytest.mark.parametrize("script", sorted(ENTRYPOINTS))
def test_a_legacy_script_on_keeps_its_trace_and_flushes_it(script: str) -> None:
    root_name, publish_live, heading = ENTRYPOINTS[script]

    with _langfuse_stand_in() as url:
        result, stdout = _run(
            {**KEYS, "LANGFUSE_BASE_URL": url},
            _ENTRYPOINT,
            str(ROOT / "scripts" / script),
        )

    assert result["instances"] == [PUBLIC_KEY]
    assert {host for _, host in result["attempts"]} == {"127.0.0.1"}
    assert result["states"] == [_expected_state(publish_live)]
    assert heading in stdout
    assert set(_Recorder.requests) == {
        ("POST", "/api/public/otel/v1/traces", EXPECTED_AUTH)
    }

    # Sent by the script's own flush(): the process skips the SDK's atexit.
    spans = {span["name"]: span for span in _Recorder.spans}

    assert sorted(spans) == sorted([root_name, "strategist", "GenericFakeChatModel"])
    root, node, generation = (
        spans[root_name],
        spans["strategist"],
        spans["GenericFakeChatModel"],
    )

    assert len({span["trace"] for span in spans.values()}) == 1
    assert root["parent"] == ""
    assert node["parent"] == root["id"]
    assert generation["parent"] == node["id"]

    assert root["attributes"]["langfuse.observation.type"] == "agent"
    assert generation["attributes"]["langfuse.observation.type"] == "generation"
    usage = generation["attributes"]["langfuse.observation.usage_details"]

    assert json.loads(usage) == {"input": 11, "output": 7, "total": 18}
    assert PROMPT_MARKER in generation["attributes"]["langfuse.observation.input"]
    assert KB_MARKER in node["attributes"]["langfuse.observation.input"]

    if script == "run_demo.py":  # its propagate_attributes() still applies
        for span in spans.values():
            assert span["attributes"]["langfuse.trace.name"] == "ai-smm-pipeline"
            assert span["attributes"]["langfuse.trace.metadata.project_id"] == (
                "ai-catalog-consultant"
            )
