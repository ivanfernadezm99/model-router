# Switches de operación (banderas estables)

Las banderas viven en el unit `systemd/user/model-router-gateway.service`
(`Environment=...`) y en su copia instalada `~/.config/systemd/user/`.
**Sobreviven reboots.** Solo se cambian cuando el operador lo pide
explícitamente. Nada de `set-environment` (eso era volátil y ya se limpió).

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

## Rehabilitar / cambiar (solo a pedido explícito)

```bash
# editar el unit (repo + copia instalada, o solo instalada + copiar al repo)
nano ~/.config/systemd/user/model-router-gateway.service
# Environment=LOOP_GUARD_DISABLED=1  ->  =0 (o borrar la línea)
# Environment=AUTO_SWITCH_DISABLED=1  ->  =0 (o borrar la línea)
cp ~/.config/systemd/user/model-router-gateway.service systemd/user/
systemctl --user daemon-reload
systemctl --user restart model-router-gateway.service
```

## Invariante: un solo modelo en VRAM (documentado, no se toca)

- El gateway en modo `AUTO_SWITCH_DISABLED` nunca inicia un segundo modelo:
  proxea al activo sano o devuelve 409. No hay `start` sin `stop` previo.
- Los jobs (`src/jobs/worker.py → orchestrator.switch_to`) siguen con
  `stop-before-start` exclusivo: paran el actual ANTES de arrancar el nuevo.
- Prohibido correr dos backends a la vez a mano (ver README: rompe el
  invariante y deja 24GB ocupados → OOM).
