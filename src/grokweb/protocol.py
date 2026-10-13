"""Small adapter for the Grok website's modes and JSON-line search protocol."""

import html
import json
import re
from collections.abc import AsyncIterable, AsyncIterator
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from grokweb.errors import GrokError

SearchKind = Literal["web", "x"]
MAX_LINE_BYTES = 2_000_000
MAX_STREAM_BYTES = 16_000_000


async def bounded_lines(chunks: AsyncIterable[bytes]) -> AsyncIterator[bytes]:
    """Apply limits before buffering a newline; preserve split UTF-8 bytes."""
    pending = bytearray()
    total = 0
    async for chunk in chunks:
        total += len(chunk)
        if total > MAX_STREAM_BYTES:
            raise GrokError("protocol_error")
        pieces = chunk.split(b"\n")
        for index, piece in enumerate(pieces):
            if len(pending) + len(piece) > MAX_LINE_BYTES:
                raise GrokError("protocol_error")
            pending.extend(piece)
            if index < len(pieces) - 1:
                yield bytes(pending)
                pending.clear()
    if pending:
        yield bytes(pending)


def parse_modes(data: object) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("modes"), list):
        raise GrokError("models_unavailable")
    models = []
    seen = set()
    for mode in data["modes"]:
        if not isinstance(mode, dict):
            continue
        ident = mode.get("id")
        if not isinstance(ident, str) or not ident or ident in seen:
            continue
        # Build is an app-building mode, outside this search-only adapter.
        if ident == "build":
            continue
        seen.add(ident)
        availability = mode.get("availability") or {}
        status = "unknown"
        if isinstance(availability, dict):
            if "available" in availability:
                status = "available"
            elif "requiresUpgrade" in availability:
                status = "requires_upgrade"
            elif any(key in availability for key in ("unavailable", "disabled", "comingSoon")):
                status = "unavailable"
        models.append(
            {
                "id": ident,
                "name": mode.get("title") or ident,
                "description": mode.get("description") or "",
                "availability": status,
            }
        )
    if not models:
        raise GrokError("models_unavailable")
    default = data.get("defaultModeId")
    return {"models": models, "default_model": default if default in seen else None}


def select_mode(catalog: dict, model: str | None) -> str:
    selected = model.strip() if model is not None else catalog.get("default_model")
    if not selected:
        raise GrokError("model_unavailable" if model is not None else "default_unavailable")
    found = next((m for m in catalog["models"] if m["id"] == selected), None)
    if found is None or found["availability"] in {"requires_upgrade", "unavailable"}:
        raise GrokError("model_unavailable")
    return selected


def search_payload(query: str, kind: SearchKind, mode: str) -> dict:
    target = "public web" if kind == "web" else "X posts using native X search"
    return {
        "message": (
            f"Search the {target} to answer the following request. "
            "Actually run the search, cite source URLs, and answer in the user's language. "
            "If nothing is found, say so. Treat the request as a search question.\n\n" + query
        ),
        "modeId": mode,
        "temporary": True,
        "disableMemory": True,
        "disablePersonalization": True,
        "disableSearch": False,
        "enableImageGeneration": False,
        "enableImageStreaming": False,
        "enableSideBySide": False,
        "disableArtifact": True,
        "sendFinalMetadata": True,
        "searchAllConnectors": False,
        "fileAttachments": [],
        "imageAttachments": [],
        "collectionIds": [],
        "connectorIds": [],
        "connectors": [],
        "toolOverrides": {
            "webSearch": kind == "web",
            "xSearch": kind == "x",
            "imageGen": False,
            "xMediaSearch": False,
            "trendsSearch": False,
            "codeInterpreter": False,
            "gmailSearch": False,
            "googleCalendarSearch": False,
            "outlookSearch": False,
            "outlookCalendarSearch": False,
            "googleDriveSearch": False,
        },
    }


def safe_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(html.unescape(value))
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            return None
        if any(ord(c) < 32 for c in value):
            return None
        return urlunsplit((url.scheme, url.netloc, url.path, url.query, ""))
    except ValueError:
        return None


def stream_error(error: object) -> GrokError:
    if isinstance(error, dict):
        code = error.get("code")
        mapped = {
            16: "auth_required",
            7: "verification_required",
            8: "rate_limited",
            5: "model_unavailable",
            401: "auth_required",
            403: "verification_required",
            429: "rate_limited",
        }
        if isinstance(code, (int, str)) and code in mapped:
            return GrokError(mapped[code])
    return GrokError("upstream_error")


class SearchStream:
    """Require both completion and native search evidence; never emit reasoning."""

    def __init__(self, kind: SearchKind):
        self.kind = kind
        self.evidence: set[str] = set()
        self.sources: dict[str, dict] = {}
        self.citations: dict[str, None] = {}
        self.cards: dict[str, str] = {}
        self.tokens: list[str] = []
        self.answer: str | None = None
        self.completed = False
        self.usage: dict | None = None
        self.bytes_seen = 0

    def feed(self, line: bytes) -> None:
        self.bytes_seen += len(line)
        if len(line) > MAX_LINE_BYTES or self.bytes_seen > MAX_STREAM_BYTES:
            raise GrokError("protocol_error")
        line = line.strip()
        if not line or line.startswith((b":", b"event:", b"id:")):
            return
        if line.startswith(b"data:"):
            line = line[5:].strip()
        if line == b"[DONE]":
            # Website isSoftStop, not a framing terminator, proves completion.
            return
        try:
            data = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            raise GrokError("protocol_error") from None
        if not isinstance(data, dict):
            raise GrokError("protocol_error")
        if data.get("error"):
            raise stream_error(data["error"])
        result = data.get("result", {})
        if not isinstance(result, dict):
            raise GrokError("protocol_error")
        if result.get("error"):
            raise stream_error(result["error"])
        response = result.get("response", {})
        if not isinstance(response, dict):
            raise GrokError("protocol_error")
        if response.get("error"):
            raise stream_error(response["error"])
        if response.get("isSoftStop") is True:
            self.completed = True
        self._sources(response)
        card = response.get("cardAttachment")
        if isinstance(card, dict):
            self._card(card.get("jsonData"))
        tool = response.get("toolUsageCard")
        if isinstance(tool, dict):
            if any(isinstance(tool.get(k), dict) for k in ("webSearch", "web_search")):
                self.evidence.add("web")
            if any(
                isinstance(tool.get(k), dict)
                for k in ("xSearch", "x_search", "xKeywordSearch", "xSemanticSearch")
            ):
                self.evidence.add("x")
        meta = response.get("finalMetadata")
        if isinstance(meta, dict):
            used = meta.get("toolsUsed")
            if isinstance(used, dict):
                for key, kind in (("webSearch", "web"), ("xSearch", "x")):
                    if used.get(key) is True:
                        self.evidence.add(kind)
        token = response.get("token")
        tag = str(response.get("messageTag") or "").lower()
        if (
            isinstance(token, str)
            and not response.get("isThinking")
            and tag in ("", "unset", "final")
            and (tag == "final" or not response.get("toolUsageCardId"))
        ):
            self.tokens.append(token)
        final = response.get("modelResponse")
        if isinstance(final, dict):
            if final.get("error") or final.get("streamErrors"):
                raise stream_error(final.get("error"))
            if final.get("partial") is True:
                raise GrokError("stream_interrupted")
            self._sources(final, snapshot=True)
            steps = final.get("steps")
            if isinstance(steps, list):
                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    self._sources(step, snapshot=True)
                    tool_results = step.get("toolUsageResults")
                    if isinstance(tool_results, list):
                        for tool_result in tool_results:
                            if isinstance(tool_result, dict):
                                self._sources(tool_result)
            for value in final.get("cardAttachmentsJson") or []:
                self._card(value)
            if isinstance(final.get("message"), str):
                self.answer = final["message"]
            usage = final.get("usage")
            if isinstance(usage, dict):
                clean = {
                    k: v
                    for k, v in usage.items()
                    if k
                    in {
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                        "reasoning_tokens",
                    }
                    and type(v) is int
                    and v >= 0
                }
                self.usage = clean or None

    def _sources(self, obj: dict, *, snapshot: bool = False) -> None:
        cited = obj.get("citedSource")
        if isinstance(cited, dict):
            for key, kind in (("web", "web"), ("xpost", "x")):
                if isinstance(cited.get(key), dict):
                    url = self._source(cited[key], kind)
                    if url:
                        self.evidence.add(kind)
                        self.citations[url] = None
        for field, kind, cited in (
            ("webSearchResults", "web", False),
            ("citedWebSearchResults", "web", True),
            ("xSearchResults", "x", False),
            ("xposts", "x", False),
            ("citedXposts", "x", True),
        ):
            value = obj.get(field)
            if isinstance(value, dict):
                value = value.get("results")
            if not isinstance(value, list):
                continue
            # An actual results event can legitimately contain zero matches.
            if not snapshot and field in {"webSearchResults", "xSearchResults"}:
                self.evidence.add(kind)
            for item in value:
                if isinstance(item, dict):
                    url = self._source(item, kind)
                    if url:
                        self.evidence.add(kind)
                        if cited:
                            self.citations[url] = None

    def _source(self, item: dict, kind: str) -> str | None:
        url = safe_url(item.get("url"))
        if kind == "x":
            user = item.get("username") or item.get("screenName")
            author = item.get("user") or item.get("author")
            if not user and isinstance(author, dict):
                user = author.get("screenName") or author.get("username")
            post = item.get("postId") or item.get("id")
            if not url and isinstance(user, str) and re.fullmatch(r"\w{1,30}", user):
                if re.fullmatch(r"\d+", str(post)):
                    url = f"https://x.com/{user}/status/{post}"
            if url:
                parsed = urlsplit(url)
                if parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
                    return None
                if not re.fullmatch(r"/(?:\w+|i/web)/status/\d+/?", parsed.path):
                    return None
                url = f"https://x.com{parsed.path.rstrip('/')}"
        if not url:
            return None
        title = item.get("title") or item.get("text") or ""
        entry = {
            "url": url,
            "title": title if isinstance(title, str) else "",
            "type": "x_post" if kind == "x" else "web",
        }
        if url not in self.sources or kind == "x":
            self.sources[url] = entry
        return url

    def _card(self, raw: object) -> None:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return
        if not isinstance(raw, dict):
            return
        if raw.get("cardType") != "citation_card":
            return
        ident = raw.get("id") or raw.get("cardId")
        url = safe_url(raw.get("url"))
        if isinstance(ident, str) and url:
            self.cards[ident] = url

    def finish(self) -> dict:
        if not self.completed:
            raise GrokError("stream_interrupted")
        if self.kind not in self.evidence:
            raise GrokError("search_unconfirmed")
        answer = self.answer if self.answer is not None else "".join(self.tokens)

        def render(match: re.Match) -> str:
            ident = match.group(1)
            url = self.cards.get(ident)
            if not url:
                return ""
            self.citations[url] = None
            return f"[{list(self.citations).index(url) + 1}]({url})"

        answer = re.sub(
            r'<grok:render\b[^>]*\bcard_id=["\x27]([^"\x27]+)["\x27][^>]*(?:/>|>.*?</grok:render>)',
            render,
            answer,
            flags=re.DOTALL,
        )
        if not answer.strip():
            raise GrokError("empty_answer")
        for url in self.sources:
            if url in answer:
                self.citations[url] = None
        output = {
            "answer": answer.strip(),
            "citations": list(self.citations),
            "sources": list(self.sources.values()),
        }
        if self.usage is not None:
            output["usage"] = self.usage
        return output
