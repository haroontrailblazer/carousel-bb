from app.runs.stream import build_trace


def _row(seq: int, timestamp: str, parts: list[dict]) -> dict:
    return {
        "seq": seq,
        "created_at": timestamp,
        "event_data": {
            "author": "research",
            "invocation_id": "invocation-1",
            "content": {"parts": parts},
        },
    }


def test_build_trace_pairs_tools_and_exposes_recorded_sources() -> None:
    rows = [
        _row(
            1,
            "2026-08-25T12:00:00+00:00",
            [
                {
                    "function_call": {
                        "id": "call-1",
                        "name": "search_web",
                        "args": {"query": "client tracing"},
                    }
                }
            ],
        ),
        _row(
            2,
            "2026-08-25T12:00:02+00:00",
            [
                {
                    "function_response": {
                        "id": "call-1",
                        "name": "search_web",
                        "response": {
                            "answer": "See https://supabase.com/docs/tracing for details.",
                            "sources": [
                                "https://supabase.com/docs/tracing",
                                "https://opentelemetry.io/docs/concepts/context-propagation/",
                            ],
                        },
                    }
                }
            ],
        ),
    ]

    frames, summary = build_trace(rows)

    assert frames[0]["tools"][0]["status"] == "ok"
    assert frames[0]["tools"][0]["ms"] == 2000
    assert frames[1]["data"]["sources"] == [
        "https://supabase.com/docs/tracing",
        "https://opentelemetry.io/docs/concepts/context-propagation/",
    ]
    assert summary["tool_calls"] == 1


def _brief_call(seq: int, args: dict) -> dict:
    return _row(
        seq,
        "2026-08-25T12:01:00+00:00",
        [{"function_call": {"id": "call-2", "name": "save_research_brief", "args": args}}],
    )


def test_brief_call_publishes_per_fact_citations() -> None:
    """The mapping claim -> URL already exists in the brief; the trace has to
    carry it through, or the console can only ever list links beside prose and
    leave the reader guessing which link proved what."""
    frames, _ = build_trace(
        [
            _brief_call(
                1,
                {
                    "summary": "Chips.",
                    "key_facts": [
                        {"fact": "Revenue was $41.1B.", "source_url": "https://www.sec.gov/a"},
                        {"fact": "Taken from the article itself.", "source_url": ""},
                    ],
                    "sources": ["https://reuters.com/x"],
                    "media_candidates": ["https://cdn.example.com/keynote.mp4"],
                },
            )
        ]
    )

    assert frames[0]["data"]["facts"] == [
        {"fact": "Revenue was $41.1B.", "source_url": "https://www.sec.gov/a"},
        # Kept, uncited: it came from the news item's own text, which is a
        # different thing from an unsourced invention.
        {"fact": "Taken from the article itself.", "source_url": ""},
    ]


def test_brief_sources_include_fact_urls_but_not_cover_footage() -> None:
    frames, _ = build_trace(
        [
            _brief_call(
                1,
                {
                    "key_facts": [{"fact": "A.", "source_url": "https://www.sec.gov/a"}],
                    "sources": ["https://reuters.com/x"],
                    # Footage to clip for the cover - a URL, but nothing was
                    # read from it, so filing it under "sources" would be a
                    # lie of categorisation.
                    "media_candidates": ["https://cdn.example.com/keynote.mp4"],
                },
            )
        ]
    )

    sources = frames[0]["data"]["sources"]
    assert "https://reuters.com/x" in sources
    assert "https://www.sec.gov/a" in sources
    assert not any("keynote.mp4" in url for url in sources)


def test_brief_facts_are_bounded_and_reject_junk_urls() -> None:
    frames, _ = build_trace(
        [
            _brief_call(
                1,
                {
                    "key_facts": [
                        {"fact": f"Fact {i}.", "source_url": "https://e.com/x"}
                        for i in range(30)
                    ]
                    + [
                        {"fact": "", "source_url": "https://e.com/y"},
                        {"fact": "Bad scheme.", "source_url": "javascript:alert(1)"},
                    ],
                },
            )
        ]
    )

    facts = frames[0]["data"]["facts"]
    assert len(facts) == 12  # capped; one huge brief cannot bloat the trace
    assert all(f["fact"] for f in facts)


def test_a_fact_with_a_javascript_url_is_never_cited() -> None:
    frames, _ = build_trace(
        [_brief_call(1, {"key_facts": [{"fact": "X.", "source_url": "javascript:alert(1)"}]})]
    )

    assert frames[0]["data"]["facts"] == [{"fact": "X.", "source_url": ""}]
    assert "sources" not in frames[0].get("data", {})


def test_other_tool_calls_publish_no_facts() -> None:
    frames, _ = build_trace(
        [
            _row(
                1,
                "2026-08-25T12:00:00+00:00",
                [{"function_call": {"id": "c", "name": "render_slide", "args": {"key_facts": [{"fact": "no"}]}}}],
            )
        ]
    )

    assert "facts" not in frames[0].get("data", {})
