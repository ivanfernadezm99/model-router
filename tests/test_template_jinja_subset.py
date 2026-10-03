"""The template must only use constructs llama.cpp's jinja engine implements.

llama.cpp renders chat templates with minja (common/jinja/), not with Jinja2.
minja implements a subset. Anything outside it does not degrade gracefully:
an unknown filter raises, the render aborts, and llama.cpp's capability probe
marks the template as having no tool support at all
(chat_template_caps.supports_tool_calls == false). The server then starts fine,
answers /health fine, and returns fluent text with the tool call embedded in it
as prose — so the failure is invisible in every smoke test.

`trim` is the one that bit us. Jinja2 has it, minja has strip / lstrip / rstrip
but not trim. One character of difference, and structured tool calling silently
turned off across the whole fleet.

Run: python3 -m pytest tests/test_template_jinja_subset.py -q
"""

import pathlib
import re

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "templates"

# common/jinja/value.cpp — global_builtins(), filters and tests.
MINJA_FILTERS = {
    "abs", "append", "capitalize", "default", "dictsort", "endswith", "first", "float", "format", "get",
    "indent", "int", "items", "join", "keys", "last", "length", "list", "lower", "lstrip", "map", "max",
    "min", "namespace", "pop", "raise_exception", "range", "reject", "rejectattr", "replace", "reverse",
    "rsplit", "rstrip", "safe", "select", "selectattr", "slice", "sort", "split", "startswith",
    "strftime_now", "string", "strip", "sum", "title", "tojson", "truncate", "unique", "upper", "values",
    "wordcount",
}

MINJA_TESTS = {
    "boolean", "callable", "defined", "divisibleby", "eq", "equalto", "even", "false", "float", "ge",
    "greaterthan", "gt", "in", "integer", "iterable", "lessthan", "lower", "lt", "mapping", "ne", "none",
    "number", "odd", "sequence", "string", "true", "undefined", "upper",
}

# Jinja2 and minja do not agree on whitespace filters, and there is no common
# name: trim exists only in Jinja2, strip/lstrip/rstrip only in minja. A filter
# from either set alone is a runtime failure in the other engine, so the guard
# below checks the INTERSECTION, not minja alone.
JINJA2_ONLY = {"trim"}
MINJA_ONLY = {"strip", "lstrip", "rstrip", "indent", "wordcount", "strftime_now", "dictsort"}

KNOWN_TRAPS = {
    "trim": "exists in Jinja2, NOT in minja — raises, and capability probing then reports supports_tool_calls=false",
    "strip": "exists in minja, NOT in Jinja2",
    "lstrip": "exists in minja, NOT in Jinja2",
    "rstrip": "exists in minja, NOT in Jinja2",
}

TPL_FILES = sorted(TEMPLATES.glob("*.jinja"))


# ChatML control tokens. They contain a pipe but are literal text, not filters.
CHATML = re.compile(r"<\|[a-z_]+\|>")


def _jinja_code_only(source: str) -> str:
    """Drop literal text so prose cannot masquerade as jinja.

    The system prompt is English prose embedded in the template, and it
    contains phrases like "is final for that call" that a naive scan reads as
    the `is final` test.
    """
    source = CHATML.sub(" ", source)
    source = re.sub(r"'(?:[^'\\]|\\.)*'", " ", source)
    source = re.sub(r'"(?:[^"\\]|\\.)*"', " ", source)
    return source


def _filters_used(source: str) -> set[str]:
    scrubbed = _jinja_code_only(source)
    found = set(re.findall(r"\|\s*([A-Za-z_][A-Za-z0-9_]*)", scrubbed))
    found |= set(re.findall(r"\bis\s+(?:not\s+)?([A-Za-z_][A-Za-z0-9_]*)", scrubbed))
    return found


def test_templates_exist():
    assert TPL_FILES, "no chat templates found — every coder unit points at one"


@pytest.mark.parametrize("path", TPL_FILES, ids=lambda p: p.name)
def test_only_minja_supported_constructs(path):
    used = _filters_used(path.read_text())
    unsupported = {c for c in used if c not in MINJA_FILTERS and c not in MINJA_TESTS}
    assert not unsupported, (
        f"{path.name} uses {sorted(unsupported)} which minja does not implement; "
        f"known trap: {KNOWN_TRAPS.get(next(iter(unsupported)), '')}"
    )


@pytest.mark.parametrize("path", TPL_FILES, ids=lambda p: p.name)
def test_filters_are_in_the_jinja2_intersection(path):
    """The template renders in Jinja2 under pytest and in minja under llama.cpp.

    Any construct outside the intersection works in one engine and throws in
    the other, and the suite here would pass while production broke.
    """
    import jinja2

    local = set(jinja2.Environment().filters) | set(MINJA_TESTS)
    used = _filters_used(path.read_text())
    missing_locally = {c for c in used if c not in local}
    assert not missing_locally, f"{path.name} uses {sorted(missing_locally)}, absent from the Jinja2 used by this suite"
    only_local = used & JINJA2_ONLY
    assert not only_local, f"{path.name} uses {sorted(only_local)} — fine in Jinja2, raises inside llama.cpp"
    only_minja = used & MINJA_ONLY
    assert not only_minja, f"{path.name} uses {sorted(only_minja)} — fine in minja, raises under this suite"


@pytest.mark.parametrize("path", TPL_FILES, ids=lambda p: p.name)
def test_no_trim_filter(path):
    """| trim raises inside llama.cpp. This is the regression that broke tools."""
    src = path.read_text()
    assert not re.search(r"\|\s*(trim|strip|lstrip|rstrip)\b", src), (
        f"{path.name} uses a whitespace filter that only one engine implements: {KNOWN_TRAPS}"
    )


@pytest.mark.parametrize("path", TPL_FILES, ids=lambda p: p.name)
def test_tool_calls_are_touched_not_just_serialised(path):
    """capability probing needs the template to actually visit these nodes.

    `tool | tojson` renders the schema but never marks tool.function.name as
    read, so llama.cpp reports supports_tools == false. The schema has to be
    assembled field by field.
    """
    src = path.read_text()
    assert re.search(r"\.function\.name|fn\.name", src), "must read tool.function.name by name, not only tojson the whole tool"
    assert re.search(r"message\.tool_calls|message\.get\(['\"]tool_calls", src), "must read message.tool_calls so the probe sees it"
