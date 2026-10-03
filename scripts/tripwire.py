#!/usr/bin/env python3
"""Tripwire: graba el contexto exacto en el instante en que un servicio llama muere.

Existe porque `status=9/KILL` no dice QUIEN lo mato, y sin root no hay forma de
ver al sender. Cada muerte deja en logs/tripwire.jsonl: estado previo, MainPID,
ExecMainCode/Status, NRestarts, snapshot de memoria, sockets conectados al puerto
y las ultimas lineas del journal del servicio y del gateway.

Uso:  python3 scripts/tripwire.py [--interval 3]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "logs" / "tripwire.jsonl"
ROUTER_LOG = REPO / "logs" / "router-errors.log"
PORT = "8082"

SYSTEMD_ENV = {
    **os.environ,
    "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
    "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
}

PROPS = (
    "ActiveState", "SubState", "MainPID", "NRestarts",
    "ExecMainCode", "ExecMainStatus", "ActiveEnterTimestamp",
)


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def run(cmd: list[str], timeout: int = 10) -> str:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=SYSTEMD_ENV,
        )
        return (r.stdout or "") + (r.stderr or "")
    except Exception as exc:
        return f"<error: {exc}>"


def llama_services() -> list[str]:
    out = run(["systemctl", "--user", "list-units", "--type=service", "--all",
               "--no-legend", "--no-pager", "llama-*.service"])
    svcs = []
    for line in out.splitlines():
        name = line.split()[0] if line.split() else ""
        if name.startswith("llama-") and name.endswith(".service"):
            svcs.append(name)
    return sorted(svcs)


def snapshot(svc: str) -> dict:
    raw = run(["systemctl", "--user", "show", svc, *(f"-p{p}" for p in PROPS)])
    snap = {"service": svc}
    for line in raw.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            snap[k] = v
    return snap


def memory() -> dict:
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                out[k] = v.strip()
    except Exception as exc:
        out["error"] = str(exc)
    try:
        psi = Path("/proc/pressure/memory").read_text().strip().replace("\n", " | ")
        out["psi_memory"] = psi
    except Exception:
        pass
    try:
        rss = run(["ps", "-o", "rss=", "-C", "llama-server"]).strip()
        if rss:
            out["llama_rss_mib"] = round(int(rss.split()[0]) / 1024)
    except Exception:
        pass
    return out


def socket_context() -> str:
    return run(["ss", "-tnp", f"sport = :{PORT} or dport = :{PORT}"]).strip()[:1200]


def tail(path: Path, n: int = 15) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()[-n:]
    except Exception:
        return []


def journal_tail(svc: str, n: int = 25) -> list[str]:
    out = run(["journalctl", "--user", "-u", svc, "-n", str(n), "--no-pager"])
    return [ln for ln in out.splitlines() if ln.strip()]


def record(kind: str, svc: str, prev: dict | None, cur: dict, extra: dict | None = None) -> None:
    event = {
        "ts": now(),
        "kind": kind,
        "prev": prev,
        "current": cur,
        "memory": memory(),
        "sockets_8082": socket_context(),
        "journal": journal_tail(svc),
        "router_errors_tail": tail(ROUTER_LOG, 12),
    }
    if extra:
        event.update(extra)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(f"[{event['ts']}] {kind} {svc} "
          f"state={cur.get('ActiveState')} pid={cur.get('MainPID')} "
          f"restarts={cur.get('NRestarts')} code={cur.get('ExecMainCode')}"
          f"/{cur.get('ExecMainStatus')}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--once", action="store_true", help="un snapshot y salir")
    args = ap.parse_args()

    state: dict[str, dict] = {}
    print(f"tripwire: interval={args.interval}s out={OUT}", flush=True)

    while True:
        try:
            for svc in llama_services():
                cur = snapshot(svc)
                prev = state.get(svc)
                if prev is None:
                    state[svc] = cur
                    continue

                changed_state = prev.get("ActiveState") != cur.get("ActiveState")
                changed_pid = prev.get("MainPID") != cur.get("MainPID")
                restarts_up = int(cur.get("NRestarts") or 0) > int(prev.get("NRestarts") or 0)

                if changed_state or changed_pid or restarts_up:
                    kind = "DEATH" if cur.get("ActiveState") != "active" else "RESTART"
                    record(kind, svc, prev, cur)
                state[svc] = cur

            if args.once:
                return 0
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"tripwire error: {exc}", flush=True)
            if args.once:
                return 1
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
