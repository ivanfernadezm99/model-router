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

---

## 2026-10-04 — RAM 31GiB confirmada, contexto 190K restaurado + timeouts opencode

**Que:** usuario amplio RAM a 31GiB (`free -h` lo confirma: 31Gi total).
Se levanto la mitigacion del 2026-10-03 y se restauro el contexto completo.

**Archivos tocados (3):**
1. `config.yaml` entrada `coder-30b-190k` — `-c`/`--ctx-size` 150000 -> 190000,
   `vram_mb` 18000 -> 22000. Comment viejo del SIGKILL reemplazado por nota de
   restauracion. `defaults.model` actualizado ("190K restaurado 2026-10-04").
2. `~/.config/systemd/user/llama-code-30b-190k.service` — `ExecStart -c 150000`
   -> `-c 190000` + `Description` actualizado. **OJO: el unit manda en runtime**
   (el gateway hace `stop/start` via systemctl); cambiar solo `config.yaml` NO
   aplica el contexto. `daemon-reload` + `restart`, `:8082/health` y `:8000/health`
   en `ok`/`ready`.
3. `~/.config/opencode/opencode.json` provider `llm-local` —
   `timeout`/`chunkTimeout`/`headerTimeout` 600000 (10min) -> 1800000 (30min).
   El `chunkTimeout` es el que cortaba con prompts de 100K+ (el primer token tarda
   minutos con KV en RAM + YaRN alto).

**Aprendido:** ante "cambie el contexto y no pasa nada", verificar el proceso
corriendo (`ps aux | grep llama-server`) ademas del `config.yaml`. El registry es
declarativo; la unidad systemd es la efectiva.

## 2026-10-04 — Templates alineados: Qwen3 -> qwen3-coder-tools.jinja

**Que:** las 6 entradas Qwen3 de `config.yaml` apuntaban al template del 2.5.
Ahora usan `templates/qwen3-coder-tools.jinja`:
`coder-30b-a3b`, `coder-30b-128k`, `coder-30b-150k`, `coder-30b-190k`,
`coder-30b-q5-100k`, `qwen3-14b-190k`. `Registry.load()` ok.

**No tocado a proposito:**
- `coder-14b-*` (Qwen2.5) quedan con `qwen25-coder-tools.jinja` — es su template.
- `qwen3-27b-flagship/xl` no tienen template explicito (usan el built-in del GGUF
  via `--jinja`). Forzarles el template coder a un denso con reasoning es riesgoso.
- El servicio corriendo YA usaba el template qwen3 (el unit estaba bien), asi que
  no hizo falta reiniciar por esto.

## 2026-10-04 — Fallo `doom_loop`: herramienta alucinada, no permisos

**Que:** opencode bloqueo un tool call a `doom_loop` con reglas
`allow * / ask doom_loop / deny *`. `doom_loop` no existe en el repo ni en
`opencode.json` — el modelo lo invento y el sistema de permisos lo denego.
Los permisos del usuario estan sanos.

**Hipotesis:** YaRN 5.9x a 190K degrada el tool-calling del 30B (se pone creativo
con nombres de herramientas). Alternativa: sesion contaminada por un loop previo.

**Plan si se repite:** sesion fresca primero; si persiste, bajar a `coder-30b-a3b`
(100K, YaRN 3.1x, tool-calling estable) y reservar el 190K para lectura masiva.

## 2026-10-04 — RAM ocupada: son los 2 WhatsApp, no un leak

**Que:** 23Gi usados de 31Gi. Top: chrome puppeteer ~9GB (2 renderers) +
llama-server ~4.7GB (KV en RAM por `--no-kv-offload`) + Brave ~4GB (renderers
desde el 29-sep) + `opencode web` ~1.7GB (desde el 29-sep).

**Aprendido:** los renderers puppeteer son los 2 WhatsApp sincronizando
(sesiones `openwa` del 01-oct) — uso legitimo, no apps mal lanzadas. Chrome
headless con WhatsApp Web + dias de uptime come eso. Alivio facil: reiniciar
Brave (3-4GB); alivio de fondo: `coder-30b-a3b` 100K todo en GPU (KV en VRAM).

---

## 2026-10-04 — APAGON :8082 10:39-12:26 por mi restart (643 crash-loops)

**Que:** mi `systemctl --user restart llama-code-30b-190k.service` de las 10:39
para aplicar el 190K dejo la unidad en crash-loop: `couldn't bind HTTP server
socket, port 8082` -> exit(1) -> `Restart=always` cada 5s -> **643 restarts**
hasta las 12:26. El puerto quedo ocupado por un proceso huerfano (el 150K
anterior no libero a tiempo / quedo colgado) y el nuevo proceso nunca pudo
bindear. A las 12:26, al levantar `llama-code-q4` (14B), el puerto se libero y
el 190K quedo `inactive`. Un solo llama-server corriendo desde entonces.

**Consecuencia:** las sesiones apply de multi-key (`sdd-apply` 10:47-11:04 y
`general` 11:14-11:38, DB `opencode.db`) corrieron DURANTE el apagon. Los
subagentes SI emitieron tool calls (16 y 25 parts `tool` en DB) pero el backend
estaba muerto -> errores -> el modelo fabulo: snippets inventados
(tipos SQL Server, `Id` en vez de `ZernioConfigurationId`), 1 solo archivo
escrito (migracion de las 11:04) y plan devuelto en el chat. No fue el prompt
(los dos prompts estan bien encarados) ni el Jinja.

**Verificado que el Jinja del 2.5 esta SANO (mismo dia, post-apagon):**
- `tests/test_template_minja.py`: 9 passed.
- Probe vivo directo a :8082 con tools: `finish_reason=tool_calls`, path absoluto
  intacto.
- Probe vivo via gateway :8000: `finish_reason=tool_calls`.
- Unidad q4 usa `%h/.../templates/qwen25-coder-tools.jinja` (el del repo, sin drift).

**Aprendido:**
1. Despues de CUALQUIER restart de una unidad llama, verificar el bind en el
   journal (`couldn't bind`), no solo `is-active` ni `/health` (un 200 aislado
   puede ser un falso positivo entre restarts).
2. `Restart=always` + puerto ocupado = loop infinito silencioso. Considerar
   `StartLimitBurst`/`StartLimitIntervalSec` en las unidades llama para que un
   bind fallido no gire 2 horas.
3. Ante "el subagente no escribe", antes de culpar al template: mirar parts en
   `opencode.db` (si hay `tool` parts, el parseo anda) y el estado del backend
   en la ventana horaria de la sesion (`journalctl --user -u <unidad>`).

---

## 2026-10-04 — Deadlock switch wan-14b: warmup /load antes del health gate

**Que:** pedir wan-14b desde la web dejaba el switch en `switching` hasta el
hard timeout (20min). Causa: `wan_server.py` es lazy (`loaded:false` hasta el
primer `/generate`) pero `poll_health` exige `loaded:true`; y el POST /load
que existia en `lifecycle.py` corria DESPUES del poll (nunca llegaba a
ejecutarse). Ademas `wan_server.py` ni siquiera tenia endpoint `/load`
(devolvia 404).

**Fix (3 archivos + 1 test nuevo):**
1. `~/Wan2.1/wan_server.py` — nuevo `POST /load`: llama `_get_pipe()` en
   thread (`asyncio.to_thread`, el `/health` sigue respondiendo durante la
   carga) y devuelve `{"loaded": true}`. Sirve a wan-14b (:8189) y wan-i2v
   (:8191): misma app.
2. `src/orchestrator/health.py` — nuevo `wait_http_200()`: gate liviano (solo
   HTTP 200, sin mirar body) para confirmar "proceso vivo" antes de disparar
   el warmup.
3. `src/orchestrator/lifecycle.py` — `switch_to`: tras el start, para targets
   no-`coder`, `wait_http_200` -> `POST /load` -> `poll_health`. Todo el bloque
   warmup es best-effort con try/except: si falla, `poll_health` sigue siendo
   la autoridad. Se elimino el bloque viejo post-poll (deadlock).
4. `tests/test_wan_warmup.py` (nuevo, 4 tests): orden wait->warmup->poll,
   coder saltea warmup, `wait_http_200` ignora `loaded:false`, timeout en 503.
   + mocks de `wait_http_200` en 4 tests viejos de `test_orchestrator.py`
   (sin eso colgaban contra red real: el gate nuevo es de verdad, no mock).

**Suite:** 176 passed (solo `test_e2e_queue.py` excluido: necesita stack vivo).

**Verificado en vivo:** switch a wan-14b completo 21:59->22:15 (`active`,
`loaded:true`). OJO: la carga real tarda ~15min en este host (disco lento con
65GB de pesos; el "30-60s" del log es optimista). El hard timeout de 1200s
alcanza justo. No achicarlo.

**Deuda:** `StartLimitBurst` en unidades llama (del apagon de las 10:39) sigue
pendiente.

---

## 2026-10-04 — `substring` de undefined al pedir video (id vs job_id)

**Que:** pedir video desde la web tiraba `Cannot read properties of undefined
(reading 'substring')`. Causa: `POST /api/submit` devolvia `{"id": ...}`
(clave del gateway) pero `index.html:306` lee `d.job_id` -> `undefined` ->
`currentJob.substring()` revienta. (202 acepta `r.ok`, asi que pasaba el
`throw` y explotaba en el render.)

**Fix (3 archivos):**
1. `web/app.py` `/api/submit` — devuelve `id` Y `job_id` (compat ambos).
2. `web/templates/index.html` — `currentJob = d.job_id || d.id` + guard con
   error legible si falta (nunca mas TypeError crudo).
3. `web/templates/monitor.html:88` — mismo crash con jobs del gateway
   (`job.job_id` inexistente): `(job.job_id || job.id || '?')`.
   (`index.html:355` ya estaba guardado con `(j.id || '?')`.)
   Reiniciado `model-router-web.service` (Flask; templates no necesitan restart
   pero `app.py` si). Verificado: `/api/submit` devuelve ambas claves.

**Nota:** la verificacion encoló un job real de prueba ("test substring fix",
`6431ce80`) detras del video del usuario — se va a generar igual (sin forma de
cancelar: los jobs corren como BackgroundTasks in-process). Borrar el mp4
resultante.

---

## 2026-10-04 — Videos fallados: OOM por default de 33 frames (fix: 17)

**Que:** los dos jobs de video fallaron con `CUDA out of memory` (23.5GB
usados, 8MB libres) en `self.model.to(self.device)` — el DiT entero sube a GPU
de una. Causa: `src/jobs/worker.py` mandaba `num_frames=33` por default
(video e i2v). El README ya documentaba 17 como maximo seguro en 3090
("33 o 81 frames OOMean"), pero el worker no lo respetaba.

**Fix:** defaults `33 -> 17` en `worker.py` (video + i2v) + comment con la
evidencia del OOM. Tests jobs/gateway/batch: 27 passed. Reiniciado el gateway
(el worker corre in-process). El wan sigue cargado (`loaded:true`), no hizo
falta recargar 15min.

**Ojo para el usuario:** "video con las palabras Enlaces Chaco" — Wan2.1 T2V
no renderiza texto legible; las letras salen deformadas casi siempre. Si quiere
texto exacto en el video, hay que componerlo encima despues (ffmpegetal).

---

## 2026-10-05 — Job videoZombie al 50%: race submit vs switch (sdxl mato a wan)

**Que:** job `1ae9bdf1` quedo `running`/`progress:50` para siempre. Eso NO es
"mitad renderizado": 50 es el default de enqueue; `updated_at == created_at`
prueba que el worker nunca lo toco. Secuencia (mismo segundo):
- 09:20:57.0 usuario pide video via web (worker arranca).
- 09:20:57.x entra `SWITCH-BEGIN target=sdxl` (click en otro modelo).
- 09:20:58 stop-before-start mata `wan-video` bajo los pies del worker.
El worker quedo colgado contra backend muerto y nunca actualizo el estado.

**Accion:** marcado `failed` a mano en Redis (`job:<id>`, `redis-py`, TTL 7d)
con error explicativo. Sin endpoint de cancel (los jobs son BackgroundTasks
in-process): no se puede cancelar limpio, solo marcar estado.

**Deuda (real, de diseno):** el worker de `/jobs/*` NO pasa por el gate de
exclusion contra switches concurrentes (ya anotado: `worker.py:115` y
`router.py:200` llaman `switch_to()` directo). Un submit + un switch al mismo
tiempo se pisan. Fix futuro: el worker deberia adoptar el modelo (si ya esta
activo, no switch) o tomar el lock del orchestrator antes de postear
/generate. Mientras tanto: NO tocar "Cargar" mientras un job corre.

---

## 2026-10-05 — Anti-zombies: worker finally + stale reap + cancel + aviso front

**Que:** job `1ae9bdf1` quedo `running`/50% eterno (el 50 es default de
enqueue, no progreso real). Capas del fix:
1. `src/jobs/worker.py` — `except CancelledError`: marca failed("worker
   detenido...") y re-raise (los BackgroundTasks morian en silencio en cada
   restart). Guards pre-POST y pre-completed: si el job ya no esta activo
   (cancelado), no genera y no pisa el failed del usuario. Heartbeat
   `progress=60` al arrancar el backend (mueve `updated_at`, el front lo usa
   para detectar stalls).
2. `src/jobs/queue.py` — `update_status()` acepta `progress` opcional.
3. `src/jobs/router.py` — `_is_stale`/`_reap_if_stale`: leer un job activo sin
   moverse >45min (peor legitimo: switch 1200s + gen 1200s + margen) lo marca
   failed con mensaje ("sin progreso... reenviá"). Aplicado en GET uno y GET
   lista. Nuevo `DELETE /jobs/{id}`: cancela (failed "cancelado por el
   usuario"; lo que ya este dentro del POST bloqueante igual termina pero se
   descarta el resultado).
4. `web/app.py` — proxy `DELETE /api/job/<id>`. `index.html` — `stallHtml()`:
   job activo con `updated_at` de hace 12+ min muestra "sin progreso hace X
   min". Boton ✖ en items activos + error visible en items failed.
   `monitor.html` ya cubierto en el fix anterior.
5. `tests/test_jobs_lifecycle.py` (nuevo, 6 tests): reap en GET y en lista,
   fresh no tocado, cancel + doble cancel + 404, CancelledError->failed,
   worker no revive pre-cancelados. **Suite: 182 passed.**

**Verificado en vivo** (sin gastar GPU: jobs fabricados en Redis): stale 2h ->
failed con mensaje al leerlo; fresh intacto; DELETE cancela; proxy UI OK.
Claves de prueba borradas.

**Hallazgo aparte (NO roto por mi):** `sdxl.service` (ComfyUI) no levanta:
`No module named 'alembic'` + `No module named 'comfy_aimdo'` — drift de
dependencias, crash-loop desde las 09:21. El switch a sdxl de las 09:20 tampoco
iba a completar nunca. Fix: instalar deps en el venv de ComfyUI (pendiente,
ofrecido al usuario). Pedir otro modelo frena el loop.
