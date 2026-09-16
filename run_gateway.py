"""Gateway startup script — avoids dual-module orchestrator bug (use this, not python -m src.gateway.app)."""
import sys
sys.path.insert(0, '/home/servidor/Descargas/model-router')

from src.gateway.app import app
import uvicorn

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    a = p.parse_args()
    uvicorn.run(app, host=a.host, port=a.port)
