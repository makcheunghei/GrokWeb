"""Per-request sessions: fresh Chrome cookies, no retries, no credential cache."""

import asyncio
from collections.abc import Awaitable, Callable

import certifi
from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException, Timeout

from grokweb.cookies import ChromeSession, load_chrome, validate_profile
from grokweb.errors import GrokError, http_error
from grokweb.protocol import (
    SearchKind,
    SearchStream,
    bounded_lines,
    parse_modes,
    search_payload,
    select_mode,
)

BASE_URL = "https://grok.com"


class GrokClient:
    def __init__(
        self,
        profile: str = "Default",
        *,
        timeout: float = 120,
        cookie_loader: Callable[[str], Awaitable[ChromeSession]] = load_chrome,
        session_factory: Callable = AsyncSession,
    ):
        self.profile = validate_profile(profile)
        self.timeout = timeout
        self._load = cookie_loader
        self._session = session_factory
        self._lock = asyncio.Lock()

    def _open(self, auth: ChromeSession):
        return self._session(
            impersonate="chrome",
            cookies=auth.jar(),
            timeout=self.timeout,
            allow_redirects=False,
            trust_env=False,
            # curl_cffi 0.14's trust_env flag alone does not clear libcurl's
            # proxy environment or its own environment CA-bundle lookup.
            curl_options={CurlOpt.PROXY: b""},
            verify=certifi.where(),
            headers={
                "User-Agent": auth.user_agent,
                "Origin": BASE_URL,
                "Referer": BASE_URL + "/",
                "Accept": "application/json",
            },
        )

    async def _modes(self, session) -> dict:
        # This is a read operation despite the POST method, matching website JS.
        response = await session.post(BASE_URL + "/rest/modes", json={"locale": "en"})
        if response.status_code != 200:
            raise http_error(response.status_code, models=True)
        try:
            return parse_modes(response.json())
        except (ValueError, TypeError):
            raise GrokError("models_unavailable") from None

    async def models(self) -> dict:
        return await self._run(None, None, None)

    async def search(self, query: str, kind: SearchKind, model: str | None = None) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 20_000:
            raise GrokError("invalid_query")
        if kind not in {"web", "x"}:
            raise GrokError("invalid_query")
        return await self._run(query.strip(), kind, model)

    async def _run(self, query: str | None, kind: SearchKind | None, model: str | None):
        try:
            # Timeout includes queueing, Keychain, discovery and the entire stream.
            async with asyncio.timeout(self.timeout), self._lock:
                auth = await self._load(self.profile)
                async with self._open(auth) as session:
                    catalog = await self._modes(session)
                    if kind is None:
                        return catalog
                    selected = select_mode(catalog, model)
                    parser = SearchStream(kind)
                    response = None
                    try:
                        response = await session.post(
                            BASE_URL + "/rest/app-chat/conversations/new",
                            json=search_payload(query, kind, selected),
                            stream=True,
                        )
                        if response.status_code != 200:
                            raise http_error(response.status_code)
                        async for line in bounded_lines(response.aiter_content()):
                            parser.feed(line)
                        return parser.finish()
                    finally:
                        # curl_cffi's aclose only awaits the transfer. Signal/cancel
                        # it first so cancellation doesn't wait for the 120s limit.
                        if response is not None:
                            if response.quit_now is not None:
                                response.quit_now.set()
                            task = response.astream_task
                            if task is not None:
                                if not task.done():
                                    task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
        except GrokError as exc:
            if exc.code == "auth_required":
                raise GrokError(exc.code, profile=self.profile) from None
            raise
        except (TimeoutError, Timeout):
            raise GrokError("timeout") from None
        except RequestException as exc:
            # Streaming curl transfers surface timeouts as the base exception.
            if exc.code == 28:  # CURLE_OPERATION_TIMEDOUT
                raise GrokError("timeout") from None
            raise GrokError("network_error") from None
        except Exception:
            # Never propagate curl/JSON/SQLite exception strings with credentials.
            raise GrokError("internal_error") from None
