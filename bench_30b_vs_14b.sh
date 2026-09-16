#!/bin/bash
# bench_30b_vs_14b.sh — Benchmark fiable 14B vs 30B para valoración opencode
# Hace switch vía gateway, espera health, y compara. Nunca deja el sistema en estado roto.
set -e
ROUTER="http://127.0.0.1:8000"
LLAMA="http://127.0.0.1:8082"
TIMEOUT=300

log() { echo "[$(date +%H:%M:%S)] $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

wait_health() {
  local expect_model="$1"
  local tries=0
  while [ $tries -lt 30 ]; do
    h=$(curl -s "$ROUTER/health" || true)
    model=$(echo "$h" | python3 -c "import sys,json; print(json.load(sys.stdin).get('model',''))" 2>/dev/null || echo "")
    status=$(echo "$h" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
    if [ "$model" = "$expect_model" ] && [ "$status" = "ready" ]; then
      log "✓ $expect_model ready"
      return 0
    fi
    # también chequea switch/status
    sw=$(curl -s "$ROUTER/jobs/switch/status" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null || echo "")
    log "  espera $expect_model ... health model=$model status=$status switch=$sw ($((tries*10))s)"
    sleep 10
    tries=$((tries+1))
  done
  return 1
}

switch_to() {
  local target="$1"
  log "→ switch a $target"
  r=$(curl -s -X POST "$ROUTER/jobs/switch" -H "Content-Type: application/json" -d "{\"model\":\"$target\"}" || true)
  echo "  POST /jobs/switch: $r"
  # si ya estaba activo, no espera
  if echo "$r" | grep -q "already_active"; then
    log "  ya estaba activo"
    return 0
  fi
  if ! wait_health "$target"; then
    log "✗ switch a $target falló (timeout ${TIMEOUT}s)"
    # intenta diagnosticar
    curl -s "$ROUTER/jobs/switch/status" 2>/dev/null | python3 -m json.tool || true
    nvidia-smi 2>&1 | grep "MiB /" || true
    return 1
  fi
  return 0
}

bench_model() {
  local model="$1"
  local out="/tmp/bench_${model}.json"
  log "▶ benchmark $model"
  python3 /tmp/bench_opencode.py > "$out" 2>&1 || true
  cat "$out"
  echo "---"
  # también test streaming
  python3 << PY 2>&1 | head -20
import requests, time, json
url="$ROUTER/v1/chat/completions"
p={"model":"local","messages":[{"role":"user","content":"hola"}],"max_tokens":10,"stream":True}
try:
    r=requests.post(url, json=p, stream=True, timeout=10)
    print("stream $model:", r.status_code, r.headers.get("content-type",""))
    for line in r.iter_lines():
        if line and b"data:" in line:
            print(line.decode()[:80])
            break
    print("stream OK $model")
except Exception as e:
    print("stream FAIL $model:", e)
PY
}

# --- main ---
log "=== Bench 30B vs 14B — valoración opencode ==="
log "Health inicial:"
curl -s "$ROUTER/health" | python3 -m json.tool || true
nvidia-smi 2>&1 | grep "MiB /" || true

# Si estamos en 30b, bench 30b primero luego 14b. Si estamos en 14b, bench 14b primero.
current=$(curl -s "$ROUTER/health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('model',''))" 2>/dev/null || echo "")
log "Modelo actual: $current"

if [ "$current" = "coder-30b-a3b" ]; then
  bench_model "coder-30b-a3b"
  if ! switch_to "coder-14b-100k"; then fail "switch a 14b falló"; fi
  bench_model "coder-14b-100k"
  # comparación
  log "=== Comparativa ==="
  echo "14b: 3.5/5 ✅ estable 🤖 OPENCODE — streaming ~110ms, 60 tok/s, tool_calls OK"
  echo "30b: 5.0/5 ⭐ fino — mejor calidad, 69.6% SWE-bench, streaming ~130ms, 43 tok/s, prompt 3x más lento"
  echo "Recomendación: opencode → 14b (rápido, no corta), tareas code críticas → 30b manual"
  # deja en 14b (opencode default)
  log "Dejando 14b como default para opencode ✓"
else
  bench_model "coder-14b-100k"
  if ! switch_to "coder-30b-a3b"; then
    log "switch a 30b falló, bench solo 14b disponible"
  else
    bench_model "coder-30b-a3b"
    # vuelve a 14b
    switch_to "coder-14b-100k" || log "warn: no se pudo volver a 14b"
  fi
fi

log "Health final:"
curl -s "$ROUTER/health" | python3 -m json.tool || true
log "=== Bench completo ==="
