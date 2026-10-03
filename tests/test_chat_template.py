"""Regression tests for the llama.cpp chat template used by every coder unit.

The template is what keeps a path intact between the user, the model and the
tool. Two bugs shipped here before: `arguments` was double-encoded into a JSON
string, and rendering raised TypeError whenever `content` arrived as a list of
content blocks (Anthropic / tool_result shape).

Run: python3 -m pytest tests/test_chat_template.py -q
"""

import json
import pathlib

import jinja2
import pytest

TEMPLATE_PATH = pathlib.Path(__file__).resolve().parents[1] / "templates" / "qwen25-coder-tools.jinja"

TOOLS = [{"type": "function", "function": {"name": "read", "description": "read a file", "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}}, "required": ["filePath"]}}}]


def render(messages, tools=TOOLS, add_generation_prompt=True):
    tpl = jinja2.Template(TEMPLATE_PATH.read_text())
    return tpl.render(messages=messages, tools=tools, add_generation_prompt=add_generation_prompt)


PATH = "/home/servidor/Descargas/model-router/src/gateway/proxy.py"


def _assistant_calls(out):
    """Every <tools>...</tools> block in the rendered prompt, parsed.

    Skips the tool-schema block and the format instruction, which also sit
    inside <tools> tags but are not tool calls.
    """
    calls = []
    for chunk in out.split("<tools>")[1:]:
        raw = chunk.split("</tools>")[0].strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "name" in parsed and "arguments" in parsed:
            calls.append(parsed)
    return calls


# --- bug 1: content as a list of blocks used to raise TypeError ---


def test_anthropic_content_blocks_do_not_raise():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "lee el archivo"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": ""},
                {"type": "tool_use", "id": "t1", "name": "read", "input": {"filePath": PATH}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "contenido del archivo"}]},
    ]
    out = render(messages)
    assert "contenido del archivo" in out


def test_tool_result_content_is_rendered():
    messages = [
        {"role": "user", "content": "listame"},
        {"role": "tool", "content": [{"type": "text", "text": "proxy.py existe"}]},
    ]
    assert "proxy.py existe" in render(messages)


def test_anthropic_tool_result_keeps_the_tool_response_wrapper():
    """Anthropic tool_result and OpenAI role:'tool' must render the same shape."""
    anthropic = [
        {"role": "user", "content": [{"type": "text", "text": "lee"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "contenido"}]},
    ]
    openai = [
        {"role": "user", "content": "lee"},
        {"role": "tool", "content": "contenido"},
    ]
    assert "<tool_response>\ncontenido\n</tool_response>" in render(anthropic)
    assert "<tool_response>\ncontenido\n</tool_response>" in render(openai)


def test_image_block_does_not_crash():
    messages = [{"role": "user", "content": [{"type": "image", "source": {}}, {"type": "text", "text": "que ves"}]}]
    assert "que ves" in render(messages)


# --- bug 2: arguments double-encoded into a JSON string ---


def test_openai_arguments_render_as_object_not_string():
    messages = [
        {"role": "user", "content": "lee"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read", "arguments": json.dumps({"filePath": PATH})}}]},
    ]
    calls = _assistant_calls(render(messages))
    assert calls, "expected a tool_call block in the rendered prompt"
    args = calls[-1]["arguments"]
    assert isinstance(args, dict), f"arguments must be an object, got {type(args).__name__}"
    assert args["filePath"] == PATH


def test_anthropic_tool_use_input_renders_as_object():
    messages = [
        {"role": "user", "content": "lee"},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "read", "input": {"filePath": PATH}}]},
    ]
    calls = _assistant_calls(render(messages))
    assert calls[-1]["arguments"] == {"filePath": PATH}


def test_arguments_already_a_dict_survives():
    messages = [
        {"role": "user", "content": "lee"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read", "arguments": {"filePath": PATH}}}]},
    ]
    assert _assistant_calls(render(messages))[-1]["arguments"] == {"filePath": PATH}


def test_malformed_arguments_string_does_not_crash():
    """A string starting with '{' is passed through verbatim.

    The template cannot parse JSON, so it must not mangle or hide what the
    client actually sent — a broken block in history is the honest signal.
    """
    messages = [
        {"role": "user", "content": "lee"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read", "arguments": "{not json"}}]},
    ]
    out = render(messages)
    assert "{not json" in out


# --- the whole point: a path survives the round trip ---


@pytest.mark.parametrize(
    "path",
    [
        "/home/servidor/Modelos/Qwen3-Coder/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
        "/home/servidor/Descargas/model-router/templates/qwen25-coder-tools.jinja",
        "~/Descargas/model-router/config.yaml",
        "src/gateway/proxy.py",
    ],
)
def test_path_survives_round_trip(path):
    messages = [
        {"role": "user", "content": f"lee {path}"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read", "arguments": json.dumps({"filePath": path})}}]},
    ]
    out = render(messages)
    assert path in out, f"path mangled in prompt: {path!r}"
    assert _assistant_calls(out)[-1]["arguments"]["filePath"] == path


def test_generated_tool_call_format_is_valid_json():
    """The shape the model is told to emit must be the shape history teaches."""
    messages = [
        {"role": "user", "content": "lee"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read", "arguments": json.dumps({"filePath": PATH})}}]},
    ]
    out = render(messages)
    instruction = '<tools>\n{"name": <function-name>, "arguments": <args-json-object>}\n</tools>'
    assert instruction in out
    for call in _assistant_calls(out):
        assert isinstance(call["arguments"], dict)


# --- structure ---


def test_generation_prompt_ends_with_assistant():
    out = render([{"role": "user", "content": "hola"}])
    assert out.rstrip().endswith("<|im_start|>assistant")


def test_empty_messages_does_not_crash():
    assert isinstance(render([]), str)


def test_system_message_is_preserved():
    out = render([{"role": "system", "content": "SISTEMA PROPIO"}, {"role": "user", "content": "hola"}])
    assert "SISTEMA PROPIO" in out


def test_no_tools_falls_back_to_plain_chat():
    out = render([{"role": "system", "content": "SISTEMA"}, {"role": "user", "content": "hola"}], tools=[])
    assert "SISTEMA" in out
    assert "TOOL ERROR POLICY" not in out


def test_absolute_path_policy_is_injected():
    out = render([{"role": "user", "content": "hola"}])
    assert "PATH RESOLUTION POLICY" in out
    assert "NEVER rewrite" in out
