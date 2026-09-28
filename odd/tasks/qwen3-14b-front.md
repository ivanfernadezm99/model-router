# Feature: Qwen3-14B disponible desde el front

## Objetivo
Descargar Qwen3-14B Q4_K_M (~9GB) y dejarlo seleccionable desde el front del model-router.

## Problema
El usuario pidió Qwen3 14B (~9-10GB). Hoy solo existe Qwen2.5-Coder-14B (8,4GB) + Qwen3 30B/32B. No hay ningún Qwen3-14B en /home/servidor/Modelos/.

## Por qué
Qwen3-14B denso rinde muy bien para code/chat general con menos VRAM que los 30B MoE. El usuario lo pidió explícito con ⭐⭐⭐⭐⭐.

## Alcance autorizado
- Descargar GGUF a /home/servidor/Modelos/Qwen3-14B-Instruct-Q4_K_M.gguf (fuente: bartowski/Qwen_Qwen3-14B-GGUF)
- Agregar entrada `qwen3-14b-100k` en /home/servidor/Descargas/model-router/config.yaml (raíz, autoritativa)
- Crear systemd/user/llama-code-qwen3-14b.service (puerto 8082, ctx 100k, YaRN como coder-14b-100k)
- Agregar descripción en web/app.py MODEL_DESCS
- daemon-reload + restart gateway para que /api/models lo liste. NO descargar otros quantes. NO tocar modelos existentes.

## Criterios de aceptación
- [ ] Archivo GGUF existe y pesa ~9GB
- [ ] config.yaml contiene qwen3-14b-100k
- [ ] service file existe y daemon-reload ok
- [ ] GET /api/models (front) o /jobs/models/list (gateway) lista qwen3-14b-100k
- [ ] switch a qwen3-14b-100k levanta /health 200 en :8082

## Checks aplicables
- `ls -lh /home/servidor/Modelos/Qwen3-14B-Instruct-Q4_K_M.gguf`
- `python3 -c "import yaml; print('qwen3-14b-100k' in yaml.safe_load(open('config.yaml'))['models'])"`
- `systemctl --user is-active llama-code-qwen3-14b.service` (tras switch)
- `curl -s http://127.0.0.1:8000/jobs/models/list | head -c 2000`

## Progreso
- [x] Exploración: registry=Root config.yaml, orchestrator usa systemctl --user, front lee /jobs/models/list + MODEL_DESCS
- [x] Alta en config (qwen3-14b-100k) + service + front desc (commit cableado)
- [x] Descarga GGUF completa 8,4GB (Qwen/Qwen3-14B-GGUF oficial, Q4_K_M)
- [x] Verificación: switch ok, :8082 /health 200, inferencia "QWEN3 OK" vía gateway /v1

## Evidencia
- Commit cableado: 327bba4
- Commit verificación: este
- Gotcha: los .service del repo hay que copiarlos a ~/.config/systemd/user/ + daemon-reload; sin eso el switch queda colgado en "switching" (le pasó al primer intento, se resolvió reiniciando el gateway y reintentando)

## Evidencia
- Ruta: delegated direct, writer trigger (2+ archivos no triviales + descarga)
- TDD: n/a (infra/descarga, checks funcionales arriba)
- Commits: work-unit commit en feature branch tras cada tarea
