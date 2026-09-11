import pytest

from src.registry.registry import Registry


def test_happy():
    r = Registry("config.yaml").load()
    assert r.resolve("coder-14b-200k")["port"] == 8082
    assert r.resolve("sdxl")["port"] == 8188


def test_unknown():
    r = Registry("config.yaml").load()
    with pytest.raises(KeyError, match="unknown model"):
        r.resolve("video-unknown")


def test_np_rejected():
    from src.common.validation import validate_np

    with pytest.raises(ValueError):
        validate_np(["-np", "2"])
    with pytest.raises(ValueError):
        validate_np(["-np", "5"])


def test_ssrf():
    from src.common.validation import validate_health

    with pytest.raises(ValueError):
        validate_health("http://evil.com", 8082)
    with pytest.raises(ValueError):
        validate_health("/evil", 8082)
    validate_health("/health", 8082)
    validate_health("/system_stats", 8188)
