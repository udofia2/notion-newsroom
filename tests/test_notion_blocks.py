from __future__ import annotations

from newsroom.notion.blocks import build_historical_context_toggle_block


def test_historical_context_uses_page_mention_for_uuid_source() -> None:
    block = build_historical_context_toggle_block(
        contexts=[
            {
                "source_page_id": "33005e19-5115-8083-ae09-c085eac6a14f",
                "title": "UUID source",
                "snippet": "Snippet",
                "score": 0.9,
            }
        ],
        query="test query",
    )

    rich_text = block["toggle"]["children"][0]["bulleted_list_item"]["rich_text"]
    assert rich_text[1]["type"] == "mention"
    assert rich_text[1]["mention"]["type"] == "page"


def test_historical_context_uses_text_link_for_non_uuid_source() -> None:
    block = build_historical_context_toggle_block(
        contexts=[
            {
                "source_page_id": "191080",
                "title": "CSV source",
                "snippet": "Snippet",
                "score": 0.9,
                "url": "https://example.com/archive/191080",
            }
        ],
        query="test query",
    )

    rich_text = block["toggle"]["children"][0]["bulleted_list_item"]["rich_text"]
    assert rich_text[1]["type"] == "text"
    assert rich_text[1]["text"]["content"] == "191080"
    assert rich_text[1]["text"]["link"]["url"] == "https://example.com/archive/191080"


def test_historical_context_splits_long_snippet_into_safe_rich_text_chunks() -> None:
    long_snippet = "A" * 3666
    block = build_historical_context_toggle_block(
        contexts=[
            {
                "source_page_id": "33005e19-5115-8083-ae09-c085eac6a14f",
                "title": "Long snippet",
                "snippet": long_snippet,
                "score": 0.9,
            }
        ],
        query="test query",
    )

    snippet_rich_text = block["toggle"]["children"][0]["bulleted_list_item"]["children"][0]["paragraph"]["rich_text"]
    assert len(snippet_rich_text) >= 3
    assert all(len(item["text"]["content"]) <= 1800 for item in snippet_rich_text)
    assert "".join(item["text"]["content"] for item in snippet_rich_text) == long_snippet
