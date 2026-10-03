# Feature: Sampler del gateway + registry determinista

## Objetivo
Que el gateway no pise la sampling del cliente, y que el registry se resuelva igual desde cualquier cwd.

## Problema
**1. Sampler.** `src/gateway/proxy.py` reescribía la sampling del cliente en 1424/1424 requests (verificado en journalctl, patch idéntico cada vez), incluso sobre valores explícitos del cliente:

```json
{"repeat_penalty": 1.15, "repeat_last_n": 256, "dry_multiplier": 0.9,
 "dry_task": "code", "temperature": 0.2, "top_p": "capped_0.85", "top_k": 20}
```

`dry_allowed_length: 2` penaliza cualquier bigram repetido en 512 tokens. Un path ES repetición (`/home`, `servidor`, `.py` se repiten dentro del mismo path y entre tool calls consecutivos). El sampler castigaba al modelo por emitir el path correcto. `top_k: 20` truncaba la cola, que es justo lo que hace falta para terminar un path largo token a token.

Además `_looks_like_code` default a "código" y cargaba una lista de palabras en español hardcodeada (`criterium`, `landing`) agregada por un incidente puntual. Resultado: cualquier mensaje corto ("escribime un mail") se clasificaba como código y recibía `temp 0.2` + DRY agresivo.

**2. Registry.** Dos loaders resolvían el mismo archivo con orden de fallback OPUESTO:
- `registry.py` → `["config.yaml", "config/config.yaml"]`
- `config.py` → `["config/config.yaml", "./config.yaml"]`

Ambos relativos al cwd. Con un `config/config.yaml` obsoleto (apuntaba a `/models/coder-q4.gguf`, inexistente, y a model keys que ya no existen), un cambio de cwd cargaba un registry donde todos los modelos estaban rotos.

**3. Fail-open silencioso.** `validate_config` aborta el archivo entero si un modelo tiene puerto o endpoint fuera del `ALLOWED` de `src/common/validation.py`. `src/gateway/app.py` logueaba un warning y seguía con `models = {}`. Resultado: 400 en todo, sin rastro de la causa real.

## Alcance autorizado
- `src/gateway/proxy.py`: sampler pasa a defaults, nunca override. Detección de tráfico de agente (`tools` presente).
- `src/gateway/proxy.py`: `_looks_like_code` exige evidencia positiva de código; se elimina la lista de palabras en español.
- `src/registry/config.py`: resolver único anclado al archivo, no al cwd.
- `src/registry/registry.py`: usa el resolver único; `resolve()` case-insensitive y con `strip()`.
- `src/gateway/app.py`: falla ruidosamente si el registry no carga.
- Eliminar `config/config.yaml` obsoleto.
- Unidades systemd: fix del typo `-np 4--kv-unified` y sincronizar `systemd/user/` con `~/.config/systemd/user/`.
- Tests: `tests/test_sampler_defaults.py` (nuevo), ampliar `tests/test_registry.py`.
- Reiniciar servicios para que el template y el samplerNuevosTOMEN efecto.

## Criterios de aceptación
- [x] Ningún valor de sampling explícito del cliente es sobreescrito
- [x] Tráfico de agente no recibe DRY ni `top_k` forzado
- [x] Los defaults anti-loop siguen existiendo para code no-agente (donde nació el loop)
- [x] `SAMPLER_DEFAULTS_DISABLED=1` desactiva todo el bloque
- [x] El registry carga igual desde cualquier cwd
- [x] `resolve()` acepta casing y espacios distintos
- [x] Un registry inválido lanza en vez de quedar vacío
- [x] `coder-30b-a3b` arranca (typo `-np 4--` corregido)
- [x] `systemd/user/` sin drift contra las unidades instaladas

## Verificación
- `python3 -m pytest tests/ -q` → **100 passed, 6 failed**
- Los 6 fallos son **preexistentes** (verificados sobre el árbol limpio con `git stash` en el work unit anterior): `test_e2e_queue`, 2× `test_jobs_wiring`, 3× `test_opencode_valuation`. Ninguno toca proxy ni registry.
- `tests/test_sampler_defaults.py` → 20 passed (nuevo)
- `tests/test_registry.py` → 8 passed (4 nuevos)
- Resolución desde cwd ajeno verificada en vivo: 18 modelos desde `/tmp`.

## Fora de alcance (decisión del usuario, nobug)
- **Template Qwen2.5 sobre modelos Qwen3-Coder.** Los `coder-30b-*` son Qwen3-Coder pero cargan `qwen25-coder-tools.jinja`. Los tags nativos de tool call de Qwen3 difieren. Cambiar el template es una decisión de modelo con riesgo propio: el actual ya está arreglado y testeado.
- **Doble fuente de verdad `args` (config.yaml) vs `ExecStart` (.service).** El path real vive en el `.service`; `config.yaml` solo aporta validación y el ctx que muestra el front. Unificarlo es un rediseño.
- **13 modelos en `8082 + /health`.** El auto-detect adopta el primero que responde 200, así que "modelo activo" depende del orden del YAML. Resolverlo con puertos/alias por modelo es un rediseño.
- **6 tests preexistentes en rojo.** Requieren decision aparte sobre si se arreglan o se documentan como deuda conocida.
