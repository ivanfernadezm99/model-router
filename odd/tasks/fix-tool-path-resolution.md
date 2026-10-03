# Feature: Arreglar resolución de paths en tool calls (chat template)

## Objetivo
Que cuando opencode le pasa un path a un modelo, el path llegue intacto a la herramienta. Hoy llega corrupto o el historial ni se renderiza.

## Problema
`templates/qwen25-coder-tools.jinja` — usado por 7 unidades systemd (todos los coder-30b, coder-14b y qwen3-14b) — tiene dos bugs verificados ejecutando el template:

1. **Doble encoding de `arguments`.** Línea 34 aplica `| tojson` sobre `tool_call.arguments`, que en formato OpenAI ya es un *string* JSON. `tojson` lo vuelve a serializar como string escapado. Resultado: `"arguments": "{\"filePath\":\"...\"}"`. El system prompt del mismo template (línea 14) le dice al modelo que emita un *objeto*. Instrucciones y ejemplos se contradicen en el mismo contexto.

2. **Crash con content blocks.** Línea 24 hace `'<|im_start|>' + message.role + '\n' + message.content`. Si `content` es una lista de bloques (formato Anthropic / `tool_result`), lanza `TypeError: can only concatenate str (not "list") to str`. No hay ningún manejo de bloques.

3. **Sin instrucción de path absoluto.** El modelo tiene自由度 para inventar paths relativos cuando el usuario da uno absoluto.

## Por qué
Los agentes (opencode → `http://127.0.0.1:8000/v1`, confirmado en `~/.config/opencode/opencode.json:424`) no fallan al buscar: fallan porque el path se degrada en el template o el historial nunca se renderiza.

## Alcance autorizado
- Reescribir `templates/qwen25-coder-tools.jinja` con macros de normalización de `content` (str / lista de bloques) y de `arguments` (str JSON → objeto).
- Manejar `tool_result` y `tool_use` en content blocks.
- Agregar instrucción de path absoluto al system prompt del template.
- Agregar `tests/test_chat_template.py` con el payload real de opencode.
- NO tocar el sampler de `src/gateway/proxy.py` (bug separado, requiere decisión propia).
- NO tocar las unidades systemd ni `config.yaml`.

## Criterios de aceptación
- [ ] El template renderiza sin excepción con payload Anthropic (content como lista de bloques)
- [ ] `arguments` se renderiza como objeto JSON, no como string escapado
- [ ] `tool_result` de un turno previo aparece en el prompt renderizado
- [ ] El system prompt y los ejemplos coinciden en el formato de `arguments`
- [ ] El path absoluto que emite el modelo sobrevive round-trip

## Checks aplicables
- `python3 -m pytest tests/test_chat_template.py -q`
- `python3 -m pytest tests/ -q` (no romper nada)
- Round-trip manual: renderizar con un tool_call que contiene un path y verificar que el path aparece literal

## Progreso

### T1 — Reescribir el template con normalización de content y arguments — DONE
- Route: inline (1 archivo no trivial + 1 test mecánico, contexto completo ya leído)
- Trigger: `writer rule` no dispara — el template es un solo archivo ya entendido; el test es mecánico.
- Commit: `9d4f938` en rama `fix/tool-path-resolution`
- Archivos: `templates/qwen25-coder-tools.jinja`, `tests/test_chat_template.py`

**Evidencia de verificación**
- `python3 -m pytest tests/test_chat_template.py -q` → 18 passed
- `python3 -m pytest tests/ -q` → 76 passed, 6 failed. Los 6 fallos son PREEXISTENTES: se.corrieron sobre el árbol limpio con `git stash` y fallan idéntico (`test_e2e_queue`, 2× `test_jobs_wiring`, 3× `test_opencode_valuation`). Ninguno toca el template.
- Render manual con payload Anthropic: `arguments` sale como objeto `{"filePath": "/home/servidor/x/proxy.py"}` y el `tool_result` sale envuelto en `<tool_response>`.

## Criterios de aceptación — estado
- [x] El template renderiza sin excepción con payload Anthropic (content como lista de bloques)
- [x] `arguments` se renderiza como objeto JSON, no como string escapado
- [x] `tool_result` de un turno previo aparece en el prompt renderizado
- [x] El system prompt y los ejemplos coinciden en el formato de `arguments`
- [x] El path absoluto que emite el modelo sobrevive round-trip

## Pendiente (NO autorizado, requiere decisión aparte)
- **Sampler override** en `src/gateway/proxy.py:99-157`: pisa la sampling del cliente en 1424/1424 requests con `dry_allowed_length: 2`, `repeat_penalty: 1.15`, `top_k: 20`, `temperature: 0.2`, `top_p: 0.85`. DRY con `allowed_length: 2` penaliza la repetición — y un path ES repetición. Es el segundo mecanismo que arruina paths aun cuando el template ya está bien.
- **Typo `-np 4--kv-unified`** en `~/.config/systemd/user/llama-code-30b-a3b.service` (instalada, no solo el repo). `coder-30b-a3b` no arranca.
- **Template Qwen2.5 sobre modelos Qwen3-Coder**: los `coder-30b-*` son Qwen3-Coder pero usan `qwen25-coder-tools.jinja`. Los tags nativos de tool call de Qwen3 difieren. Decisión de modelo, no de template.
- `systemd/user/` desincronizado de `~/.config/systemd/user/`: faltan `llama-code-30b-150k.service`, `llama-code-30b-190k.service` (el default) y `llama-code-30b-q5-100k.service` en el repo.

