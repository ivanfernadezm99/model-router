"""Guard the served templates against minja, not only against jinja2.

The whole tool-calling bug hid behind a green suite. Both real causes —
`| default(...)` on a missing property and `is sequence` on a String — render
happily under jinja2 and raise under minja, so every jinja2 test passed while
the served template parsed zero tool calls. Passing against jinja2 is not
evidence that the template works.

llama.cpp ships the harness that runs the actual engine:

    build/bin/test-chat-template <template> --with-tools

Its JJ_DEBUG is a runtime flag rather than NDEBUG, so it prints minja's exact
error in milliseconds. The capability block it prints comes from the same probe
that serves /props, so asserting on it here is asserting on production
behaviour, not on a reimplementation.

Skipped when the harness is not built, so the suite still runs on a machine
without a local llama.cpp build.
"""
import os
import pathlib
import re
import subprocess

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "templates"
TEMPLATE_NAMES = ["qwen25-coder-tools.jinja", "qwen3-coder-tools.jinja"]

HARNESS = pathlib.Path.home() / "llama.cpp/build/bin/test-chat-template"

CAP_LINE = re.compile(r"^\s+(supports_\w+)=(\w+)\s*$", re.M)


def _require_harness():
    if not HARNESS.exists():
        pytest.skip(f"harness not built at {HARNESS}")
    return HARNESS


def run_harness(template: pathlib.Path) -> tuple[list[str], dict[str, bool]]:
    """Return (execution errors, capability map) for one template."""
    harness = _require_harness()
    proc = subprocess.run(
        [str(harness), "--with-tools", str(template)],
        capture_output=True,
        text=True,
        timeout=120,
        # the harness links llama's shared objects out of its own build tree
        env={**os.environ, "LD_LIBRARY_PATH": str(harness.parent)},
    )
    out = proc.stdout + proc.stderr
    errors = [ln for ln in out.splitlines() if "Error executing" in ln]
    caps = {k: v == "true" for k, v in CAP_LINE.findall(out)}
    return errors, caps


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_template_renders_without_minja_errors(name):
    """minja must execute the template, not just jinja2."""
    errors, _ = run_harness(TEMPLATES / name)
    assert errors == [], "minja raised while rendering, which /props reports as caps=false:\n" + "\n".join(errors)


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
@pytest.mark.parametrize("cap", ["supports_tools", "supports_tool_calls", "supports_object_arguments"])
def test_template_declares_tool_caps(name, cap):
    """These are the flags llama.cpp reads at serve time."""
    _, caps = run_harness(TEMPLATES / name)
    assert caps, "harness printed no capability block — probe changed shape?"
    assert caps.get(cap) is True, f"{cap} is false for {name}; tool calls will not be parsed"


def test_guard_can_actually_fail(tmp_path):
    """The assertions above must not be vacuous.

    A template with no tool handling at all reports the caps as false, so if
    this ever passes, the harness stopped exercising these flags and every
    other test in this file is meaningless.
    """
    blind = tmp_path / "no-tools.jinja"
    blind.write_text("{{ messages[0].content }}")

    _, caps = run_harness(blind)
    assert caps
    assert caps.get("supports_tool_calls") is False
    assert caps.get("supports_tools") is False
