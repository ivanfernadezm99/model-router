#!/bin/bash
# ./video.sh "prompt"
# Texto → Video usando Wan2.1 T2V-14B (:8189)
set -e

if [ -z "$1" ]; then
  echo "Uso: ./video.sh \"prompt\""
  exit 1
fi

PROMPT="$1"
curl -s -X POST http://127.0.0.1:8000/v1/jobs \
  -H "X-Model-Hint: video" \
  -H "Content-Type: application/json" \
  -d "{\"prompt\":\"$PROMPT\"}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('Job ID:', d.get('job_id','N/A')); print('Target:', d.get('target','N/A')); print('Status:', d.get('status','N/A'))"