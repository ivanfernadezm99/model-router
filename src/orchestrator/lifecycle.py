"""Exclusive systemd lifecycle — asyncio.Lock, stop-before-start, -np 1, holds, VRAM guard."""

import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path

import httpx

from src.common.notify import notify_error
from src.common.validation import holds_ok, validate_np
from src.orchestrator.health import HARD_TIMEOUT_S, POLL_INTERVAL_S, poll_health
from src.orchestrator.vram import check_vram_for_model, get_vram_used_mb

logger = logging.getLogger(__name__)

SERVICE_RE = re.compile(r"^[a-z0-9-]+\.service$")


def expected_ctx(spec: dict) -> int | None:
    """Contexto esperado (-c/--ctx-size) de los args del registry. None si no figura."""
    try:
        args = list(spec.get("args") or [])
        for i, a in enumerate(args):
            if a in ("-c", "--ctx-size") and i + 1 < len(args):
                return int(args[i + 1])
    except Exception:
        pass
    return None


async def props_n_ctx(port: int, timeout_s: float = 10.0) -> int | None:
    """n_ctx real del servidor en el puerto (vía /props). None si no responde."""
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/props")
            if resp.status_code != 200:
                return None
            data = resp.json()
        return (data.get("default_generation_settings") or {}).get("n_ctx")
    except Exception:
        return None


async def wait_port_free(port: int, endpoint: str, timeout_s: float = 20.0) -> bool:
    """True cuando el puerto deja de responder 200 (el ocupante soltó)."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    async with httpx.AsyncClient(timeout=2.0) as client:
        url = f"http://127.0.0.1:{port}{endpoint}"
        while True:
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return True
            except Exception:
                return True
            if asyncio.get_event_loop().time() >= deadline:
                return False
            await asyncio.sleep(min(POLL_INTERVAL_S, max(0.5, deadline - asyncio.get_event_loop().time())))


def _validate_service(name: str) -> None:
    if not SERVICE_RE.match(name):
        raise ValueError(f"invalid service name: {name}")


_SYSTEMCTL_ENV = {
    "XDG_RUNTIME_DIR": "/run/user/1000",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
}


def _run_systemctl(action: str, service: str) -> subprocess.CompletedProcess:
    _validate_service(service)
    if action not in ("stop", "start", "is-active"):
        raise ValueError(f"invalid action {action}")
    import os
    env = {**os.environ, **_SYSTEMCTL_ENV}
    return subprocess.run(
        ["systemctl", "--user", action, service],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )


def _port_occupant_pids(port: int) -> list[dict]:
    """PIDs escuchando en un puerto TCP, con su cmdline. Solo lectura.

    Esto existe para que un SIGKILL sea atribuible. `fuser -k` es la unica
    llamada capaz de producir `status=9/KILL` en el repo, y hasta ahora
    cuando se ejecutaba no dejaba rastro de a quien mato.
    """
    try:
        res = subprocess.run(
            ["fuser", "-v", f"{int(port)}/tcp"],
            capture_output=True, text=True, timeout=10,
        )
        blob = f"{res.stdout}\n{res.stderr}"
    except Exception as exc:
        logger.warning("port occupant scan failed port=%s err=%s", port, exc)
        return []

    pids: list[int] = []
    for tok in blob.replace("/", " ").replace(":", " ").split():
        if tok.isdigit():
            pids.append(int(tok))

    out, seen = [], set()
    for pid in pids:
        if pid in seen:
            continue
        # `fuser -v 8082/tcp` imprime tambien el numero de puerto en su salida;
        # validar contra /proc descarta esos tokens sin proceso real.
        proc = Path(f"/proc/{pid}/cmdline")
        if not proc.exists():
            continue
        seen.add(pid)
        try:
            cmd = proc.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except Exception:
            cmd = "<unreadable>"
        out.append({"pid": pid, "cmd": cmd[:220]})
    return out


def _kill_port_occupants(
    port: int,
    *,
    target: str | None = None,
    active_model: str | None = None,
    active_service: str | None = None,
    shared_with: list[str] | None = None,
    reason: str = "",
) -> bool:
    """Mata procesos huérfanos escuchando en el puerto (unit borrada, kill manual,
    crash sin cleanup). Último recurso antes de arrancar: sin esto, el nuevo
    server muere con bind failed y el health le da OK al impostor viejo.
    Retorna True si el puerto quedó libre.

    `fuser -k` manda SIGKILL. Antes de matar registra puerto, PIDs, cmdlines,
    target, modelo activo y que modelos del registry comparten el puerto, para
    que un `status=9/KILL` en el journal sea atribuible a este call site.
    """
    port = int(port)
    occupants = _port_occupant_pids(port)
    logger.warning(
        "SIGKILL-PORT-BEGIN port=%s reason=%s target=%s active_model=%s "
        "active_service=%s shared_with=%s occupants=%s",
        port, reason, target, active_model, active_service,
        shared_with or [], json.dumps(occupants),
    )

    # Guard: nunca SIGKILL al servicio que el gateway cree estar sirviendo. Un
    # puerto compartido (8082 tiene 13 modelos) hace que `fuser -k` alcance a
    # todos los que esten vivos en el. Si el modelo activo esta ahi, el switch
    # esta a punto de matar al servidor que el usuario esta usando: fallar
    # honesto es mejor que un SIGKILL sin atribucion.
    if active_service and any(active_service in (o.get("cmd") or "") for o in occupants):
        logger.error(
            "SIGKILL-PORT-REFUSED port=%s active_service=%s esta entre los "
            "ocupantes; no se ejecuta fuser -k para no matar al modelo en servicio",
            port, active_service,
        )
        return False

    try:
        res = subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        logger.warning("SIGKILL-PORT-FAILED port=%s err=%s", port, exc)
        return False
    logger.warning(
        "SIGKILL-PORT-DONE port=%s rc=%s stdout=%r stderr=%r",
        port, res.returncode, (res.stdout or "").strip()[:200], (res.stderr or "").strip()[:200],
    )
    return True


class Orchestrator:
    """Owns exclusive VRAM via asyncio.Lock + systemctl --user."""

    def __init__(self, registry=None):
        self.registry = registry
        self.lock = asyncio.Lock()
        self.active_model: str | None = None
        self.active_service: str | None = None
        self.switching_to: str | None = None  # set before lock, cleared after switch

    def _check_holds(self, target: str | None = None) -> bool:
        a, b = holds_ok()
        # LLM (coder-*) only needs cuda hold; kornia is SDXL/ComfyUI specific (FX-8350 AVX2)
        if target and target.startswith("coder"):
            if not a:
                logger.warning("holds missing cuda=%s kornia=%s — blocking swap for %s", a, b, target)
                return False
            if not b:
                logger.info("kornia missing but target is %s — allowing LLM swap (cuda ok)", target)
            return True
        if not (a and b):
            logger.warning("holds missing cuda=%s kornia=%s — blocking swap", a, b)
            return False
        return True

    async def stop_current(self) -> None:
        if self.active_service:
            _run_systemctl("stop", self.active_service)
            # poll VRAM until <2GB or short grace (best-effort, not blocking health)
            for _ in range(5):
                used = get_vram_used_mb()
                if used is not None and used < 2048:
                    break
                await asyncio.sleep(0.5)
            self.active_model = None
            self.active_service = None

    async def switch_to(self, target: str) -> bool:
        """Exclusive stop-before-start. Returns True on ready, False on timeout/block.

        Callers waiting on the same target coalesce via the lock — only one
        systemctl start is issued per swap.
        """
        if self.registry is None:
            raise RuntimeError("registry not configured")

        # Switching to the model that is already serving is a no-op. This has
        # to be checked BEFORE stop_current(): that call clears active_model,
        # so the coalesce check inside the lock below would never match, and we
        # would stop the model we were just asked to keep and start it again.
        # A pointless swap that also reports success while doing it.
        if target == self.active_model:
            return True

        self.switching_to = target  # signal that a switch is in progress
        try:
            spec = self.registry.resolve(target)  # raises KeyError -> unknown model
            validate_np(spec.get("args", []), spec.get("service"))

            logger.warning(
                "SWITCH-BEGIN target=%s target_service=%s target_port=%s active_model=%s active_service=%s",
                target, spec.get("service"), spec.get("port"),
                self.active_model, self.active_service,
            )

            if not self._check_holds(target):
                logger.warning(
                    "SWITCH-BLOCKED-HOLDS target=%s active_model=%s", target, self.active_model,
                )
                notify_error(f"Switch bloqueado: holds faltan", f"target={target} — cuda/kornia no disponibles")
                return False

            can, used = check_vram_for_model(spec["vram_mb"])
            if not can:
                logger.warning("vram block target=%s needed=%s used=%s", target, spec["vram_mb"], used)
                if used is None:
                    notify_error(f"Switch bloqueado: VRAM sin lectura", f"target={target} needed={spec['vram_mb']}MB")
                    return False

            # detener servicio actual ANTES de arrancar el nuevo.
            # Sin esto, un servicio en puerto diferente sigue "active"
            # en systemd y el auto-detect del health lo adopta.
            try:
                await self.stop_current()
            except Exception as exc:
                logger.warning("stop_current failed before switch target=%s err=%s", target, exc)

            async with self.lock:
                # coalesce: if another waiter already completed this target
                if self.active_model == target:
                    return True

                port = int(spec["port"])
                # stop everything known on this port (active service + same-port
                # siblings + target itself if a previous attempt left it running).
                # Sin esto, un server viejo ocupa el puerto, el health le da OK
                # al impostor y el switch miente "cargado OK".
                to_stop = {spec["service"]}
                if self.active_service:
                    to_stop.add(self.active_service)
                same_port: list[str] = []
                for _name, _spec in (self.registry.models or {}).items():
                    try:
                        if int(_spec.get("port")) == port:
                            to_stop.add(_spec["service"])
                            same_port.append(_name)
                    except Exception:
                        continue
                logger.warning(
                    "SWITCH-STOP-SET target=%s port=%s active_model=%s active_service=%s "
                    "active_in_to_stop=%s to_stop=%s same_port_models=%s",
                    target, port, self.active_model, self.active_service,
                    self.active_service in to_stop if self.active_service else False,
                    sorted(to_stop), sorted(same_port),
                )
                for svc in sorted(to_stop):
                    try:
                        _run_systemctl("stop", svc)
                    except Exception as exc:
                        logger.warning("stop failed svc=%s err=%s", svc, exc)
                # wait VRAM reclaim briefly
                for _ in range(10):
                    used_now = get_vram_used_mb()
                    if used_now is not None and used_now < 2048:
                        break
                    await asyncio.sleep(0.5)
                # el puerto tiene que quedar libre; si sigue respondiendo hay
                # un ocupante desconocido -> matarlo (huérfano) y re-verificar.
                # Si ni así libera, fallar honesto en vez de adoptar un impostor.
                if not await wait_port_free(port, spec["health_endpoint"]):
                    logger.warning("port occupied, killing orphans port=%s target=%s", port, target)
                    _kill_port_occupants(
                        port,
                        target=target,
                        active_model=self.active_model,
                        active_service=self.active_service,
                        shared_with=sorted(same_port),
                        reason="wait_port_free failed before starting target",
                    )
                    for _ in range(10):
                        used_now = get_vram_used_mb()
                        if used_now is not None and used_now < 2048:
                            break
                        await asyncio.sleep(0.5)
                    if not await wait_port_free(port, spec["health_endpoint"]):
                        logger.warning("port still occupied after kill port=%s target=%s", port, target)
                        notify_error(f"Switch falló: puerto {port} ocupado", f"target={target} — ni matando huérfanos liberó {port}{spec['health_endpoint']}")
                        return False

                # re-check VRAM after stop (fail-closed)
                can2, used2 = check_vram_for_model(spec["vram_mb"])
                if not can2:
                    logger.warning("vram still blocked after stop target=%s used=%s", target, used2)
                    if used2 is None:
                        notify_error(f"Switch falló: VRAM bloqueada post-stop", f"target={target} used={used2}")
                        return False
                    # if still over budget after stop, block — exclusive lock should have freed it,
                    # so this means spec itself exceeds 24GB
                    if spec["vram_mb"] > 24 * 1024:
                        notify_error(f"Switch falló: modelo excede 24GB", f"target={target} needed={spec['vram_mb']}MB")
                        return False

                _run_systemctl("start", spec["service"])

                ok = await poll_health(spec["port"], spec["health_endpoint"], timeout_s=HARD_TIMEOUT_S)
                if not ok:
                    # timeout -> rollback stop target
                    logger.warning("health timeout target=%s", target)
                    notify_error(f"Switch timeout: {target}", f"health {spec['port']}{spec['health_endpoint']} no respondió en {HARD_TIMEOUT_S}s — modelo no cargó")
                    try:
                        _run_systemctl("stop", spec["service"])
                    except Exception:
                        pass
                    return False

                # For non-LLM services (echomimic, etc.) that have /load endpoint:
                # call /load to actually load model into VRAM after health check passes.
                # LLM services (llama.cpp) load at startup, so skip.
                if not target.startswith("coder"):
                    try:
                        async with httpx.AsyncClient(timeout=1200) as client:
                            load_resp = await client.post(f"http://127.0.0.1:{port}/load")
                            if load_resp.status_code == 200:
                                logger.info("model loaded into VRAM target=%s", target)
                            else:
                                logger.warning("load failed target=%s status=%s", target, load_resp.status_code)
                    except Exception as exc:
                        logger.warning("load call failed target=%s err=%s", target, exc)

                # verificar que el que responde es el nuestro (n_ctx del /props
                # contra el -c del registry). Sin esto, un server viejo con el
                # mismo puerto da falso positivo.
                want = expected_ctx(spec)
                if want is not None:
                    got = await props_n_ctx(port)
                    if got is None:
                        logger.warning("props unreadable port=%s target=%s", port, target)
                        notify_error(f"Switch falló: /props no responde", f"target={target} port={port} — no se pudo verificar n_ctx")
                        try:
                            _run_systemctl("stop", spec["service"])
                        except Exception:
                            pass
                        return False
                    if abs(got - want) > 512:
                        logger.warning("n_ctx mismatch port=%s want=%s got=%s", port, want, got)
                        notify_error(f"Switch falló: n_ctx mismatch", f"target={target} want={want} got={got} — modelo equivocado en puerto {port}")
                        try:
                            _run_systemctl("stop", spec["service"])
                        except Exception:
                            pass
                        return False

                self.active_model = target
                self.active_service = spec["service"]
                return True
        finally:
            self.switching_to = None
