#!/bin/bash
# ./mejorar.sh foto.png "mejora"
# SDXL img2img (:8188)
set -e

if [ -z "$1" ] || [ -z "$2" ]; then
  echo "Uso: ./mejorar.sh <foto> \"descripcion\""
  exit 1
fi

FOTO="$1"
PROMPT="${2:-mejora de calidad}"

curl -s -X POST http://127.0.0.1:8000/v1/jobs \
  -H "X-Model-Hint: image" \
  -H "Content-Type: application/json" \
  -d "{\"prompt\":\"$PROMPT\", \"image\": \"data:image/png;base64,$(base64 -w0 \"$FOTO\" | tr -d '\n')\"}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('Job ID:', d.get('job_id','N/A')); print('Target:', d.get('target','N/A')); print('Status:', d.get('status','N/A'))"