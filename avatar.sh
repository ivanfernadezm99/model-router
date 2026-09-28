#!/bin/bash
# ./avatar.sh foto.png audio.wav "texto"
# Avatar hablante usando EchoMimicV2 (:8192)
set -e

if [ -z "$1" ] || [ -z "$2" ] || [ -z "$3" ]; then
  echo "Uso: ./avatar.sh <foto> <audio> \"texto\""
  exit 1
fi

FOTO="$1"
AUDIO="$2"
TEXTO="${3:-Hola, bienvenidos a mi sistema}"

curl -s -X POST http://127.0.0.1:8000/v1/jobs \
  -H "X-Model-Hint: avatar" \
  -H "Content-Type: application/json" \
  -d "{\"prompt\":\"$TEXTO\", \"image\": \"data:image/png;base64,$(base64 -w0 \"$FOTO\" | tr -d '\n')\", \"audio\": \"data:audio/wav;base64,$(base64 -w0 \"$AUDIO\" | tr -d '\n')\"}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('Job ID:', d.get('job_id','N/A')); print('Target:', d.get('target','N/A')); print('Status:', d.get('status','N/A'))"