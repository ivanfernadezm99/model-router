# MEMORY — model-router

Suplanta local de Engram. Engram MCP esta caido en este repo con
`multiple active runtime sessions match the current project and directory`.
Mientras siga asi, este archivo es la unica memoria persistente.

Rama de trabajo: `fix/tool-path-resolution` -> `origin/fix/tool-path-resolution`.

---

## 2026-10-03 — Tool calls de llama.cpp arreglados y verificados

**Que**: `tool_calls` estructurados funcionando con path absoluto intacto.
Commit `fe0474f`. Verificado directo en `:8082` y via gateway en `:8000`:
`finish_reason: tool_calls`, `{"filePath": "/home/servidor/Descargas/model-router/src/gateway/proxy.py"}`,
`content` vacio. `147 passed`.

**Por que**: el modelo emitia el marker correcto como texto pero
`message.tool_calls` llegaba vacio.

**Donde**: `templates/qwen3-coder-tools.jinja`,
`templates/qwen25-coder-tools.jinja`, `tests/test_template_envelope.py`,
`tests/test_chat_template.py`, `odd/tasks/fix-tool-path-resolution.md`.

**Aprendido**:
1. `~/llama.cpp/build/bin/test-chat-template <archivo> --with-tools` acepta un
   template arbitrario y tiene el debug prendido (`JJ_DEBUG` es flag runtime, no
   `NDEBUG`). Imprime el error exacto de minja en **milisegundos**. Use esto para
   depurar templates; reiniciar el modelo cuesta ~70s por iteracion.
2. minja corre VARIOS probes con payloads de `tools` distintos. Cuando una
   propiedad falta, el valor es `Undefined`, y ahi **no existen** ni `default`
   ni `tojson`. Guardar con `{%- if x is defined and x -%}`.
3. `is sequence` da `true` para un String (trampa clasica de Jinja: un string
   es una secuencia). Guard: `is sequence and is not string`.
4. **`common/chat.cpp` NO tiene U+200B en ningun archivo.** Matchea
   `<tool_call>` plano. Un ZWSP en el template enseña al modelo a emitir un
   marker que el parser no puede matchear.
5. **La leccion de fondo**: jinja2 degrada en silencio donde minja revienta. Una
   suite completa pasando contra jinja2 no dice NADA sobre si el template
   servido funciona. Probar contra minja.
6. **U+200B se pierde SIEMPRE al escribirlo en un heredoc de bash.** Verificar
   con `chr(0x200B)`, nunca con el literal; `count('')` devuelve `len+1` y
   parece OK.
7. Para probar si un marcador es real, `hexdump -C` sobre la linea del parser.
   Un `grep` visual no distingue U+200B de nada.

---

## 2026-10-03 — 6 tests preexistentes arreglados

Commit `786f40d`. Uno era **bug real de produccion**:

- `lifecycle.switch_to`: `stop_current()` corria antes del check de coalescencia
  y ponia `active_model=None`, asi que el check nunca matcheaba. Pedir el modelo
  ya activo lo detenia y lo volvia a arrancar, y reportaba exito. Early-return
  antes de tocar systemd.
- `test_jobs_wiring`: el worker salta el backend si `PYTEST_CURRENT_TEST` esta
  seteado, no genera archivo, y `has_file` lo marca `failed` — correctamente.
  El assert de `completed` era inalcanzable. Inyectar el archivo en el seam
  `_move_file_to_output`.
- `test_opencode_valuation`: asserts del mundo pre-7824057. Ese commit promocio
  `coder-30b-190k` y relego el 14B **a proposito**. Tests reescritos.

**Aprendido**: ante un test preexistente rojo, preguntarse si el TEST o el
CODIGO estan mal. `git log -S<simbolo>` sobre el simbolo fantasma revela si fue
un cambio deliberado. Editar el codigo para contentar el test habia revertido
una decision del usuario.

---

## 2026-10-03 — Guard de minja en la suite

`tests/test_template_minja.py` corre el harness real de llama.cpp
(`test-chat-template --with-tools`) contra ambas plantillas y afirma sobre el
bloque de capabilities. 9 tests, ~1s con templates sanos.

- Afirma `supports_tools`, `supports_tool_calls`, `supports_object_arguments`
  en `true`, mas cero lineas `Error executing`.
- Incluye `test_guard_can_actually_fail`, que verifica con un template sin tool
  handling que los caps dan `false`. Sin ese test, el guard podria quedar
  vacuo y pasar siempre.
- Se saltea solo si el harness no esta compilado, asi la suite corre igual en
  una maquina sin build local de llama.cpp.
- Verificado que atrapa regresiones: revirtiendo el guard
  `is not string` el suite falla 4 tests.

**Aprendido**: el exit code del harness es 0 incluso con template roto
(`{{ x | trim }}` sobre undefined no ejecuta el filtro). La unica senal util son
los caps y las lineas `Error executing`.

---

## Deuda abierta (deliberada, NO tocar)

- `TASK_TO_MODEL["code"]` sigue en `coder-14b-100k` mientras `config.yaml`
  declara `coder-30b-190k`. Decision del usuario: **manual por ahora, sin
  automatizar**. No tocar `src/gateway/detector.py`.
- `/health` reporta `coder-14b-100k` aunque la unidad activa sea
  `llama-code-30b-190k.service` (varios modelos comparten 127.0.0.1:8082).
- `rustdesk.service` falla en la maquina. No relacionado.
- La unidad del modelo responde 503 durante ~30s tras un restart porque carga
  un modelo 30B. Para decidir "esta listo?", usar `GET /props` y no `/health`:
  /health da 503 mientras carga, despues da 200 pero todavia sin
  `chat_template_caps` hasta que el probe del template termina.
- **EN vs ES**: el historial de este repo paso a usar ingles en commits y
  comentarios de codigo. Commits y codigo en ingles; la respuesta al usuario en
  espanol.

## Entorno

- Las unidades systemd referencian los templates por path al repo
  (`%h/Descargas/model-router/templates/...`). **No** hay copias en
  `~/.config/systemd/user/`; buscar "drift" ahi da falsos MISSING.
- No tocar ni commitear `.atl/`, `.playwright-mcp/`, `logs/`.
- Artefactos tecnicos en ingles; comunicacion con el usuario en espanol.
- Nunca agregar trailer `Co-Authored-By` a los commits.
