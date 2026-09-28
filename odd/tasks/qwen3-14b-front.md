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
- Commit verificación: ddfc3da + 28e6860
- Gotcha: los .service del repo hay que copiarlos a ~/.config/systemd/user/ + daemon-reload; sin eso el switch queda colgado en "switching" (le pasó al primer intento, se resolvió reiniciando el gateway y reintentando)
- Incidente: a mitad del trabajo el :8082 quedó sirviendo el Qwen2.5 viejo (posible switch manual desde el front). Se detectó por `ps` (cmdline mostraba Qwen2.5-Coder + -np 1) y se corrigió con switch del gateway. Lección: verificar identidad por /props model_path, no por tests de eco.
- Concurrencia: -np 1 → -np 4 SOLO en config+service propios de qwen3-14b-100k. Verificado: /slots=4, /props model_path=Qwen3-14B-Instruct-Q4_K_M.gguf, 4 reqs concurrentes en 4.1s total. n_ctx por slot = pool completo 100096 (unificado, no dividido en 25K).
- BLOQUEO ARQUITECTURA: validate_np (src/common/validation.py) rechaza -np>1 y tumba el registry entero (0 modelos, gateway degradado). Renombre a qwen3-14b-190k + np8 causó el incidente; restaurado a 190K/np1. 190K verificado: n_ctx 190208, VRAM 17.8GB/24GB. Paralelismo multi-slot requiere decisión explícita del usuario (cambiar la guarda).

## Evidencia
- Ruta: delegated direct, writer trigger (2+ archivos no triviales + descarga)
- TDD: n/a (infra/descarga, checks funcionales arriba)
- Commits: work-unit commit en feature branch tras cada tarea
