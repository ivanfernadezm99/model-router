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

## AUTO_SWITCH_DISABLED ya estaba activo — el bug de TASK_TO_MODEL era teorico

- `AUTO_SWITCH_DISABLED=1` esta en la unidad del repo, en la instalada y en el
  proceso corriendo. El `/v1` **nunca** cambia de modelo.
  Verificar con: `tr '\0' '\n' < /proc/$(systemctl --user show -p MainPID
  --value model-router-gateway.service)/environ | grep AUTO_SWITCH`.
- `TASK_TO_MODEL["code"]` ya era inofensivo: sobrevive solo como etiqueta en
  logs y en el campo `target` del 409. No lo borres — `app.py` lo usa en el
  fallback image/video y en `_resolve_port("code")`.
- El agujero real era otro: con un modelo cargado que NO corresponde a la tarea
  pedida, el request se proxyeaba al modelo equivocado y devolvia **HTTP 200 con
  una respuesta de chat** (pedir imagen daba `"¡"` del modelo de codigo).
  Corregido en `src/gateway/app.py`: 409 explicito con `required_model` y
  `active_model`. Exento para `code`, que ya tiene passthrough a cualquier
  `coder*`.
- **Pendiente**: `/jobs/*` NO pasa por el gate — `src/jobs/worker.py:115` y
  `src/jobs/router.py:200` llaman `orchestrator.switch_to()` directo. Con
  "nada cambia automaticamente" eso sigue cambiando de modelo.
- Al reiniciar el gateway, `active_model` queda `None` hasta un adopt exitoso.
  Mientras el 30B carga (503 durante ~100s) TODO request da 409 "cargá un modelo
  manualmente". Es preexistente, no del guard; esperar a que `/props` responda.

## active_model era estado ficticio — reconciliado con systemd

- `orchestrator.active_model` vive en memoria: arranca en `None` en cada restart
  del gateway mientras el modelo sigue corriendo. Eso hacia que
  `POST /jobs/switch` recargara un modelo YA cargado (~100s de downtime por nada).
  Verificado: 202 "switching" + modelo 503 -> 200 cuando ya estaba corriendo.
- **`src/gateway/adopt.py`**: la discriminacion la hace la unidad systemd, NO el
  puerto. Trece modelos de coder comparten el 8082, asi que un puerto sano
  prueba que hay algo cargado pero nunca cual. El puerto es solo gate de
  readiness (una unidad queda "active" mientras sus pesos siguen cargando y
  llama-server responde 503).
- `reconcile_until_ready` reintenta hasta 300s: un reconcile one-shot pierde la
  carrera contra un modelo que recien arranca, que es justo cuando se reinicia
  el stack.
- `reconcile_active_model` NUNCA sobreescribe un `active_model` ya seteado:
  `switch_to` es dueno de ese estado durante un swap exclusivo.
- **Trampa de tests**: `TestClient(app)` SIN `with` no corre el lifespan. Un
  NameError en el startup task paso los 167 tests con el gateway en crash-loop.
  Cubierto por `test_lifespan_starts_clean`; usar `with TestClient(app)`.
- **Trampa de edicion**: `python3` con dos `write_text` sobre la misma `s` leida
  una vez -> el segundo write pisa el primero. Releer antes de cada write.

## SIGKILL al modelo 30B — problema preexistente, NO del gateway

- `llama-code-30b-190k.service` recibe SIGKILL externo periodicamente:
  `Main process exited, code=killed, status=9/KILL`. systemd no lo manda
  (eso seria SIGTERM + "Stopping"). Ocurre con el modelo **sirviendo**, no al
  cargar. Preexistente: hay SIGKILL a las 22:37 y 23:25, antes de esta sesion.
- Indicio fuerte de OOM killer: 31Gi RAM totales, 732Mi libres, 10Gi de swap
  usada (9.8Gi en swapfile2). llama-server ~5.7Gi RSS + chrome ~1Gi x3 +
  opencode ~1.6Gi. La unidad no tiene MemoryMax/MemoryHigh (infinity) y
  `OOMPolicy=stop`.
- **No confirmable sin root**: `sudo` pide password y `journalctl -k` no es
  accesible como usuario. Para confirmarlo hace falta(root o|Uso de root para ver kernel journal).
- Durante esto falló una verificacion mia de tool_calls con `KeyError: 'choices'`:
  la respuesta cruda era `503 Loading model`. NO era una regresion del fix, era
  el modelo muriendose. Verificar SIEMPRE el 8082 antes de concluir que algo
 and broke.

## Contexto del 30B bajado 190K -> 150K (2026-10-03)

**Por que:** `llama-code-30b-190k.service` recibia SIGKILL externo (`status=9/KILL`)
mientras servia, no al cargar. Pre-existente (SIGKILL a las 22:37 y 23:25, antes de
esta sesion). Indicio de OOM killer: 347Mi libres de 31Gi + 8Gi de swap usada.
**NO CONFIRMADO** — `sudo` pide password y `journalctl -k` no es accesible como
usuario. Es mitigacion por hipotesis, no por diagnostico confirmado.

**Cuanto vale el contexto.** Medido del GGUF (no estimado): 48 capas,
head_count_kv 4, key/value_length 128, KV en `-ctk q4_0 -ctv q4_0` y
`--no-kv-offload` (KV en RAM, no VRAM). Da ~27 KiB/token.
  - 190208 tokens -> KV ~4.90 GiB
  - 150016 tokens -> KV ~3.87 GiB  (lo que quedo configurado)

**Archivos tocados (3):**
1. `systemd/user/llama-code-30b-190k.service` — `-c 190000` -> `-c 150000`.
   `Description=` reescrito para advertir que la clave dice 190k pero el
   contexto real es 150000.
2. `config.yaml` entrada `coder-30b-190k` — `-c` y `--ctx-size` a 150000,
   `vram_mb` 22000 -> 18000 (igual que la entrada 150k). Comment de 7 lineas
   explique el por que y la deuda de nombres.
3. `config.yaml` `defaults.model` — comment actualizado: dice "contexto REAL 150K"
   en vez de "190K (22GB VRAM)".

**Lo que NO se toco a proposito:** la clave `coder-30b-190k` y el nombre del unit
siguen igual, para no romper `defaults.model`, `web/app.py`, `tests/test_adopt.py`,
`tests/test_gateway.py` y `tests/test_opencode_valuation.py` que la referencian.
**Debt pendiente: renombrar a `coder-30b-150k`** — esa entrada y su unit ya existen
y son byte-por-byte identicos salvo el `-c`. Es el fix limpio.

**Resultado medido:** RSS de llama-server 5.68 -> 4.37 GiB (**-1.31 GiB**, mas de
lo estimado porque el resto es allocator). `n_ctx` ahora 150016. 168 tests pasan.
Verificacion en viva OK: `finish_reason=tool_calls`, `read_file`, path absoluto
`/tmp/opencode/live_probe.txt` intacto.

**Si vuelve a morir con SIGKILL en 150K:** el contexto no era la causa. Bajar a
140000 solo libera ~86Mi mas, asi que no vale la pena — el culpable es otro proceso
comiendose la RAM. Para confirmar hace falta(root o|Uso de root para ver kernel journal).

## Instrumentacion del SIGKILL (2026-10-03, commit pending)

Todo en `src/orchestrator/lifecycle.py` + `src/gateway/proxy.py` +
`scripts/tripwire.py` + `tests/test_sigkill_instrumentation.py`.

- `_port_occupant_pids(port)`: PIDs + cmdline en un puerto, validando /proc
  (si no, `fuser -v 8082/tcp` reporta el 8082 como PID).
- `_kill_port_occupants`: loguea PIDs/contexto antes de `fuser -k`, y tiene
  GUARD que se niega a matar si `active_service` esta entre los ocupantes.
- `switch_to`: `SWITCH-BEGIN`, `SWITCH-STOP-SET`, `SWITCH-BLOCKED-HOLDS`.
- proxy: `BACKEND-5XX` ahora con method+path+body (el 5xx precede 1s a cada
  muerte y no se sabia que request era).
- `model-router-tripwire.service`: watcher 3s -> `logs/tripwire.jsonl` con el
  contexto completo de cada muerte.

**Correccion importante:** la hipotesis de que `to_stop` metia a todos los
coder (13 en 8082) era FALSA. `stop_current()` corre antes del lock y ya
pone active_service=None. Un test lo demostro. El riesgo real queda en
`fuser -k {port_del_target}/tcp`.

**Geteo de la clave:** el OOM ya no es la hipotesis (kern.log sin eventos OOM,
systemd-oomd inactive). El SIGKILL sigue sin causa cerrada.
