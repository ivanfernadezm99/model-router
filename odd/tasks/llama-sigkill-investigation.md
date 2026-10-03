# Investigación: SIGKILL al servicio llama-code-30b-190k

**Fecha:** 2026-10-03
**Síntoma:** `llama-code-30b-190k.service` muere con
`Main process exited, code=killed, status=9/KILL` de forma periódica.
**Estado:** causa raíz **NO** confirmada. Hipótesis del OOM **descartada**.

---

## 1. Timeline de las 6 ocurrencias

| Hora (oct 03) | Evento |
|---|---|
| oct 02 22:37:33 | SIGKILL |
| oct 02 23:25:05 | SIGKILL |
| oct 03 01:30:23 | SIGKILL |
| oct 03 01:32:25 | SIGKILL |
| oct 03 01:35:35 | SIGKILL |
| oct 03 09:07:03 | SIGKILL (con contexto 150K) |

Pre-existente: las dos primeras son anteriores a la sesión de trabajo.

## 2. Lo que se DESCARTÓ (con evidencia)

### OOM killer del kernel — DESCARTADO
`/var/log/kern.log` es legible (grupo `adm`) y está actualizado
(última línea 09:21:44). Contiene mensajes del kernel — hay ~130k líneas
con eventos `UFW BLOCK`. **Cero eventos de OOM.**

Si el OOM killer hubiera actuado, kern.log tendría
`Out of memory: Killed process ...`. No está.

> Trampa: `grep -ciE "oom" /var/log/syslog` da **354 falsos positivos**.
> Todos son `llama-server: alloc: - making room for prompt cache entry` —
> el "oom" está dentro de "room". Todos de septiembre.

### systemd-oomd — DESCARTADO
`systemctl is-active systemd-oomd` → `inactive`.

### IdleReaper del gateway — NO ES
`src/orchestrator/idle.py` dispara con `elapsed >= 600s` y modelo activo.
Usa `orchestrator.stop_current()` → `systemctl --user stop` → **SIGTERM**,
no SIGKILL. No hay líneas "idle reaper firing" en el journal del gateway.

### systemd escalando por timeout — NO
Un escalamiento de `TimeoutStopSec` loguearía `State 'stop-sigterm' timed
out. Killing.` No aparece. El único "Stopping" cerca de 09:03:58 fue el
`systemctl restart` manual mío.

### root / sudo — NO
`/var/log/auth.log` no muestra ningún sudo contra llama-server. La única
actividad root reciente es `rustdesk --server` a las 09:01:11.

## 3. Lo que se ENCONTRÓ

### El único SIGKILL del código es `fuser -k`

`src/orchestrator/lifecycle.py:88` → `_kill_port_occupants(port)`:

```python
subprocess.run(["fuser", "-k", f"{int(port)}/tcp"], timeout=15)
```

`fuser -k` manda **SIGKILL** por defecto. Es el único mecanismo en el repo
capaz de producir `status=9/KILL`.

Se llama en `lifecycle.py:219`, cuando `wait_port_free()` falla tras
intentar parar todo lo que conoce en el puerto del target.

### Correlación 6/6 con actividad de switch

`logs/router-errors.log` — cada SIGKILL tiene, en el mismo segundo:

```
<hora-1s> | Backend 5xx port 8082 | request_id=... status=500
<hora>     | Switch timeout: sdxl | health 8188/system_stats no respondió en 1200s
<hora>     | Switch bloqueado: holds faltan | target=sdxl — cuda/kornia no disponibles
```

Ejemplos verbatim:
- `2026-10-03 01:30:22` 500 → `01:30:23` SIGKILL
- `2026-10-03 01:32:25` 500 → `01:32:25` SIGKILL
- `2026-10-03 09:07:02` 500 → `09:07:03` SIGKILL

**6 de 6.** La correlación es perfecta.

### El riesgo estructural en `switch_to`

`lifecycle.py:194-202` — `to_stop` incluye el servicio activo:

```python
to_stop = {spec["service"]}
if self.active_service:
    to_stop.add(self.active_service)          # <- el 30B entra acá
for _name, _spec in (self.registry.models or {}).items():
    if int(_spec.get("port")) == port:        # port = port del TARGET
        to_stop.add(_spec["service"])
```

13 modelos comparten el puerto 8082. Un switch cuyo target esté en 8082
mete a **todos** los coder en `to_stop`. Si `wait_port_free` falla después,
`_kill_port_occupants(8082)` hace `fuser -k 8082/tcp` → SIGKILL al 30B
y a cualquier coder vivo.

## 4. Lo que NO se pudo probar

El mecanismo exacto por el que el 30B recibe SIGKILL **no está cerrado**.
La correlación con los switch a `sdxl` (port 8188) es perfecta, pero
`sdxl` está en 8188 y `_kill_port_occupants` se invoca con el puerto del
target — 8188, no 8082. Para que el 30B muera por esa vía hace falta un
switch concurrente a un target en 8082 que no aparece en
`router-errors.log`.

Descartado: OOM, systemd-oomd, IdleReaper, escalamiento por timeout, root.
Queda: `fuser -k` desde otro camino, o un agente externo no identificado.

## 5. Cambio aplicado (y por qué ya no se sostiene)

Se bajó el contexto 190000 → 150000 (`43c3e74`) **por hipótesis de OOM
que quedó desmentida**. El modelo siguió muriendo (09:07:03).

Medición real del GGUF: 48 capas, 4 KV heads, key/value 128, KV en
`q4_0` + `--no-kv-offload` → ~27 KiB/token → KV de 4.90GiB a 3.87GiB.
RSS real de llama-server: **5.68GiB → 4.37GiB (-1.31GiB medido)**.

Archivos tocados:
1. `systemd/user/llama-code-30b-190k.service` — `-c 150000`, `Description=` advertised
2. `config.yaml` → `coder-30b-190k` — `-c`/`--ctx-size` 150000, `vram_mb` 18000, comment
3. `config.yaml` → `defaults.model` — comment dice "contexto REAL 150K"

El cambio **no arregla el SIGKILL**. Mantenerlo o revertirlo es decisión
del usuario; los tests pasan en ambos casos.

## 6. Deuda de nombres

La clave `coder-30b-190k` y el nombre del unit NO se renombraron para no
romper `defaults.model`, `web/app.py`, `tests/test_adopt.py`,
`tests/test_gateway.py`, `tests/test_opencode_valuation.py`.

**`coder-30b-150k` ya existe** y su unit es byte-por-byte idéntico al de
190k salvo el `-c 150000`. Es el destino del renombre limpio.

## 7. Metodología — no hizo falta root

El usuario mencionó que la contraseña de admin está en Engram. **No hizo
falta**: el usuario `servidor` pertenece al grupo `adm`, que da lectura
sobre `/var/log/kern.log` y `/var/log/syslog`. Eso permitió descartar el
OOM killer sin `sudo`. (Engram MCP además está caído con
`multiple active runtime sessions match the current project and directory`.)

## 8. Próximos pasos propuestos

1. Auditar todos los call sites de `_kill_port_occupants` y hacer que un
   switch a un target en 8082 **no** arrastre a los coder hermanos al
   `fuser -k`, o al menos loguear el `port` y el PID matado antes de matar.
2. Instrumentar `_kill_port_occupants` con un log explícito
   (`port`, `pids`, `motivo`) para que el próximo SIGKILL sea atribuible.
3. Revisar si los switch a `sdxl` que fallan por `1200s` deberían tener un
   timeout menor y no arrastrar al modelo activo.
4. Averiguar qué pide un 500 en 8082 justo antes de cada muerte — puede ser
   la causa(queue cheia, request inválido) y no el síntoma.
