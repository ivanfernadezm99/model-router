"""The tool-call envelope must match what llama.cpp actually parses.

llama.cpp builds a PEG parser for tool calls from the model's own vocabulary
(common/chat.cpp). For this build the Qwen3 envelope is:

    <tool_call>
    {"name": ..., "arguments": {...}}
    </tool_call>

where the zero-width space (U+200B) after each `<` is part of the special
token. Qwen2.5 uses `<|tool_call|>` for both open and close.

A template that teaches any other shape still produces fluent output, still
emits the right path, and still looks healthy in a smoke test — the tool call
just arrives as assistant text and OpenCode never sees a tool call at all.
So these tests pin the envelope to the parser's markers rather than to a
pretty-printed expectation.

Run: python3 -m pytest tests/test_template_envelope.py -q
"""

import json
import pathlib
import re

import jinja2
import pytest

ZWSP = "\u200b"
TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "templates"

# Plain markers, no zero-width space. llama.cpp's parser is the consumer here:
# common/chat.cpp matches "<tool_call>" / "</tool_call>" as literal
# bytes and has no ZWSP anywhere in the codebase. A ZWSP in the template
# teaches the model to emit a marker the parser cannot match, which is what
# silently produced content-with-tool-call-text and tool_calls: 0.
Q3_OPEN = "<tool_call>"
Q3_CLOSE = "</tool_call>"
Q25 = "<|tool_call|>"

PATH = "/home/servidor/Descargas/model-router/src/gateway/proxy.py"
TOOLS = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}}, "required": ["filePath"]}}}]

HISTORY = [
    {"role": "user", "content": "lee el archivo"},
    {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read", "arguments": json.dumps({"filePath": PATH})}}]},
    {"role": "tool", "content": "contenido"},
]


def render(name):
    tpl = jinja2.Template((TEMPLATES / name).read_text())
    return tpl.render(messages=HISTORY, tools=TOOLS, add_generation_prompt=True)


# --- the parser defines the marker; nothing may sneak a ZWSP back in ---


def test_qwen3_template_has_no_zero_width_space():
    raw = (TEMPLATES / "qwen3-coder-tools.jinja").read_text()
    assert ZWSP not in raw, (
        "U+200B reintroduced — llama.cpp's chat.cpp does not match it, so tool "
        "calls render as text and tool_calls comes back empty"
    )


def test_qwen3_uses_distinct_open_and_close():
    assert Q3_OPEN != Q3_CLOSE, "ZWSP before tool_call makes open and close distinguishable"


def test_qwen3_emits_parsers_open_marker():
    out = render("qwen3-coder-tools.jinja")
    assert Q3_OPEN in out


def test_qwen3_emits_parsers_close_marker():
    out = render("qwen3-coder-tools.jinja")
    assert Q3_CLOSE in out


def test_qwen3_does_not_emit_the_invented_tools_envelope():
    """The format the model used to be taught, which llama.cpp cannot parse."""
    out = render("qwen3-coder-tools.jinja")
    assert not re.search(r"<tools>\s*\{\"name\"", out)


def test_qwen25_uses_its_own_native_envelope():
    out = render("qwen25-coder-tools.jinja")
    assert Q25 in out
    assert ZWSP not in out


# --- the body between the markers must be what the parser expects ---


@pytest.mark.parametrize("name,open_m,close_m", [("qwen3-coder-tools.jinja", Q3_OPEN, Q3_CLOSE), ("qwen25-coder-tools.jinja", Q25, Q25)])
def test_envelope_body_is_a_single_json_object(name, open_m, close_m):
    out = render(name)
    chunks = [c for c in out.split(open_m)[1:]]
    parsed = []
    for c in chunks:
        raw = c.split(close_m)[0].strip()
        try:
            parsed.append(json.loads(raw))
        except json.JSONDecodeError:
            pass  # the format instruction, with placeholder arguments
    assert parsed, "no tool-call body parsed out of the rendered history"
    for p in parsed:
        assert set(p) == {"name", "arguments"}
        assert isinstance(p["arguments"], dict)


@pytest.mark.parametrize("name", ["qwen3-coder-tools.jinja", "qwen25-coder-tools.jinja"])
def test_path_survives_into_the_envelope(name):
    """The whole reason this template exists: the path must not be mangled."""
    out = render(name)
    assert PATH in out
    # and it must be inside a JSON body, not a bare quoted string
    assert json.dumps({"filePath": PATH})[1:-1] in out


@pytest.mark.parametrize("name", ["qwen3-coder-tools.jinja", "qwen25-coder-tools.jinja"])
def test_tool_response_uses_the_parser_delimiter(name):
    """common/chat.cpp maps the tool role to '<|im_start|>user\\n<tool_response>'."""
    out = render(name)
    assert "<|im_start|>user\n<tool_response>" in out


@pytest.mark.parametrize("name", ["qwen3-coder-tools.jinja", "qwen25-coder-tools.jinja"])
def test_generation_prompt_is_assistant_turn(name):
    out = render(name)
    assert out.rstrip().endswith("<|im_start|>assistant")
