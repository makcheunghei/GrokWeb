"""Bound memory before a newline arrives, independently of the JSON parser."""

import json

import pytest

from grokweb import protocol
from grokweb.errors import GrokError
from grokweb.protocol import SearchStream, bounded_lines


async def chunks(*values):
    for value in values:
        yield value


async def test_utf8_split_across_chunks_and_no_final_newline():
    data = '{"answer":"香港"}\r\n{"end":true}'.encode()
    result = [line async for line in bounded_lines(chunks(*(bytes([c]) for c in data)))]
    assert [json.loads(line) for line in result] == [{"answer": "香港"}, {"end": True}]


async def test_line_limit_stops_reading_before_newline(monkeypatch):
    monkeypatch.setattr(protocol, "MAX_LINE_BYTES", 8)
    consumed = []

    async def source():
        for part in [b"12345", b"6789", b"not consumed\n"]:
            consumed.append(part)
            yield part

    with pytest.raises(GrokError, match="protocol_error"):
        _ = [line async for line in bounded_lines(source())]
    assert consumed == [b"12345", b"6789"]


async def test_total_limit_includes_newlines_and_empty_lines(monkeypatch):
    monkeypatch.setattr(protocol, "MAX_STREAM_BYTES", 8)
    with pytest.raises(GrokError, match="protocol_error"):
        _ = [line async for line in bounded_lines(chunks(b"\n" * 5, b"\n" * 4))]


async def test_many_short_lines_in_large_chunk_do_not_exceed_line_limit(monkeypatch):
    monkeypatch.setattr(protocol, "MAX_LINE_BYTES", 4)
    assert len([line async for line in bounded_lines(chunks(b"1234\n" * 20))]) == 20


@pytest.mark.parametrize(
    "kind,field,rows",
    [
        ("web", "webSearchResults", [None]),
        ("x", "xposts", [{}]),
        ("web", "citedWebSearchResults", [{"url": "javascript:bad"}]),
    ],
)
def test_invalid_snapshot_sources_do_not_manufacture_search_evidence(kind, field, rows):
    parser = SearchStream(kind)
    parser.feed(
        json.dumps(
            {
                "result": {
                    "response": {
                        "isSoftStop": True,
                        "modelResponse": {"message": "Ordinary answer", field: rows},
                    }
                }
            }
        ).encode()
    )
    with pytest.raises(GrokError, match="search_unconfirmed"):
        parser.finish()
