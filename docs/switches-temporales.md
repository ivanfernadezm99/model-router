# Switches temporales deshabilitados (2026-09-30)

Todo lo de esta página es **temporal** y vive solo en memoria del user manager
(`systemctl --user show-environment`). **Se pierde al reiniciar la máquina.**
El código de los toggles está commiteado en `master` (`src/gateway/app.py`).

## Qué está apagado

| Toggle | Efecto | Por qué |
|---|---|---|
| `LOOP_GUARD_DISABLED=1` | No carga `LoopGuardMiddleware`: el gateway no corta por `LOOP_DETECTED`, límites de iteración/reintentos ni prompt idéntico 3x | Cortaba reintentos MCP legítimos |
| `AUTO_SWITCH_DISABLED=1` | Ante mismatch target≠activo: proxea a lo que esté cargado (si responde health) o devuelve **409** pidiendo carga manual. Nunca hace `stop-before-start` solo | El switch on-demand tumbó el 27B a las 22:45 al llegar un request de código |

## Operación manual mientras tanto

- El modelo lo elegís vos desde el front (Cargar) o con
  `systemctl --user stop <viejo> && systemctl --user start <nuevo>`.
- Si un request pide otro modelo y no hay nada sano en el puerto → `409
  {"error": "auto-switch disabled: cargá un modelo manualmente desde el front"}`.
- Los jobs de video/imagen (`src/jobs/worker.py`) siguen llamando
  `switch_to` directo: esos SÍ cambian de modelo aunque el gateway no lo haga.

## Rehabilitar

```bash
systemctl --user unset-environment LOOP_GUARD_DISABLED AUTO_SWITCH_DISABLED
systemctl --user restart model-router-gateway.service
```

Ver estado: `systemctl --user show-environment | grep -E "LOOP|AUTO"`.
