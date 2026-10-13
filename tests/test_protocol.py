"""Offline protocol contracts, using synthetic frames only.

Shapes follow the public Grok REST serializers and stream consumer researched
for this project. No browser state, captured conversations, or network is used.
"""

import json

import pytest

from grokweb.errors import GrokError
from grokweb.protocol import SearchStream, parse_modes, search_payload, select_mode

WEB = {"url": "https://example.org/research", "title": "Fixture article", "preview": "Excerpt"}
POST = {
    "postId": "100000000000000001",
    "username": "fixture_user",
    "name": "Fixture User",
    "text": "A synthetic research post.",
    "citationId": "fixture-citation",
    "createTime": "2026-01-01T00:00:00Z",
}
POST_URL = "https://x.com/fixture_user/status/100000000000000001"
SECRET = "SYNTHETIC_DO_NOT_ECHO_COOKIE"


def encoded(frame):
    return json.dumps(frame, ensure_ascii=False).encode("utf-8")


def feed(stream, **response):
    stream.feed(encoded({"result": {"response": response}}))


def searched(kind="web"):
    stream = SearchStream(kind)
    field = "webSearchResults" if kind == "web" else "xSearchResults"
    feed(stream, **{field: {"results": []}})
    return stream


def complete(stream, answer="Synthetic answer."):
    feed(stream, token=answer, messageTag="final")
    feed(stream, isSoftStop=True)
    return stream.finish()


def assert_code(exc, expected):
    assert exc.value.code == expected


@pytest.mark.parametrize(
    ("kind", "field", "record", "url", "source_type"),
    [
        ("web", "webSearchResults", WEB, WEB["url"], "web"),
        ("x", "xSearchResults", POST, POST_URL, "x_post"),
    ],
)
def test_native_results_preserve_sources_and_citations(kind, field, record, url, source_type):
    stream = SearchStream(kind)
    feed(stream, **{field: {"results": [record]}})
    result = complete(stream, f"Found a result: {url}")
    assert result["sources"] == [
        {"url": url, "title": record.get("title", record.get("text")), "type": source_type}
    ]
    assert result["citations"] == [url]
    assert "usage" not in result


@pytest.mark.parametrize("kind", ["web", "x"])
def test_native_zero_matches_is_a_successful_search(kind):
    result = complete(searched(kind), "No matches found.")
    assert result == {"answer": "No matches found.", "citations": [], "sources": []}


@pytest.mark.parametrize("kind", ["web", "x"])
@pytest.mark.parametrize("evidence", ["tool_card", "tools_used"])
def test_native_tool_events_confirm_search_without_sources(kind, evidence):
    stream = SearchStream(kind)
    key = "webSearch" if kind == "web" else "xSearch"
    if evidence == "tool_card":
        feed(stream, toolUsageCard={"toolUsageCardId": "tool-1", key: {"args": {"query": "q"}}})
    else:
        feed(stream, finalMetadata={"toolsUsed": {key: True}})
    assert complete(stream, "No matches found.")["sources"] == []


@pytest.mark.parametrize("kind", ["web", "x"])
def test_plain_answers_and_markdown_links_do_not_prove_search(kind):
    stream = SearchStream(kind)
    with pytest.raises(GrokError) as exc:
        complete(stream, f"I searched: [web]({WEB['url']}) and [X]({POST_URL}).")
    assert_code(exc, "search_unconfirmed")


@pytest.mark.parametrize("kind", ["web", "x"])
def test_ordinary_snapshot_empty_source_defaults_do_not_prove_search(kind):
    stream = SearchStream(kind)
    feed(stream, modelResponse={
        "message": "An ordinary answer without running any search.",
        "partial": False,
        "webSearchResults": [],
        "citedWebSearchResults": [],
        "xposts": [],
        "citedXposts": [],
    })
    feed(stream, isSoftStop=True)
    with pytest.raises(GrokError) as exc:
        stream.finish()
    assert_code(exc, "search_unconfirmed")


def test_web_result_linking_to_x_does_not_prove_native_x_search():
    stream = SearchStream("x")
    feed(stream, webSearchResults={"results": [{"url": POST_URL, "title": "Indexed post"}]})
    with pytest.raises(GrokError) as exc:
        complete(stream, POST_URL)
    assert_code(exc, "search_unconfirmed")


@pytest.mark.parametrize("kind", ["web", "x"])
def test_null_tool_card_does_not_prove_execution(kind):
    stream = SearchStream(kind)
    key = "webSearch" if kind == "web" else "xSearch"
    feed(stream, toolUsageCard={"toolUsageCardId": "tool-1", key: None})
    with pytest.raises(GrokError) as exc:
        complete(stream)
    assert_code(exc, "search_unconfirmed")


@pytest.mark.parametrize("kind", ["web", "x"])
def test_false_tools_used_does_not_prove_execution(kind):
    stream = SearchStream(kind)
    feed(stream, finalMetadata={"toolsUsed": {"webSearch": False, "xSearch": False}})
    with pytest.raises(GrokError) as exc:
        complete(stream)
    assert_code(exc, "search_unconfirmed")


@pytest.mark.parametrize(
    "last_response",
    [
        {"modelResponse": {"message": "Complete-looking snapshot.", "partial": False}},
        {"finalMetadata": {"toolsUsed": {"webSearch": True}}},
        {},
    ],
    ids=["snapshot", "final-metadata", "empty-response"],
)
def test_snapshot_metadata_and_framing_terminators_do_not_replace_softstop(last_response):
    stream = searched()
    feed(stream, token="Answer.")
    feed(stream, **last_response)
    stream.feed(encoded({"result": {"title": {"newTitle": "Synthetic title"}}}))
    stream.feed(b"data: [DONE]")
    with pytest.raises(GrokError) as exc:
        stream.finish()
    assert_code(exc, "stream_interrupted")


def test_metadata_and_snapshot_after_softstop_are_still_consumed():
    stream = searched()
    feed(stream, token="Draft answer.", messageTag="final")
    feed(stream, isSoftStop=True)
    feed(stream, finalMetadata={"toolsUsed": {"webSearch": True}})
    feed(stream, modelResponse={"message": "Final snapshot.", "partial": False})
    stream.feed(encoded({"result": {}}))
    assert stream.finish()["answer"] == "Final snapshot."


@pytest.mark.parametrize("softstop_first", [False, True])
def test_partial_snapshot_fails_even_with_softstop(softstop_first):
    stream = searched()
    if softstop_first:
        feed(stream, isSoftStop=True)
    with pytest.raises(GrokError) as exc:
        feed(stream, modelResponse={"message": "Partial answer.", "partial": True})
        complete(stream)
    assert_code(exc, "stream_interrupted")


@pytest.mark.parametrize(
    ("kind", "snapshot", "url"),
    [
        ("web", {"webSearchResults": [WEB], "citedWebSearchResults": [WEB]}, WEB["url"]),
        ("x", {"xposts": [POST], "citedXposts": [POST]}, POST_URL),
    ],
)
def test_snapshot_sources_are_arrays_and_cited_records_are_preserved(kind, snapshot, url):
    stream = SearchStream(kind)
    feed(stream, modelResponse={"message": "Final snapshot.", "partial": False, **snapshot})
    feed(stream, isSoftStop=True)
    result = stream.finish()
    assert [source["url"] for source in result["sources"]] == [url]
    assert result["citations"] == [url]


@pytest.mark.parametrize("kind", ["web", "x"])
@pytest.mark.parametrize("nested_tool_result", [False, True])
def test_snapshot_step_sources_are_not_lost(kind, nested_tool_result):
    stream = SearchStream(kind)
    record, url = (WEB, WEB["url"]) if kind == "web" else (POST, POST_URL)
    if nested_tool_result:
        field = "webSearchResults" if kind == "web" else "xSearchResults"
        step = {"toolUsageResults": [{"toolUsageCardId": "tool-1", field: {"results": [record]}}]}
    else:
        field = "webSearchResults" if kind == "web" else "xposts"
        step = {field: [record]}
    feed(stream, modelResponse={"message": "Snapshot with steps.", "steps": [step]})
    feed(stream, isSoftStop=True)
    result = stream.finish()
    assert [source["url"] for source in result["sources"]] == [url]


def test_search_sources_and_citations_deduplicate_across_events_and_snapshot():
    stream = SearchStream("web")
    feed(stream, webSearchResults={"results": [WEB, WEB]})
    feed(stream, citedWebSearchResults={"results": [WEB]})
    feed(stream, citedSource={"web": WEB})
    feed(stream, modelResponse={"message": WEB["url"], "webSearchResults": [WEB]})
    feed(stream, isSoftStop=True)
    result = stream.finish()
    assert len(result["sources"]) == 1
    assert result["citations"] == [WEB["url"]]


@pytest.mark.parametrize(("kind", "key", "record", "url"), [
    ("web", "web", WEB, WEB["url"]),
    ("x", "xpost", POST, POST_URL),
])
def test_single_cited_source_records(kind, key, record, url):
    stream = SearchStream(kind)
    feed(stream, citedSource={key: record})
    result = complete(stream)
    assert result["citations"] == [url]
    assert result["sources"][0]["url"] == url


@pytest.mark.parametrize("from_snapshot", [False, True])
def test_citation_card_json_strings_resolve_repeated_anchors(from_snapshot):
    stream = searched()
    card = json.dumps({"id": "cite-1", "cardType": "citation_card", "url": WEB["url"]})
    anchor = '<grok:render card_id="cite-1" card_type="citation_card"></grok:render>'
    answer = f"Evidence {anchor} and again {anchor}."
    if from_snapshot:
        feed(stream, modelResponse={"message": answer, "cardAttachmentsJson": [card]})
    else:
        feed(stream, cardAttachment={"jsonData": card})
        feed(stream, token=answer, messageTag="final")
    feed(stream, isSoftStop=True)
    result = stream.finish()
    assert result["answer"] == f"Evidence [1]({WEB['url']}) and again [1]({WEB['url']})."
    assert result["citations"] == [WEB["url"]]


@pytest.mark.parametrize("raw", ["not JSON", "null", "[]", '{"id":"cite-1"}'])
def test_invalid_or_incomplete_cards_do_not_break_answer(raw):
    stream = searched()
    feed(stream, cardAttachment={"jsonData": raw})
    result = complete(stream, 'Answer <grok:render card_id="cite-1" card_type="citation_card"/>')
    assert result["answer"] == "Answer"
    assert result["citations"] == []


def test_non_citation_cards_are_not_fabricated_as_citations():
    stream = searched()
    card = {"id": "image-1", "cardType": "image_card", "url": "https://example.org/image.png"}
    feed(stream, cardAttachment={"jsonData": json.dumps(card)})
    result = complete(stream, 'Answer <grok:render card_id="image-1" card_type="image_card"/>')
    assert result["citations"] == []
    assert card["url"] not in result["answer"]


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "https://user:password@example.org/private",
    "https://example.org/line\nbreak",
])
def test_unsafe_source_and_card_urls_are_not_returned(url):
    stream = searched()
    feed(stream, webSearchResults={"results": [{"url": url, "title": "Unsafe"}]})
    card = {"id": "unsafe", "cardType": "citation_card", "url": url}
    feed(stream, cardAttachment={"jsonData": json.dumps(card)})
    result = complete(stream, 'Answer <grok:render card_id="unsafe" card_type="citation_card"/>')
    assert result == {"answer": "Answer", "citations": [], "sources": []}


def test_thinking_and_tool_tokens_never_leak_into_answer():
    stream = searched()
    feed(stream, token="private reasoning", isThinking=True)
    for tag in ("summary", "header", "thinking", "tool_usage_card", "raw_function_result",
                "tool_partial_output"):
        feed(stream, token=f"hidden-{tag}", messageTag=tag)
    feed(stream, token="你好", messageTag="UNSET")
    feed(stream, token="，", messageTag="FINAL")
    feed(stream, token="世界。")
    feed(stream, isSoftStop=True)
    assert stream.finish()["answer"] == "你好，世界。"


def test_final_text_with_tool_card_id_is_still_answer_text():
    stream = searched()
    feed(stream, token="Final answer.", messageTag="final", toolUsageCardId="tool-1")
    feed(stream, isSoftStop=True)
    assert stream.finish()["answer"] == "Final answer."


def test_empty_completed_answer_is_an_error():
    stream = searched()
    feed(stream, isSoftStop=True)
    with pytest.raises(GrokError) as exc:
        stream.finish()
    assert_code(exc, "empty_answer")


@pytest.mark.parametrize("style", ["ndjson", "crlf", "sse", "without-newline"])
def test_complete_line_framing_variants_and_unicode(style):
    stream = searched()
    lines = [encoded({"result": {"response": {"token": "合成答案。"}}}),
             encoded({"result": {"response": {"isSoftStop": True}}})]
    for line in lines:
        if style == "ndjson":
            line += b"\n"
        elif style == "crlf":
            line += b"\r\n"
        elif style == "sse":
            line = b"data: " + line + b"\n"
        stream.feed(line)
    assert stream.finish()["answer"] == "合成答案。"


def test_keepalives_and_non_response_frames_do_not_fabricate_text():
    stream = searched()
    for line in (b"", b"  \r\n", b": ping", b"event: message", b"id: synthetic"):
        stream.feed(line)
    feed(stream, keepAlive=True)
    stream.feed(encoded({"result": {"conversation": {"conversationId": "fixture"}}}))
    stream.feed(encoded({"result": {"title": {"newTitle": "Synthetic title"}}}))
    assert complete(stream)["answer"] == "Synthetic answer."


@pytest.mark.parametrize("line", [b"{", b"[]", b"null", b"\xff", b'{"result":[]}'])
def test_invalid_lines_raise_controlled_protocol_errors(line):
    with pytest.raises(GrokError) as exc:
        SearchStream("web").feed(line)
    assert_code(exc, "protocol_error")


@pytest.mark.parametrize("location", ["frame", "result", "response", "snapshot"])
@pytest.mark.parametrize(("upstream_code", "expected"), [
    (16, "auth_required"), (7, "verification_required"), (8, "rate_limited"),
    (5, "model_unavailable"), (999, "upstream_error"),
])
def test_upstream_errors_are_classified_without_echoing_untrusted_details(
    location, upstream_code, expected,
):
    error = {"code": upstream_code, "message": SECRET, "details": {"Cookie": SECRET}}
    frame = {
        "frame": {"error": error},
        "result": {"result": {"error": error}},
        "response": {"result": {"response": {"error": error}}},
        "snapshot": {"result": {"response": {"modelResponse": {"error": error}}}},
    }[location]
    with pytest.raises(GrokError) as exc:
        SearchStream("web").feed(encoded(frame))
    assert_code(exc, expected)
    assert SECRET not in str(exc.value)
    assert SECRET not in repr(exc.value)


def test_snapshot_stream_errors_without_error_field_raise_controlled_error():
    stream = searched()
    with pytest.raises(GrokError) as exc:
        feed(stream, modelResponse={
            "message": "Failed answer.",
            "streamErrors": [{"message": SECRET, "severity": "STREAM_ERROR_SEVERITY_FATAL"}],
        })
    assert_code(exc, "upstream_error")
    assert SECRET not in str(exc.value)


def test_only_explicit_valid_usage_is_returned():
    stream = searched()
    feed(stream, modelResponse={"message": "Answer.", "usage": {
        "input_tokens": 12, "output_tokens": 3, "total_tokens": 15,
        "reasoning_tokens": -1, "unknown": SECRET,
    }})
    feed(stream, isSoftStop=True)
    assert stream.finish()["usage"] == {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}


@pytest.fixture
def modes():
    return {"defaultModeId": "fast", "modes": [
        {"id": "fast", "title": "Fast", "availability": {"available": {}}},
        {"id": "expert", "title": "Expert", "availability": {"requiresUpgrade": {
            "message": "Upgrade required", "minimumSubscriptionTier": "TIER_SUPERGROK",
        }}},
        {"id": "heavy", "availability": {"unavailable": {"message": "Temporarily unavailable"}}},
        {"id": "unknown-mode"},
    ]}


def test_mode_availability_and_server_default_are_preserved(modes):
    catalog = parse_modes(modes)
    assert catalog["default_model"] == "fast"
    assert {m["id"]: m["availability"] for m in catalog["models"]} == {
        "fast": "available", "expert": "requires_upgrade", "heavy": "unavailable",
        "unknown-mode": "unknown",
    }
    assert select_mode(catalog, None) == "fast"
    assert select_mode(catalog, " fast ") == "fast"
    assert select_mode(catalog, "unknown-mode") == "unknown-mode"


@pytest.mark.parametrize("model", ["expert", "heavy", "missing", "  "])
def test_unavailable_or_invalid_explicit_modes_are_rejected(modes, model):
    with pytest.raises(GrokError) as exc:
        select_mode(parse_modes(modes), model)
    assert_code(exc, "model_unavailable")


def test_coming_soon_mode_is_not_selectable(modes):
    modes["modes"].append({"id": "future", "availability": {
        "comingSoon": {"message": "Not released", "eta": "unspecified"},
    }})
    catalog = parse_modes(modes)
    with pytest.raises(GrokError) as exc:
        select_mode(catalog, "future")
    assert_code(exc, "model_unavailable")


@pytest.mark.parametrize("default", [None, "missing", "build"])
def test_missing_or_excluded_default_never_falls_back_to_first_mode(modes, default):
    modes["defaultModeId"] = default
    modes["modes"].append({"id": "build", "availability": {"available": {}}})
    catalog = parse_modes(modes)
    assert catalog["default_model"] is None
    assert "build" not in {mode["id"] for mode in catalog["models"]}
    with pytest.raises(GrokError) as exc:
        select_mode(catalog, None)
    assert_code(exc, "default_unavailable")


def test_unavailable_server_default_does_not_silently_choose_another_mode(modes):
    modes["defaultModeId"] = "expert"
    with pytest.raises(GrokError) as exc:
        select_mode(parse_modes(modes), None)
    assert_code(exc, "model_unavailable")


@pytest.mark.parametrize("raw", [None, [], {}, {"modes": []}, {"modes": [{"id": "build"}]}])
def test_invalid_catalog_does_not_invent_models(raw):
    with pytest.raises(GrokError) as exc:
        parse_modes(raw)
    assert_code(exc, "models_unavailable")


def test_malformed_and_duplicate_mode_entries_do_not_replace_valid_model(modes):
    modes["modes"].extend([None, "bad", {}, {"id": ""}, {"id": "fast", "title": "Duplicate"}])
    catalog = parse_modes(modes)
    assert [m["id"] for m in catalog["models"]].count("fast") == 1
    assert next(m for m in catalog["models"] if m["id"] == "fast")["name"] == "Fast"


@pytest.mark.parametrize("kind", ["web", "x"])
def test_payload_selects_native_search_and_preserves_private_temporary_request(kind):
    payload = search_payload("合成搜尋問題", kind, "auto")
    assert payload["modeId"] == "auto"
    assert "合成搜尋問題" in payload["message"]
    assert payload["temporary"] is True
    assert payload["disableMemory"] is True
    assert payload["disablePersonalization"] is True
    assert payload["disableSearch"] is False
    assert payload["enableSideBySide"] is False
    assert payload["searchAllConnectors"] is False
    assert payload["connectorIds"] == payload["connectors"] == payload["collectionIds"] == []
    assert payload["toolOverrides"]["webSearch"] is (kind == "web")
    assert payload["toolOverrides"]["xSearch"] is (kind == "x")
