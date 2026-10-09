# Fixes switch / jobs — racha 2026-10-04 → 2026-10-06

## Síntomas (lo que veía el usuario)

| Síntoma | Causa real |
|---|---|
| Carga 30B-190K queda en 50% / `failed` | Switch exclusivo posterior la mató a los 2s para subir otro modelo en `:8082` |
| `failed 0% cancelado por el usuario` sin haber cancelado | `DELETE /jobs/{id}` de otro tab/dispositivo Tailscale (front no tiene auto-cancel; verificado en JS) |
| Job video `running 60%` congelado | Worker trabado en `switch_to` (240s); backend `:8082` vacío |
| Job video failed: `systemctl start wan-video timed out after 15s` | Parar 13GB tarda ~2min; timeout fijo 15s |
| Front: `Read timed out (15s)` al Cargar | `subprocess.run` sincrónico bloqueaba el loop de uvicorn |
| `Bad Gateway: backend unavailable` con visión cargada | Memoria rancia (`coder-30b-150k` muerto) → passthrough al `:8082` vacío |
| `409 auto-switch disabled: cargá un modelo` con 190K sano arriba | Probes de 2-3s no ven el `/health` del 190k bajo carga |

## Cambios

- `src/orchestrator/lifecycle.py`
  - `_SYSTEMCTL_TIMEOUTS`: stop 150s / start 60s / is-active 10s (antes 15s todo).
  - `_run_systemctl_async()` vía `asyncio.to_thread`: systemctl ya no bloquea el loop.
  - `SWITCH-ADOPT`: si el servicio del target está activo y su health (`loaded:true` en wan) responde, se adopta sin restart. Cura amnesia post-restart del gateway.
- `src/gateway/adopt.py`
  - `drop_stale_active()`: limpia `active_model` cuya unidad systemd murió (solo sin swap en curso). Se corre en `/health` y cada `/v1`.
  - `active_unit_names()`: lista unidades del registry activas (best-effort, para el 409).
- `src/gateway/app.py`
  - `vision_passthrough`: chats `code` van al `vision-*` activo (el VL responde texto).
  - Probes de adopción 2-3s → 8s.
  - El 409 incluye `units_up` (qué systemd tiene arriba).
- `src/jobs/router.py` + `web/app.py`: log `AUDIT` en cada cancel (`DELETE /jobs/{id}`) con IP + user-agent.
  - Ver: `journalctl --user -u model-router-gateway.service | grep AUDIT`
- `web/app.py`: `/api/switch` timeout 15s → 30s.
- Tests: `test_e2e_queue` (adopt), `test_jobs_wiring` (namespace `lifecycle.poll_health` + mock red `wait_port_free`/`wait_http_200`), `test_orchestrator` + `test_wan_warmup` (fakes aceptan `timeout`), `test_gateway` (aísla systemd real), `test_adopt` (+2 tests drop_stale). Suite: **188 passed**.

## Lecciones

- `reconcile_active_model` solo llenaba `None`: nunca corregía memoria rancia. Segundo-guessear está bien si la unidad está provadamente muerta y no hay swap en curso.
- `subprocess.run` en un handler async = loop muerto por N segundos. Todo systemctl va por `to_thread`.
- llama-server con 190k ctx tarda >2s en `/health` bajo carga: probes cortos mienten "caído".
- Tests que tocan red real (`wait_http_200` 180s) o systemd real cuelgan o flakean la suite: mockear en namespace `lifecycle.*`.
- `fuser -k` en tests es peligroso: mockear `_kill_port_occupants`/`wait_port_free` siempre.

## Qwen-Image-Edit — probado y descartado (2026-10-07)

- **Test 1 (52 precios de una pasada):** layout OK, varios precios bien, pero texto deformado ("Papas Oheddar y Pamela"), ítem 51 mal ($16.000 vs $12.000), numeración corrida.
- **Test 2 (solo sección Hamburguesas, 15 precios):** tocó precios que se le dijo no tocar (ítem 28 $10.000 vs $30.000, ítem 46 $28.000 vs $25.000, ítem 26 $86.000 vs $8.000) y metió comas ($16,000).
- **Conclusión:** la difusión no da exactitud contable. Pesos borrados (`qwen-image-edit-2511-Q4_K_M.gguf` 13.2GB + outputs + workflow de test). Se conserva encoder VL fp8 + VAE (sirven para Qwen-Image base text-to-image).
- Workflow de referencia quedó en: `docs` (este archivo) — el JSON de test se eliminó con los pesos.

## Qwen-Image base — generador publicitario (2026-10-07)

- **Pesos:** `models/diffusion_models/qwen-image-Q4_K_M.gguf` (13.1GB, unsloth) + mismo encoder VL fp8 + VAE.
- **Test (aviso Gera Burguer 2x1, 40 steps euler/simple cfg 4.0, 13 min en 3090):** imagen `gera-ad-test_00001_.png` — "ECO ESPECIAL" y "$10.000" perfectos, "GERA" parcial, "Solo esta semenia" (semana con glitch). Útil para publicidad creativa; texto largo con algún detalle a pulir (reintentar seed o más steps).
- **Pendiente:** cablear opción `image` del front a este pipeline.

## image → Qwen-Image cableado al combo (2026-10-07)

- **Registry:** `qwen-image` (sdxl.service :8188, vram 22000). Front: `🆕 NUEVO image — FOTO FIJA...` + hints que distinguen foto vs video.
- **Worker:** `_run_comfyui_t2i()` (workflow GGUF Q4 + poll history + descarga a OUTPUT_DIR). `image` mapea a `qwen-image`.
- **Batería:** 192 passed (incl. `test_qwen_image_job.py` nuevo) + validación real: job `098ef89a` completed en ~7 min por el camino completo (switch + generate + file).

## Doble orchestrator — la incoherencia raíz (2026-10-07)

- **Síntoma:** `/health` decía `coder-14b-100k` y `/jobs/models/list` decía `qwen-image`, mismo proceso, estable en el tiempo.
- **Causa:** el gateway corre como `python3 -m src.gateway.app` (`__main__`), pero `worker._get_orchestrator` hacía `from src.gateway.app import orchestrator` → Python re-ejecuta el módulo → DOS orchestrators. Todo `/jobs/*` usaba la sombra; `/v1` y `/health` la servida.
- **Fix:** `_get_orchestrator` prefiere `sys.modules["__main__"]` cuando tiene orchestrator+registry (en tests cae al módulo). Tests de regresión incluidos. Suite: 194 passed.
- Verificado: health/list/debug con mismo id y mismo modelo.

## Overlays exactos (texto por código) — 2026-10-07

- **Flujo:** `image` con `overlays[{slot,text}]` → fondo SIN texto (Qwen) + PIL pega título/subtítulo/contacto/marca. Slots: title, subtitle, contact, brand. Emojis sanitizados (tofu), ñ/acentos OK (DejaVu Bold).
- **Test real job `45e3b2ce`:** OCR verifica subtitle, contact y brand LETRA POR LETRA exactos. 196 tests (overlay unit + slot inválido 400).

## Chat real en avisos (2026-10-07)

- **Flujo:** `messages[{who,text}]` (bot=verde derecha, cliente=blanca izquierda, máx 12) → burbujas WhatsApp sobre la foto. Front: textarea (formato `bot: ...` / `cliente: ...`).
- **Test job `a0455c42`:** OCR verifica título, subtítulo, contacto, marca y mensajes del bot exactos. 198 tests.
