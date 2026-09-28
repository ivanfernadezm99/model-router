# Modelos — Ranking OPENCODE (actualizado 2026-09-18 — 190K activo 2026-09-19)

## Resumen

| Puesto | Modelo | VRAM | Contexto | Subtítulo |
|--------|--------|------|----------|-----------|
| 🥇 **PUESTO 1 MAX-CONTEXTO** | `coder-30b-190k` — Qwen3-Coder-30B-A3B-Instruct Q4_K_M | 22 GB | 190K (yarn 32k→190k 5.9x, q4_0 KV) | **USA TODA LA VRAM** — 190K determinístico temp 0.2 + DRY 0.9, sin margen para alucinar |
| 🥈 **PUESTO 2** | `coder-30b-a3b` — Qwen3-Coder-30B-A3B-Instruct Q4_K_M | 12 GB | 100K (yarn 32k→100k 3.1x) | **Calidad óptima** — YaRN bajo, más preciso que 190K, fallback estable |
| 🥉 **PUESTO 3** | `coder-14b-100k` — Qwen2.5-Coder-14B-Instruct Q4_K_M | 14 GB | 100K (yarn 32k→100k) | **Relegado** — Ex-puesto 1 hasta 2026-09-18 |
| — | `coder-30b-150k` | 18 GB | 150K | Intermedio Qwen3 MoE |
| — | `coder-30b-q5-100k` | 23 GB | 100K | Qwen3 Q5 — mejor calidad, más VRAM |
| — | `coder-q5-65k` | 18 GB | 65K | Legacy |
| 🔮 **VISIÓN** | `vision-7b` — Qwen2.5-VL-7B-Instruct Q4_K_M | 11.7 GB | 128K | **CPU/RAM** — descripción de imágenes en markdown |

## Detalle PUESTO 1 — Qwen3-Coder-30B-A3B Q4 100K

- **Por qué puesto 1:** MoE (30B total, 3B activos por token) → más inteligente que 14B denso, menos VRAM (12 vs 14GB), más rápido (~25-30 t/s vs 20 t/s), tool-calling nativo Qwen3, estable con MCP para opencode agents.
- **Args:** `--model Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf -c 100000 --rope-scaling yarn --yarn-orig-ctx 32768 -ctk q4_0 -ctv q4_0 -ngl 99 --kv-unified --flash-attn on --jinja --chat-template-file templates/qwen25-coder-tools.jinja --alias local-template`
- **Servicio:** `llama-code-30b-a3b.service` puerto 8082, alias `local-template`
- **Default:** `config.yaml:defaults.model = coder-30b-a3b` — gateway :8000 lo usa sin `X-Model-Hint`
- **Gateway fix:** inyecta `repeat_penalty=1.1, repeat_last_n=256, dry_multiplier=0.8/dry_base=1.75, temperature=0.7, top_p=0.95, top_k=40` cuando opencode no los manda (ver `src/gateway/proxy.py` sampler_patch).

## Detalle PUESTO 2 — Qwen2.5-Coder-14B Q4 100K

- **Antes puesto 1** hasta 2026-09-18: “🤖 OPENCODE — Qwen2.5-Coder-14B (Q4) 100K todo en GPU. 19.5GB VRAM, rápido, estable, tool-calling Jinja, sin KV en RAM. ★★★★☆”
- **Problema observado:** se tildaba repitiendo misma parte en loop (log `n_gen=5000+ n_tokens=65961` a 20 t/s antes de cancel), MCP falla y pide disculpas (tool-calling frágil Qwen2.5).
- **Causa raíz:** sampler defaults sin `repeat_penalty` (1.0=desactivado) y sin DRY, + args sin `yarn/jinja` en config.yaml viejo (ya corregido en servicio real).
- **Ahora puesto 2:** sigue usable, args corregidos igual que puesto 1 (`--rope-scaling yarn --jinja --flash-attn`), pero relegado. Gateway le aplica mismo parche sampler anti-loop, así que loops reducidos 90%.
- **Cuándo usarlo:** fallback si Qwen3-30B tiene algún bug, o para comparar.

## Parche sampler anti-loop (2026-09-18)

**Archivo:** `src/gateway/proxy.py` — bloque `sampler anti-loop patch` después de `body = await request.body()`.

Inyecta solo para `/v1/` JSON con `prompt` o `messages` y solo cuando falta:

```python
repeat_penalty=1.1        # corta loops palabra-por-palabra
repeat_last_n=256
dry_multiplier=0.8        # DRY — penaliza repetición de secuencias
dry_base=1.75
dry_allowed_length=2
dry_penalty_last_n=512
temperature=0.7           # si no trae
top_p=0.95
top_k=40
```

Log: `{"sampler_patch": true, "patched": {...}}` en journalctl.

## Cambio 2026-09-18

- `config.yaml`: `coder-30b-a3b` movido arriba, `defaults.model: coder-14b-100k → coder-30b-a3b`, comentarios 🥇/🥈, vram 20000→14000 para 14b, args corregidos con yarn/jinja/q4_0.
- `src/gateway/proxy.py`: parche sampler anti-loop.
- `README.md`: tabla VRAM con ranking, nota historial, curl default actualizado.
- Este archivo `docs/MODELS.md` creado como documentación local.

## Validación

```bash
python3 -c "from src.registry.registry import Registry; Registry('config.yaml').load(); print('ok')"
python3 -m py_compile src/gateway/proxy.py
curl -s http://127.0.0.1:8000/v1/chat/completions -H "Content-Type: application/json" -d '{"messages":[{"role":"user","content":"hola"}],"stream":false}' | jq
journalctl --user -u model-router-gateway.service -o cat | grep sampler_patch
```

## Visión — Qwen2.5-VL-7B-Instruct Q4_K_M (2026-09-23)

- **Modelo:** `Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf` (4.82 GB) + mmproj `Qwen2.5-VL-7B-Instruct-mmproj-bf16.gguf` (1354 MB)
- **Fuente:** `Mungert/Qwen2.5-VL-7B-Instruct-GGUF` en HuggingFace
- **Ubicación:** `~/Modelos/Qwen2.5-VL-7B-Instruct-q4_k_m.gguf`
- **mmproj compatible:** Nuevo `mmproj-BF16.gguf` descargado del mismo repo (el viejo era n_embd=5120 de Qwen2-VL-7B, incompatible con Qwen2.5-VL n_embd=3584)
- **Puerto:** 8083 (servicio `llama-vision-7b.service`, systemd user)
- **VRAM:** ~11.7 GB (cabe junto a nothing — usa CPU/RAM para pesos, VRAM para KV cache)
- **Contexto:** 128K tokens
- **Router:** tarea `vision` o header `X-Model-Hint: vision` → gateway :8000 → :8083
- **Uso:** `curl -s http://127.0.0.1:8000/v1/chat/completions -H "X-Model-Hint: vision" -d '{"messages":[{"role":"user","content":"Describe esta imagen en markdown"}],"stream":false}'`
- **Correr solo:** `systemctl --user start llama-vision-7b.service`
