"""Regression tests for thinking-prefill stacking.

A second thinking-only prefill retry must merge into the existing trailing
prefill message instead of appending another assistant message. llama.cpp
(gemma chat templates) rejects message lists ending in 2+ assistant messages
("Cannot have 2 or more assistant messages at the end of the list"), so
stacked prefills turned every second retry into an HTTP 400.
"""

from agent.conversation_loop import _append_thinking_prefill


def _prefill(content="", reasoning=None):
    msg = {
        "role": "assistant",
        "content": content,
        "finish_reason": "incomplete",
        "_thinking_prefill": True,
    }
    if reasoning is not None:
        msg["reasoning"] = reasoning
    return msg


def test_first_prefill_appends():
    messages = [
        {"role": "user", "content": "do the task"},
    ]
    _append_thinking_prefill(messages, _prefill(reasoning="thinking A"))
    assert len(messages) == 2
    assert messages[-1]["_thinking_prefill"] is True
    assert messages[-1]["reasoning"] == "thinking A"


def test_second_prefill_merges_instead_of_stacking():
    messages = [
        {"role": "user", "content": "do the task"},
    ]
    _append_thinking_prefill(messages, _prefill(reasoning="thinking A"))
    _append_thinking_prefill(messages, _prefill(reasoning="thinking B"))

    assistant_tail = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_tail) == 1
    assert messages[-1]["reasoning"] == "thinking A\nthinking B"


def test_merge_combines_content_and_reasoning_fields():
    messages = [{"role": "user", "content": "go"}]
    first = _prefill(content="", reasoning="r1")
    first["reasoning_content"] = "rc1"
    second = _prefill(content="partial text", reasoning="r2")
    second["reasoning_content"] = "rc2"

    _append_thinking_prefill(messages, first)
    _append_thinking_prefill(messages, second)

    tail = messages[-1]
    assert tail["content"] == "partial text"  # empty first content not joined
    assert tail["reasoning"] == "r1\nr2"
    assert tail["reasoning_content"] == "rc1\nrc2"


def test_prefill_after_real_assistant_message_appends():
    """A trailing non-prefill assistant message must not be merged into."""
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "real answer"},
    ]
    _append_thinking_prefill(messages, _prefill(reasoning="r"))
    assert len(messages) == 3
    assert messages[1]["content"] == "real answer"
    assert "reasoning" not in messages[1]
    assert messages[-1]["_thinking_prefill"] is True
