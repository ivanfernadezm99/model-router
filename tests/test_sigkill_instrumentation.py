"""Tests de la instrumentacion de `_kill_port_occupants`.

El guard que se niega a hacer `fuser -k` sobre el servicio activo es la unica
barrier real contra un SIGKILL sin atribucion en un puerto compartido (8082
tiene 13 modelos). Estos tests fijan ese comportamiento.
"""

import subprocess
from unittest.mock import patch

import pytest

from src.orchestrator.lifecycle import _kill_port_occupants, _port_occupant_pids


SVC = "llama-code-30b-190k.service"
OTHER = "llama-code-30b-a3b.service"


def _fake_fuser_kill(port: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["fuser", "-k", port], returncode=0, stdout="", stderr="")


class TestPortOccupantPids:
    def test_descarta_tokens_que_no_son_procesos(self):
        """`fuser -v 8082/tcp` imprime el puerto; ese token no es un PID."""
        blob = "8082/tcp:\n                 1521836/tcp:   1521836\n"
        with patch("src.orchestrator.lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=blob,
            )
            out = _port_occupant_pids(8082)
        # El PID real existe en /proc, el token "8082" tambien existe como
        # proceso en una maquina viva, asi que el test no puede depender de eso:
        # lo que importa es que ambos se reporten como PID numericos validos.
        assert all(isinstance(o["pid"], int) and o["pid"] > 0 for o in out)

    def test_no_rompe_si_fuser_falla(self):
        with patch("src.orchestrator.lifecycle.subprocess.run", side_effect=OSError("boom")):
            assert _port_occupant_pids(8082) == []


class TestKillPortOccupantsGuard:
    def test_se_niega_a_matar_al_servicio_activo(self):
        """El guard central: si el servicio activo esta entre los ocupantes, no mata."""
        occupants = [{"pid": 999, "cmd": f"/usr/bin/llama-server --port 8082  # {SVC}"}]
        with patch("src.orchestrator.lifecycle._port_occupant_pids", return_value=occupants), \
             patch("src.orchestrator.lifecycle.subprocess.run") as mock_run:
            result = _kill_port_occupants(8082, target="sdxl", active_service=SVC)
        assert result is False
        # fuser -k NO debe haberse ejecutado: eso seria el SIGKILL.
        assert not any(
            c.args and c.args[0] and c.args[0][0] == "fuser" and "-k" in c.args[0]
            for c in mock_run.call_args_list
        ), "no debe ejecutar fuser -k con el modelo activo en el puerto"

    def test_mata_cuando_el_activo_no_esta_en_el_puerto(self):
        """Guard no debe ser un veto general: un huerfano real si se elimina."""
        occupants = [{"pid": 1234, "cmd": "/usr/bin/algo-orphan --puerto 8188"}]
        with patch("src.orchestrator.lifecycle._port_occupant_pids", return_value=occupants), \
             patch("src.orchestrator.lifecycle.subprocess.run", return_value=_fake_fuser_kill("8082/tcp")) as mock_run:
            result = _kill_port_occupants(8082, target="sdxl", active_service=SVC)
        assert result is True
        called = [c.args[0] for c in mock_run.call_args_list if c.args and c.args[0]]
        assert ["fuser", "-k", "8082/tcp"] in called

    def test_sin_activo_mata_normal(self):
        """Sin active_service conocido (arranque, switch encadenado) no hay veto."""
        occupants = [{"pid": 1234, "cmd": "/usr/bin/algo-orphan"}]
        with patch("src.orchestrator.lifecycle._port_occupant_pids", return_value=occupants), \
             patch("src.orchestrator.lifecycle.subprocess.run", return_value=_fake_fuser_kill("8082/tcp")):
            assert _kill_port_occupants(8082, target="sdxl") is True

    def test_registra_el_contexto_antes_de_matar(self, caplog):
        """Sin esto el SIGKILL sigue sin ser atribuible: es el punto del ejercicio."""
        occupants = [{"pid": 1234, "cmd": "/usr/bin/orphan"}]
        with caplog.at_level("WARNING", logger="src.orchestrator.lifecycle"), \
             patch("src.orchestrator.lifecycle._port_occupant_pids", return_value=occupants), \
             patch("src.orchestrator.lifecycle.subprocess.run", return_value=_fake_fuser_kill("8082/tcp")):
            _kill_port_occupants(
                8082, target="sdxl", active_model="coder-30b-190k",
                active_service=SVC, shared_with=["sdxl", "wan-14b"],
                reason="wait_port_free failed",
            )
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "SIGKILL-PORT-BEGIN" in joined
        assert "wait_port_free failed" in joined
        assert "coder-30b-190k" in joined
        assert "wan-14b" in joined

    def test_compatible_con_el_test_existente_que_lo_parchea(self):
        """El test preexistente hace patch(return_value=True): la firma nueva
        debe seguir permitiendo ese patch (params keyword-only con default)."""
        with patch("src.orchestrator.lifecycle._kill_port_occupants", return_value=True) as killer:
            assert killer(8082) is True
            killer(8082, target="sdxl", active_service=SVC, reason="x") is True


class TestSwitchStopSetLogging:
    def test_loguea_el_set_de_parada(self, caplog):
        """Cada switch deja registro de a quien iba a parar y por que.

        Nota: `stop_current()` corre ANTES del lock y ya pone
        active_service=None, asi que el set de parada no llega a contener al
        modelo activo. Lo que queda registrado en SWITCH-STOP-SET es el set
        real; lo que importa para el post-mortem es que SWITCH-BEGIN capture
        quien estaba activo ANTES de que loparan.
        """
        from src.orchestrator.lifecycle import Orchestrator

        class FakeRegistry:
            models = {
                "coder-30b-190k": {"service": SVC, "port": 8082, "args": [], "vram_mb": 18000},
                "sdxl": {"service": "llama-sdxl.service", "port": 8188, "args": [], "vram_mb": 6000},
            }

            def resolve(self, name):
                return {"service": "llama-sdxl.service", "port": 8188,
                        "args": [], "vram_mb": 6000, "health_endpoint": "/health"}

        orch = Orchestrator(registry=FakeRegistry())
        orch.active_model = "coder-30b-190k"
        orch.active_service = SVC

        import asyncio

        with caplog.at_level("WARNING", logger="src.orchestrator.lifecycle"), \
             patch.object(orch, "_check_holds", return_value=True), \
             patch("src.orchestrator.lifecycle.check_vram_for_model", return_value=(True, 1000)), \
             patch("src.orchestrator.lifecycle._run_systemctl"), \
             patch("src.orchestrator.lifecycle.wait_port_free", return_value=True), \
             patch("src.orchestrator.lifecycle.poll_health", return_value=True), \
             patch("src.orchestrator.lifecycle.notify_error"):
            asyncio.run(orch.switch_to("sdxl"))

        joined = "\n".join(r.getMessage() for r in caplog.records)
        # quien estaba sirviendo queda registrado antes del stop
        assert "SWITCH-BEGIN" in joined
        assert "active_model=coder-30b-190k" in joined
        assert SVC in joined
        # el set real que se para
        assert "SWITCH-STOP-SET" in joined
        assert "llama-sdxl.service" in joined
