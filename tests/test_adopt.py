import asyncio
from unittest.mock import AsyncMock, patch


from src.gateway.adopt import probe_running_model, reconcile_active_model, reconcile_until_ready
from src.gateway.app import orchestrator, registry


class _FakeRegistry:
    def __init__(self, models):
        self.models = models


class _FakeOrchestrator:
    def __init__(self, active_model=None, active_service=None, switching_to=None):
        self.active_model = active_model
        self.active_service = active_service
        self.switching_to = switching_to


# Thirteen coder models share port 8082, so a healthy port cannot name one.
_SHARED = {
    "coder-30b-190k": {"service": "llama-code-30b-190k.service", "port": 8082, "health_endpoint": "/health"},
    "coder-14b-100k": {"service": "llama-code-14b-100k.service", "port": 8082, "health_endpoint": "/health"},
    "sdxl": {"service": "sdxl.service", "port": 8188, "health_endpoint": "/health"},
}


def test_identifies_model_by_unit_not_by_shared_port():
    # Both coder units answer on 8082; only one unit is active. If adoption went
    # by port health it would pick whichever appears first in config order.
    reg = _FakeRegistry(_SHARED)
    active = {"llama-code-30b-190k.service"}
    with patch("src.gateway.adopt.systemd_active", side_effect=lambda s: s in active), \
         patch("src.gateway.adopt._port_ready", return_value=True):
        found = asyncio.run(probe_running_model(reg))
    assert found["name"] == "coder-30b-190k"


def test_ignores_active_unit_whose_port_is_still_loading():
    # A unit goes active before its weights land: llama-server answers 503 for
    # ~100s. Adopting on systemd state alone would claim a model that is not
    # serving yet.
    reg = _FakeRegistry(_SHARED)
    active = {"llama-code-30b-190k.service"}
    with patch("src.gateway.adopt.systemd_active", side_effect=lambda s: s in active), \
         patch("src.gateway.adopt._port_ready", return_value=False):
        assert asyncio.run(probe_running_model(reg)) is None


def test_returns_none_when_nothing_runs():
    reg = _FakeRegistry(_SHARED)
    with patch("src.gateway.adopt.systemd_active", return_value=False):
        assert asyncio.run(probe_running_model(reg)) is None


def test_reconcile_fills_missing_active_model():
    orc = _FakeOrchestrator(active_model=None)
    reg = _FakeRegistry(_SHARED)
    active = {"llama-code-30b-190k.service"}
    with patch("src.gateway.adopt.systemd_active", side_effect=lambda s: s in active), \
         patch("src.gateway.adopt._port_ready", return_value=True):
        assert asyncio.run(reconcile_active_model(orc, reg)) == "coder-30b-190k"
    assert orc.active_model == "coder-30b-190k"
    assert orc.active_service == "llama-code-30b-190k.service"


def test_reconcile_never_overrides_a_model_already_set():
    # switch_to owns active_model during an exclusive swap; second-guessing it
    # here would race with the swap in progress.
    orc = _FakeOrchestrator(active_model="sdxl", active_service="sdxl.service")
    reg = _FakeRegistry(_SHARED)
    with patch("src.gateway.adopt.systemd_active", return_value=False):
        assert asyncio.run(reconcile_active_model(orc, reg)) == "sdxl"
    assert orc.active_model == "sdxl"


def test_switch_to_running_model_does_not_reload_it():
    """The regression this fixes, end to end.

    A gateway restart leaves active_model None while the model keeps running.
    POST /jobs/switch compared against None, concluded "not me", and reloaded an
    already-loaded model: a full ~100s stop-before-start for nothing.
    """
    from src.jobs import router as jobs_router

    async def _fake_reconcile(orc, reg):
        orc.active_model = "coder-30b-190k"
        orc.active_service = "llama-code-30b-190k.service"
        return orc.active_model

    previous = orchestrator.active_model
    previous_switching = orchestrator.switching_to
    orchestrator.active_model = None  # the post-restart blind state
    orchestrator.switching_to = None
    try:
        with patch.object(jobs_router, "reconcile_active_model", _fake_reconcile), \
             patch.object(orchestrator, "switch_to", new=AsyncMock(return_value=True)) as sw:
            result = asyncio.run(jobs_router.switch_model(jobs_router.SwitchRequest(model="coder-30b-190k")))
    finally:
        orchestrator.active_model = previous
        orchestrator.switching_to = previous_switching

    assert result["status"] == "already_active"
    sw.assert_not_called()


def test_switch_still_works_when_target_is_genuinely_different():
    """The guard must not swallow a real switch request."""
    from src.jobs import router as jobs_router

    previous = orchestrator.active_model
    previous_switching = orchestrator.switching_to
    orchestrator.active_model = "coder-30b-190k"
    orchestrator.switching_to = None

    async def _fake_reconcile(orc, reg):
        return orc.active_model

    try:
        with patch.object(jobs_router, "reconcile_active_model", _fake_reconcile), \
             patch.object(orchestrator, "switch_to", new=AsyncMock(return_value=True)):
            result = asyncio.run(jobs_router.switch_model(jobs_router.SwitchRequest(model="sdxl")))
    finally:
        orchestrator.active_model = previous
        orchestrator.switching_to = previous_switching

    # accepted as a real switch, not short-circuited into "already_active"
    assert result["status"] == "switching"
    assert result["model"] == "sdxl"


def test_reconcile_until_ready_retries_while_the_model_is_loading():
    # A one-shot reconcile loses the race against a model that is still loading
    # its weights — exactly when a stack restart happens.
    orc = _FakeOrchestrator()
    reg = _FakeRegistry(_SHARED)
    active = {"llama-code-30b-190k.service"}
    probes = {"n": 0}

    def _ready(port, endpoint):
        probes["n"] += 1
        return probes["n"] >= 3

    with patch("src.gateway.adopt.systemd_active", side_effect=lambda s: s in active), \
         patch("src.gateway.adopt._port_ready", side_effect=_ready):
        found = asyncio.run(reconcile_until_ready(orc, reg, deadline_s=30, interval_s=0))

    assert probes["n"] >= 3
    assert found == "coder-30b-190k"
    assert orc.active_model == "coder-30b-190k"


def test_reconcile_until_ready_gives_up_instead_of_spinning_forever():
    orc = _FakeOrchestrator()
    with patch("src.gateway.adopt.systemd_active", return_value=False):
        assert asyncio.run(reconcile_until_ready(orc, _FakeRegistry(_SHARED), deadline_s=0, interval_s=0)) is None
    assert orc.active_model is None


def test_lifespan_starts_clean():
    """Guard against a startup crash-loop shipping green.

    TestClient(app) used without a context manager never runs the lifespan, so
    a NameError in the startup task passed a 167-test suite while systemd
    restart-looped the gateway and /health returned nothing.
    """
    from fastapi.testclient import TestClient
    from src.gateway.app import app

    async def _fast(orc, reg, *a, **k):
        return orc.active_model

    # patching the module global also asserts the name exists at all
    with patch("src.gateway.app.reconcile_until_ready", _fast):
        with TestClient(app) as c:
            assert c.get("/health").status_code == 200
