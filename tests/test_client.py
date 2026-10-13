"""Exercise the real client/protocol with synthetic cookies and fake curl I/O."""

import asyncio
import json
import socket
import subprocess
from collections import deque
from unittest.mock import AsyncMock, Mock, call

import pytest
from curl_cffi.const import CurlECode
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException, Timeout

from grokweb.client import BASE_URL, GrokClient
from grokweb.cookies import ChromeSession
from grokweb.errors import GrokError

MODES_URL = BASE_URL + "/rest/modes"
SEARCH_URL = BASE_URL + "/rest/app-chat/conversations/new"
PRIVATE_DETAIL = "synthetic-private-cookie-or-upstream-body"


def _catalog(default="mode-alpha"):
    return {
        "defaultModeId": default,
        "modes": [
            {"id": ident, "title": ident, "availability": {"available": {}}}
            for ident in ("mode-alpha", "mode-beta")
        ],
    }


def _line(response):
    return json.dumps({"result": {"response": response}}).encode()


def _success_lines(kind="web"):
    if kind == "web":
        sources = {"webSearchResults": [{"url": "https://example.test/source", "title": "Test"}]}
    else:
        sources = {"xSearchResults": [{"username": "synthetic", "postId": "123", "text": "Test"}]}
    return [
        _line(sources),
        _line({"modelResponse": {"message": f"Synthetic {kind} answer"}}),
        _line({"isSoftStop": True}),
    ]


class FakeResponse:
    """Non-stream responses have synchronous json(), as curl_cffi does."""

    def __init__(self, *, status=200, data=None, json_error=None):
        self.status_code = status
        self.data = _catalog() if data is None else data
        self.json_error = json_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.data


class FakeStream:
    """Match response.aiter_content, quit_now and astream_task without real curl."""

    def __init__(self, *, status=200, lines=None, failure=None, gate=None, transfer=True):
        self.status_code = status
        self.lines = _success_lines() if lines is None else lines
        self.failure = failure
        self.gate = gate
        self.quit_now = asyncio.Event() if transfer else None
        self.astream_task = None
        self.transfer_started = asyncio.Event()
        self.transfer_stopped = asyncio.Event()
        self.read_started = asyncio.Event()
        self.read_count = 0

    async def start(self):
        if self.quit_now is None:
            return

        async def transfer():
            self.transfer_started.set()
            try:
                await self.quit_now.wait()
            finally:
                self.transfer_stopped.set()

        self.astream_task = asyncio.create_task(transfer())
        await self.transfer_started.wait()

    async def aiter_content(self):
        self.read_count += 1
        self.read_started.set()
        if self.gate is not None:
            await self.gate.wait()
        for line in self.lines:
            yield line + b"\n"
        if self.failure is not None:
            raise self.failure


class FakeSession:
    def __init__(self, *, modes=None, stream=None, modes_gate=None, search_gate=None):
        self.modes = FakeResponse() if modes is None else modes
        self.stream = FakeStream() if stream is None else stream
        self.modes_gate = modes_gate
        self.search_gate = search_gate
        self.modes_started = asyncio.Event()
        self.search_started = asyncio.Event()
        self.calls = []
        self.entered = False
        self.closed = False
        self.exit_type = None

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True
        self.exit_type = exc_type
        # Deliberately do not clean up the fake transfer: the client must do it.

    async def post(self, url, *, json, stream=False):
        self.calls.append({"url": url, "json": json, "stream": stream})
        if url == MODES_URL:
            assert json == {"locale": "en"}
            assert stream is False
            self.modes_started.set()
            if self.modes_gate is not None:
                await self.modes_gate.wait()
            if isinstance(self.modes, Exception):
                raise self.modes
            return self.modes
        assert url == SEARCH_URL
        assert stream is True
        self.search_started.set()
        if self.search_gate is not None:
            await self.search_gate.wait()
        if isinstance(self.stream, Exception):
            raise self.stream
        await self.stream.start()
        return self.stream


class Harness:
    def __init__(self, sessions, *, timeout=120, profile="Profile 2"):
        self.sessions = sessions
        self.remaining = deque(sessions)
        self.auths = []
        self.opened = []
        self.loader = AsyncMock(side_effect=self.load)
        self.factory = Mock(side_effect=self.open)
        self.client = GrokClient(
            profile, timeout=timeout, cookie_loader=self.loader, session_factory=self.factory,
        )

    async def load(self, profile):
        number = len(self.auths) + 1
        auth = ChromeSession([{
            "domain": ".grok.com", "path": "/", "secure": True, "expires": None,
            "name": "sso", "value": f"synthetic-cookie-{number}",
        }], f"Synthetic Chrome agent {number}")
        self.auths.append(auth)
        return auth

    def open(self, **kwargs):
        self.opened.append(kwargs)
        assert self.remaining, "Unexpected extra session: possible automatic retry"
        return self.remaining.popleft()


@pytest.fixture(autouse=True)
def forbid_live_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Client tests must not access real cookies, subprocesses or the network")

    async def forbidden_spawn(*args, **kwargs):
        forbidden()

    # Patch the class itself because GrokClient's default factory is bound at import.
    monkeypatch.setattr(AsyncSession, "__init__", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
async def harness():
    instances = []
    foreground_tasks = []

    def create(sessions, **kwargs):
        instance = Harness(sessions, **kwargs)
        instances.append(instance)
        return instance

    def spawn(coro):
        task = asyncio.create_task(coro)
        foreground_tasks.append(task)
        return task

    create.spawn = spawn
    yield create
    # Keep failed concurrency assertions from leaking tasks into the next test.
    for task in foreground_tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*foreground_tasks, return_exceptions=True)
    transfers = [
        session.stream.astream_task
        for instance in instances for session in instance.sessions
        if isinstance(session.stream, FakeStream) and session.stream.astream_task is not None
    ]
    for task in transfers:
        if not task.done():
            task.cancel()
    await asyncio.gather(*transfers, return_exceptions=True)


def _assert_transfer_stopped(stream):
    assert stream.quit_now.is_set()
    assert stream.astream_task.done()
    assert stream.transfer_stopped.is_set()


async def _wait(event):
    await asyncio.wait_for(event.wait(), timeout=1)


@pytest.mark.parametrize("phase", ["modes", "search"])
@pytest.mark.parametrize("status, expected", [
    (401, "auth_required"), (403, "verification_required"), (429, "rate_limited"),
    (404, "model_unavailable"), (500, "upstream_error"), (302, "auth_required"),
])
async def test_http_failure_mapping_and_no_retry(harness, phase, status, expected):
    session = FakeSession(
        modes=FakeResponse(status=status if phase == "modes" else 200),
        stream=FakeStream(status=status if phase == "search" else 200),
    )
    h = harness([session])

    with pytest.raises(GrokError) as exc:
        await h.client.search("Synthetic query", "web")

    if phase == "modes" and status == 404:
        expected = "models_unavailable"
    assert exc.value.code == expected
    if expected == "auth_required":
        assert "Chrome Profile 2" in str(exc.value)
    assert session.closed
    assert h.loader.await_count == h.factory.call_count == 1
    assert [request["url"] for request in session.calls] == (
        [MODES_URL] if phase == "modes" else [MODES_URL, SEARCH_URL]
    )
    if phase == "search":
        _assert_transfer_stopped(session.stream)
        assert session.stream.read_count == 0


@pytest.mark.parametrize("code, expected", [
    (16, "auth_required"), (7, "verification_required"),
    (8, "rate_limited"), (5, "model_unavailable"),
])
async def test_error_inside_successful_http_stream_is_not_retried(harness, code, expected):
    stream = FakeStream(lines=[_line({"error": {"code": code, "message": PRIVATE_DETAIL}})])
    session = FakeSession(stream=stream)
    h = harness([session])

    with pytest.raises(GrokError) as exc:
        await h.client.search("Synthetic query", "web")

    assert exc.value.code == expected
    assert PRIVATE_DETAIL not in str(exc.value)
    assert h.loader.await_count == h.factory.call_count == 1
    assert len(session.calls) == 2
    assert session.closed
    _assert_transfer_stopped(stream)


@pytest.mark.parametrize("malformed", ["json-error", "non-object", "missing-modes", "empty-modes"])
async def test_invalid_model_response_never_falls_back_to_fixed_models(harness, malformed):
    response = {
        "json-error": FakeResponse(json_error=ValueError(PRIVATE_DETAIL)),
        "non-object": FakeResponse(data=[]),
        "missing-modes": FakeResponse(data={"model": "invented"}),
        "empty-modes": FakeResponse(data={"modes": []}),
    }[malformed]
    session = FakeSession(modes=response)
    h = harness([session])

    with pytest.raises(GrokError) as exc:
        await h.client.search("Synthetic query", "web")

    assert exc.value.code == "models_unavailable"
    assert PRIVATE_DETAIL not in str(exc.value)
    assert [request["url"] for request in session.calls] == [MODES_URL]
    assert session.closed


async def test_fresh_cookies_and_user_agent_for_every_search_and_model_request(harness):
    sessions = [FakeSession() for _ in range(3)]
    h = harness(sessions)

    await h.client.search("First", "web")
    await h.client.models()
    await h.client.search("Second", "web")

    assert h.loader.await_args_list == [call("Profile 2")] * 3
    assert h.factory.call_count == 3
    for number, (options, session) in enumerate(zip(h.opened, sessions, strict=True), 1):
        assert {cookie.name: cookie.value for cookie in options["cookies"]} == {
            "sso": f"synthetic-cookie-{number}",
        }
        assert options["headers"]["User-Agent"] == f"Synthetic Chrome agent {number}"
        assert options["allow_redirects"] is False
        assert options["trust_env"] is False
        assert options["timeout"] == 120
        assert session.closed
    assert [request["url"] for request in sessions[1].calls] == [MODES_URL]
    assert len({id(options["cookies"]) for options in h.opened}) == 3


async def test_default_mode_is_discovered_again_for_each_search(harness):
    sessions = [
        FakeSession(modes=FakeResponse(data=_catalog("mode-alpha"))),
        FakeSession(modes=FakeResponse(data=_catalog("mode-beta"))),
    ]
    h = harness(sessions)

    for _ in sessions:
        result = await h.client.search("Synthetic query", "web")
        assert result["answer"] == "Synthetic web answer"

    assert [session.calls[1]["json"]["modeId"] for session in sessions] == [
        "mode-alpha", "mode-beta",
    ]
    assert all(session.calls[0]["url"] == MODES_URL for session in sessions)


@pytest.mark.parametrize("kind", ["web", "x"])
async def test_explicit_mode_and_search_kind_reach_real_payload_and_parser(harness, kind):
    session = FakeSession(stream=FakeStream(lines=_success_lines(kind)))
    h = harness([session])

    result = await h.client.search("  Synthetic query  ", kind, " mode-beta ")

    payload = session.calls[1]["json"]
    assert payload["modeId"] == "mode-beta"
    assert payload["message"].endswith("\n\nSynthetic query")
    assert payload["toolOverrides"]["webSearch"] is (kind == "web")
    assert payload["toolOverrides"]["xSearch"] is (kind == "x")
    assert payload["temporary"] is payload["disableMemory"] is True
    assert payload["searchAllConnectors"] is False
    assert payload["connectorIds"] == []
    assert result["answer"] == f"Synthetic {kind} answer"
    assert result["sources"][0]["type"] == ("web" if kind == "web" else "x_post")
    if kind == "x":
        assert result["sources"][0]["url"] == "https://x.com/synthetic/status/123"
    _assert_transfer_stopped(session.stream)


@pytest.mark.parametrize("condition, model, expected", [
    ("missing-default", None, "default_unavailable"),
    ("unknown-default", None, "default_unavailable"),
    ("normal", "missing-model", "model_unavailable"),
    ("normal", " ", "model_unavailable"),
    ("requiresUpgrade", "mode-alpha", "model_unavailable"),
    ("unavailable", "mode-alpha", "model_unavailable"),
])
async def test_unavailable_model_never_sends_search(harness, condition, model, expected):
    catalog = _catalog()
    if condition == "missing-default":
        del catalog["defaultModeId"]
    elif condition == "unknown-default":
        catalog["defaultModeId"] = "not-in-catalog"
    elif condition in {"requiresUpgrade", "unavailable"}:
        catalog["modes"][0]["availability"] = {condition: {}}
    session = FakeSession(modes=FakeResponse(data=catalog))
    h = harness([session])

    with pytest.raises(GrokError) as exc:
        await h.client.search("Synthetic query", "web", model)

    assert exc.value.code == expected
    assert [request["url"] for request in session.calls] == [MODES_URL]
    assert session.closed


@pytest.mark.parametrize("phase", ["modes", "search-post", "stream"])
@pytest.mark.parametrize("error_type, expected", [
    (RequestException, "network_error"), (Timeout, "timeout"), (RuntimeError, "internal_error"),
])
async def test_transport_errors_are_controlled_without_partial_success_or_retry(
    harness, phase, error_type, expected,
):
    failure = error_type(PRIVATE_DETAIL)
    session = FakeSession(
        modes=failure if phase == "modes" else None,
        stream=failure if phase == "search-post" else FakeStream(
            lines=_success_lines(), failure=failure if phase == "stream" else None,
        ),
    )
    h = harness([session])

    with pytest.raises(GrokError) as exc:
        await h.client.search("Synthetic query", "web")

    assert exc.value.code == expected
    assert PRIVATE_DETAIL not in str(exc.value)
    assert "Synthetic web answer" not in str(exc.value)
    assert h.loader.await_count == h.factory.call_count == 1
    assert len(session.calls) == (1 if phase == "modes" else 2)
    assert session.closed
    if phase == "stream":
        _assert_transfer_stopped(session.stream)


async def test_curl_stream_timeout_error_code_is_classified_as_timeout(harness):
    # curl_cffi's streaming perform() enqueues RequestException for every CurlError,
    # including code 28; it does not construct the Timeout subclass used by POST.
    stream = FakeStream(failure=RequestException(PRIVATE_DETAIL, CurlECode.OPERATION_TIMEDOUT))
    session = FakeSession(stream=stream)
    h = harness([session])

    with pytest.raises(GrokError) as exc:
        await h.client.search("Synthetic query", "web")

    assert exc.value.code == "timeout"
    assert PRIVATE_DETAIL not in str(exc.value)
    assert len(session.calls) == 2
    _assert_transfer_stopped(stream)


@pytest.mark.parametrize("failure", ["truncated", "malformed", "no-evidence"])
async def test_invalid_stream_does_not_retry_and_always_cleans_up(harness, failure):
    lines, expected = {
        "truncated": (_success_lines()[:-1], "stream_interrupted"),
        "malformed": ([b"synthetic-private-not-json"], "protocol_error"),
        "no-evidence": (_success_lines()[1:], "search_unconfirmed"),
    }[failure]
    session = FakeSession(stream=FakeStream(lines=lines))
    h = harness([session])
    with pytest.raises(GrokError) as exc:
        await h.client.search("Synthetic query", "web")
    assert exc.value.code == expected
    assert len(session.calls) == 2
    assert h.loader.await_count == 1
    assert session.closed
    _assert_transfer_stopped(session.stream)


async def test_response_without_transfer_handles_can_finish(harness):
    session = FakeSession(stream=FakeStream(transfer=False))
    h = harness([session])
    result = await h.client.search("Synthetic query", "web")
    assert result["answer"] == "Synthetic web answer"
    assert session.closed


async def test_timeout_includes_lock_wait_and_does_not_load_cookies(harness):
    session = FakeSession()
    h = harness([session], timeout=0.03)
    async with h.client._lock:
        with pytest.raises(GrokError) as exc:
            await asyncio.wait_for(h.client.search("Queued", "web"), timeout=1)
        assert exc.value.code == "timeout"
        h.loader.assert_not_awaited()
        h.factory.assert_not_called()
        assert h.client._lock.locked()

    result = await asyncio.wait_for(h.client.search("After timeout", "web"), timeout=1)
    assert result["answer"] == "Synthetic web answer"


@pytest.mark.parametrize("phase", ["cookies", "modes", "stream"])
async def test_total_timeout_covers_each_await_and_leaves_client_usable(harness, phase):
    gate = asyncio.Event()
    first = FakeSession(
        modes_gate=gate if phase == "modes" else None,
        stream=FakeStream(gate=gate if phase == "stream" else None),
    )
    recovery = FakeSession()
    h = harness([recovery] if phase == "cookies" else [first, recovery], timeout=0.03)
    if phase == "cookies":
        async def load(profile):
            if h.loader.await_count == 1:
                await gate.wait()
            return await h.load(profile)
        h.loader.side_effect = load

    with pytest.raises(GrokError) as exc:
        await asyncio.wait_for(h.client.search("Will time out", "web"), timeout=1)

    assert exc.value.code == "timeout"
    assert not h.client._lock.locked()
    if phase != "cookies":
        assert first.closed
    if phase == "stream":
        _assert_transfer_stopped(first.stream)
    result = await asyncio.wait_for(h.client.search("Recovered", "web"), timeout=1)
    assert result["answer"] == "Synthetic web answer"


@pytest.mark.parametrize("following_operation", ["search", "models"])
async def test_search_serializes_cookie_loading_and_requests(harness, following_operation):
    gate = asyncio.Event()
    first = FakeSession(stream=FakeStream(gate=gate))
    second = FakeSession()
    h = harness([first, second])
    first_task = harness.spawn(h.client.search("First", "web"))
    await _wait(first.stream.read_started)
    second_started = asyncio.Event()

    async def follow():
        second_started.set()
        if following_operation == "models":
            return await h.client.models()
        return await h.client.search("Second", "web")

    second_task = harness.spawn(follow())
    await _wait(second_started)
    assert h.loader.await_count == h.factory.call_count == 1
    assert second.calls == []
    assert not second_task.done()

    gate.set()
    first_result, second_result = await asyncio.wait_for(
        asyncio.gather(first_task, second_task), timeout=1,
    )
    assert first_result["answer"] == "Synthetic web answer"
    if following_operation == "models":
        assert second_result["default_model"] == "mode-alpha"
    else:
        assert second_result["answer"] == "Synthetic web answer"
    assert h.loader.await_count == h.factory.call_count == 2
    assert first.closed and second.closed
    assert not h.client._lock.locked()


@pytest.mark.parametrize("phase", ["cookies", "modes", "search-post", "stream"])
async def test_cancellation_releases_lock_and_next_call_is_usable(harness, phase):
    gate = asyncio.Event()
    first = FakeSession(
        modes_gate=gate if phase == "modes" else None,
        search_gate=gate if phase == "search-post" else None,
        stream=FakeStream(gate=gate if phase == "stream" else None),
    )
    recovery = FakeSession()
    h = harness([recovery] if phase == "cookies" else [first, recovery])
    cookie_started = asyncio.Event()
    if phase == "cookies":
        async def load(profile):
            if h.loader.await_count == 1:
                cookie_started.set()
                await gate.wait()
            return await h.load(profile)
        h.loader.side_effect = load
    started = {
        "cookies": cookie_started, "modes": first.modes_started,
        "search-post": first.search_started, "stream": first.stream.read_started,
    }[phase]
    task = harness.spawn(h.client.search("Cancel me", "web"))
    await _wait(started)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert not h.client._lock.locked()
    if phase != "cookies":
        assert first.closed
        assert first.exit_type is asyncio.CancelledError
    if phase == "stream":
        _assert_transfer_stopped(first.stream)
        assert first.stream.astream_task.cancelled()
    result = await asyncio.wait_for(h.client.search("After cancellation", "web"), timeout=1)
    assert result["answer"] == "Synthetic web answer"
    assert recovery.closed


async def test_cancelled_waiter_does_not_release_another_requests_lock(harness):
    gate = asyncio.Event()
    first = FakeSession(stream=FakeStream(gate=gate))
    recovery = FakeSession()
    h = harness([first, recovery])
    holder = harness.spawn(h.client.search("Holding lock", "web"))
    await _wait(first.stream.read_started)
    waiting = asyncio.Event()

    async def queued():
        waiting.set()
        return await h.client.search("Cancel in queue", "web")

    waiter = harness.spawn(queued())
    await _wait(waiting)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert h.client._lock.locked()
    assert h.loader.await_count == 1
    assert not first.stream.quit_now.is_set()
    gate.set()
    await asyncio.wait_for(holder, timeout=1)
    assert not h.client._lock.locked()
    result = await h.client.search("Still usable", "web")
    assert result["answer"] == "Synthetic web answer"


@pytest.mark.parametrize("code", ["auth_required", "keychain_denied", "cookie_database"])
async def test_cookie_loader_errors_are_preserved_without_opening_session(harness, code):
    h = harness([])
    h.loader.side_effect = GrokError(code)
    with pytest.raises(GrokError) as exc:
        await h.client.search("Synthetic query", "web")
    assert exc.value.code == code
    h.loader.assert_awaited_once_with("Profile 2")
    h.factory.assert_not_called()
    if code == "auth_required":
        assert "Chrome Profile 2" in str(exc.value)


@pytest.mark.parametrize("query, kind", [
    (None, "web"), ("", "web"), (" \n ", "web"), ("x" * 20_001, "web"), ("query", "other"),
])
async def test_invalid_queries_do_not_load_cookies_or_open_sessions(harness, query, kind):
    h = harness([])
    with pytest.raises(GrokError) as exc:
        await h.client.search(query, kind)
    assert exc.value.code == "invalid_query"
    h.loader.assert_not_awaited()
    h.factory.assert_not_called()
