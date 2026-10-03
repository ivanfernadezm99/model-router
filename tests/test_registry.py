import pytest

from src.registry.registry import Registry


def test_happy():
    r = Registry("config.yaml").load()
    assert r.resolve("coder-14b-100k")["port"] == 8082
    assert r.resolve("sdxl")["port"] == 8188


def test_unknown():
    r = Registry("config.yaml").load()
    with pytest.raises(KeyError, match="unknown model"):
        r.resolve("video-unknown")


def test_resolves_regardless_of_cwd(tmp_path, monkeypatch):
    """The registry must not depend on the cwd the process happens to have.

    Two loaders used to walk a cwd-relative fallback list in opposite order,
    so the same cwd could yield two different registries.
    """
    monkeypatch.chdir(tmp_path)
    r = Registry().load()
    assert len(r.models) > 0
    assert r.resolve("vision-7b")["port"] == 8083


def test_resolve_is_case_insensitive():
    r = Registry().load()
    assert r.resolve("CODER-30B-A3B")["port"] == 8082


def test_resolve_strips_whitespace():
    r = Registry().load()
    assert r.resolve("  vision-7b  ")["port"] == 8083


def test_registry_load_is_not_silently_empty(tmp_path):
    """A registry that fails validation must raise, not come back as {}."""
    bad = tmp_path / "config.yaml"
    bad.write_text("models:\n  roto:\n    service: x.service\n    port: 9999\n    health_endpoint: /health\n    vram_mb: 1\n    args: []\n")
    with pytest.raises(ValueError, match="health not allowlisted"):
        Registry(str(bad)).load()


def test_np_rejected():
    from src.common.validation import validate_np

    with pytest.raises(ValueError):
        validate_np(["-np", "2"])
    with pytest.raises(ValueError):
        validate_np(["-np", "5"])
    # excepción chatbot: solo el service autorizado pasa con -np > 1
    validate_np(["-np", "8"], "llama-code-qwen3-14b-190k.service")
    with pytest.raises(ValueError):
        validate_np(["-np", "8"], "llama-code-q4.service")


def test_ssrf():
    from src.common.validation import validate_health

    with pytest.raises(ValueError):
        validate_health("http://evil.com", 8082)
    with pytest.raises(ValueError):
        validate_health("/evil", 8082)
    validate_health("/health", 8082)
    validate_health("/system_stats", 8188)
