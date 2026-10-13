"""Inspect real libcurl options without scheduling a network transfer.

Use empty synthetic browser state. Only the native transfer scheduler is
replaced; AsyncSession request preparation and Curl.setopt remain real.
"""

import certifi
import pytest
from curl_cffi import Curl, CurlOpt
from curl_cffi.requests import session as curl_session

from grokweb.client import GrokClient
from grokweb.cookies import ChromeSession


class TransferIntercepted(Exception):
    """Stop after request options are applied, before any network activity."""


@pytest.fixture
def effective_options(monkeypatch):
    observations = []
    handles = []

    class RecordingCurl(Curl):
        def __init__(self, *args, **kwargs):
            self.applied_options = {}
            super().__init__(*args, **kwargs)
            handles.append(self)

        def setopt(self, option, value):
            result = super().setopt(option, value)
            # Record only successfully applied native options, including any
            # later overrides during request preparation.
            self.applied_options[option] = value
            return result

    class OfflineMulti:
        def __init__(self, *, loop):
            pass

        def add_handle(self, curl):
            # AsyncCurl normally ensures the CA immediately before scheduling.
            # Preserve that final setup step, but never create a native multi
            # handle, perform DNS, or connect to a socket.
            curl._ensure_cacert()
            observations.append(dict(curl.applied_options))
            raise TransferIntercepted

        def remove_handle(self, curl):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(curl_session, "Curl", RecordingCurl)
    monkeypatch.setattr(curl_session, "AsyncCurl", OfflineMulti)
    try:
        yield observations
    finally:
        # A stream request interrupted before scheduling has not returned its
        # handle to the session pool, so also release those handles explicitly.
        for handle in handles:
            handle.close()


@pytest.mark.parametrize("stream", [False, True], ids=["modes", "search-stream"])
@pytest.mark.parametrize(
    "ca_environment",
    [
        ("REQUESTS_CA_BUNDLE",),
        ("CURL_CA_BUNDLE",),
        ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"),
    ],
    ids=["requests-ca", "curl-ca", "both-ca"],
)
async def test_effective_proxy_and_ca_ignore_environment(
    monkeypatch, effective_options, stream, ca_environment,
):
    for name in (
        "http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    ):
        monkeypatch.setenv(name, "http://synthetic-proxy.invalid:8080")
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(name, "")
    for name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(name, raising=False)
    for name in ca_environment:
        monkeypatch.setenv(name, f"/synthetic/{name}.pem")

    auth = ChromeSession([], "Synthetic Chrome")
    client = GrokClient()
    endpoint = "/rest/app-chat/conversations/new" if stream else "/rest/modes"
    async with client._open(auth) as session:
        with pytest.raises(TransferIntercepted):
            await session.post("https://grok.com" + endpoint, json={}, stream=stream)

    assert len(effective_options) == 1
    options = effective_options[0]
    # Inspect the final native options, not constructor arguments or attributes.
    assert options[CurlOpt.PROXY] in (b"", "")
    assert options[CurlOpt.CAINFO] in (certifi.where(), certifi.where().encode())
