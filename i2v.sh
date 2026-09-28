#!/bin/bash
# ./i2v.sh foto.png "prompt"
# Imagen → Video animada usando Wan2.1 I2V-14B-720P (:8191)
set -e

if [ -z "$1" ] || [ -z "$2" ]; then
  echo "Uso: ./i2v.sh <foto> \"prompt\""
  exit 1
fi

FOTO="$1"
PROMPT="${2:-slow camera pan}"

curl -s -X POST http://127.0.0.1:8000/v1/jobs \
  -H "X-Model-Hint: i2v" \
  -H "Content-Type: application/json" \
  -d "{\"prompt\":\"$PROMPT\", \"image\": \"data:image/png;base64,$(base64 -w0 \"$FOTO\" | tr -d '\n')\"}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('Job ID:', d.get('job_id','N/A')); print('Target:', d.get('target','N/A')); print('Status:', d.get('status','N/A'))"