# Preferencia LLM local 2.5 — rápido pero exigente (2026-10-09)

Pedido explícito del usuario: el Qwen2.5-Coder-14B local es el favorito por
velocidad (todo en GPU, ~30 tok/s). Lo que le falta es disciplina: entregar
los trabajos completos como se le pide, sin vagancia.

## Diagnóstico verificado

- `llama-server` en :8082 vivo, `health: ok`, inferencia OK, VRAM 14.3/24 GB.
- Sampler defaults en `src/gateway/proxy.py` YA existen: `repeat_penalty`
  1.05 (agente) / 1.15 (código), `repeat_last_n` 256, DRY solo para código
  no-agente, `temperature` 0.2 código / 0.6 texto.
- PERO: las llamadas directas a :8082 (opencode, workers sdd) **bypasean el
  gateway**, así que el parche no las alcanza. La vaguedad en workers es
  problema de **prompt**, no de sampler.

## Decisión

1. No tocar el sampler del gateway (ya cumple su función para tráfico /v1).
2. Endurecer el prompt de workers: exigir inventario con paths reales
   verificados o FAIL, Result Contract completo, comparación de enfoques.
3. Reintentar explore del gimnasio con ese prompt duro.

## Verificación en producción (2026-10-09)

Usuario confirmó que FUNCIONÓ en su otra sesión abierta contra el gateway
reiniciado (proceso nuevo 19:18, commit `2211521`).

- Alcance final: solo Qwen2.5-Coder (`_is_qwen25_coder` por archivo de modelo
  en args del registry — cubre coder-14b-100k/150k/190k/250k-ram y futuros).
- Qwen3, 30B, vision y resto: intactos, `1.0` explícito se respeta.
- Restart gateway: `XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user
  restart model-router-gateway.service` (sin sudo).

## Nota Engram

`mem_save` falló esta sesión (múltiples sesiones runtime activas,
error de ambigüedad). Reintentarlo desde una sesión limpia con
`project: model-router`.
