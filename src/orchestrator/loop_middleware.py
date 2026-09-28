"""LoopGuard middleware for FastAPI — applies anti-loop protection to all /v1 requests."""

import asyncio
import json
import logging
import time
import uuid
from contextvars import ContextVar
from typing import Dict, Set

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.orchestrator.loop_guard import LoopGuard, ToolResult, get_session_guard

logger = logging.getLogger(__name__)

# Context variable to track loop guard per request
current_guard: ContextVar[LoopGuard] = ContextVar('current_guard', default=None)


class LoopGuardMiddleware(BaseHTTPMiddleware):
    """Middleware that applies LoopGuard to prevent cascading loops.

    Session binding: prefers explicit x-session-id, else falls back to the
    shared "opencode-default" guard (all opencode traffic comes from
    127.0.0.1, so per-IP or random-UUID guards would never accumulate).
    Additionally tracks recent prompt hashes globally to catch an LLM that
    resends the identical prompt after an identical error (MCP retry loops).
    """

    def __init__(self, app, max_iterations: int = 12, max_retries: int = 15):
        super().__init__(app)
        self.max_iterations = max_iterations
        self.max_retries = max_retries
        self._failed_paths: Set[str] = set()
        self._guard_creation_count = 0
        self._recent_prompt_hashes: Dict[str, int] = {}

    def _resolve_session_id(self, request: Request) -> str:
        explicit = request.headers.get("x-session-id")
        if explicit:
            return explicit
        # Stable fallback: one shared guard for local opencode traffic so
        # limits actually accumulate instead of resetting per request.
        return "opencode-default"

    def _prompt_hash(self, body: bytes) -> str:
        import hashlib

        try:
            data = json.loads(body) if body else {}
            prompt = data.get("prompt", "")
            messages = data.get("messages", [])
            last_user = ""
            for m in reversed(messages):
                if isinstance(m, dict) and m.get("role") == "user":
                    last_user = str(m.get("content", ""))
                    break
            key = prompt or last_user or body.decode(errors="ignore")[:2000]
        except Exception:
            key = body.decode(errors="ignore")[:2000] if body else ""
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    async def dispatch(self, request: Request, call_next):
        # Create session guard for this request (stable across agent loop)
        session_id = self._resolve_session_id(request)
        guard = get_session_guard(
            session_id=session_id,
            max_iterations=self.max_iterations,
            max_total_retries=self.max_retries
        )
        current_guard.set(guard)
        
        # Check if we've exceeded global limits
        if not guard.can_proceed():
            logger.warning(json.dumps({
                "event": "request_blocked_by_loop_guard",
                "session_id": session_id,
                "iteration": guard._iteration_count,
                "reason": "iteration_limit_exceeded"
            }))
            return Response(
                content=json.dumps({
                    "error": "Request blocked: too many iterations",
                    "session_id": session_id
                }),
                status_code=429,
                media_type="application/json"
            )
        
        # Add request context entry
        guard.add_context_entry(f"request:{request.url.path}")

        # Identical-prompt loop detection (LLM resending same prompt after same error)
        if request.url.path.startswith("/v1/"):
            try:
                raw = await request.body()
                # Re-inject body downstream: Starlette caches request.body(), safe to re-read.
                phash = self._prompt_hash(raw)
                count = self._recent_prompt_hashes.get(phash, 0) + 1
                self._recent_prompt_hashes[phash] = count
                # Keep bounded
                if len(self._recent_prompt_hashes) > 200:
                    oldest = next(iter(self._recent_prompt_hashes))
                    del self._recent_prompt_hashes[oldest]
                if count >= 3:
                    logger.warning(json.dumps({
                        "event": "loop_guard_identical_prompt",
                        "session_id": session_id,
                        "prompt_hash": phash,
                        "count": count,
                    }))
                    return Response(
                        content=json.dumps({
                            "error": "LOOP_DETECTED / retryable=false - STOP. Identical prompt sent 3x. Continue with available info.",
                            "session_id": session_id,
                            "prompt_hash": phash,
                        }),
                        status_code=429,
                        media_type="application/json",
                    )
            except Exception:
                pass
        
        try:
            response = await call_next(request)
            return response
        except Exception as exc:
            # Record the error
            guard.record_failure(
                str(exc),
                "middleware",
                status_code=getattr(exc, 'status_code', None)
            )
            
            # Log and potentially re-raise or handle
            logger.error(json.dumps({
                "event": "middleware_exception",
                "session_id": session_id,
                "error": str(exc),
                "iteration": guard._iteration_count
            }))
            
            # Return error response
            return Response(
                content=json.dumps({
                    "error": "Request processing failed",
                    "details": str(exc),
                    "session_id": session_id
                }),
                status_code=500,
                media_type="application/json"
            )


class ToolGuard:
    """Context manager for applying LoopGuard to tool calls.
    
    Usage:
        with ToolGuard("read_file") as guard:
            result = await read_file(path)
            if not result.success and not guard.should_retry():
                break
    """
    
    def __init__(self, tool_name: str, session_id: str = ""):
        self.tool_name = tool_name
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self.guard = get_session_guard(session_id=self.session_id)
        self.attempt_count = 0
        self.last_result: ToolResult | None = None
        
    def __enter__(self):
        self.attempt_count = 0
        if not self.guard.record_tool_attempt(self.tool_name):
            raise RuntimeError(f"Tool {self.tool_name} exceeded maximum attempts")
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        return False  # Don't suppress exceptions
    
    @property
    def should_retry(self) -> bool:
        """Check if we should retry based on loop guard limits."""
        return self.guard.can_proceed()
    
    def record_failure(self, error_msg: str) -> bool:
        """Record a failure and return True if we should continue retrying."""
        result = self.guard.record_failure(error_msg, self.tool_name)
        self.last_result = result
        self.attempt_count += 1
        return result.retryable


# Global set of failed tools to prevent cascading failures
_failed_tools: Set[str] = set()
_tool_failure_count: Dict[str, int] = {}
_tool_failure_lock = asyncio.Lock()


async def rate_limit_tool_calls(
    tool_name: str,
    max_failures: int = 3,
    reset_interval_s: int = 60
) -> bool:
    """Check if a tool should be rate-limited due to repeated failures.
    
    Returns True if the tool is healthy to call, False if rate-limited.
    """
    async with _tool_failure_lock:
        # Clean old failures
        if tool_name in _tool_failure_count:
            count, first_fail = _tool_failure_count[tool_name]
            if time.time() - first_fail > reset_interval_s:
                del _tool_failure_count[tool_name]
                _failed_tools.discard(tool_name)
        
        # Check if rate limited
        if tool_name in _failed_tools:
            count = _tool_failure_count.get(tool_name, (0, 0))[0]
            if count >= max_failures:
                logger.warning(json.dumps({
                    "event": "tool_rate_limited",
                    "tool": tool_name,
                    "failure_count": count
                }))
                return False
        
        return True


def mark_tool_failure(tool_name: str) -> None:
    """Mark a tool as having failed."""
    async def _mark():
        async with _tool_failure_lock:
            count, first_fail = _tool_failure_count.get(tool_name, (0, time.time()))
            _tool_failure_count[tool_name] = (count + 1, first_fail)
            if count + 1 >= 3:
                _failed_tools.add(tool_name)
    asyncio.create_task(_mark())


def clear_tool_failure(tool_name: str) -> None:
    """Clear failure marker for a tool."""
    async def _clear():
        async with _tool_failure_lock:
            if tool_name in _tool_failure_count:
                del _tool_failure_count[tool_name]
            _failed_tools.discard(tool_name)
    asyncio.create_task(_clear())


# Configuration for the model router
LOOP_GUARD_CONFIG = {
    "max_iterations": 12,
    "max_retries": 15,
    "max_tool_attempts": 3,
    "max_path_resolution": 2,
    "tool_failure_threshold": 3,
    "context_reduction_threshold": 50,
}


def get_loop_guard_status() -> dict:
    """Get current loop guard status for monitoring."""
    from src.orchestrator.loop_guard import _guards

    guards = [guard.get_status() for guard in _guards.values()]
    return {
        "active_guards": guards,
        "config": LOOP_GUARD_CONFIG,
        "failed_tools": list(_failed_tools),
        "tool_failure_counts": dict(_tool_failure_count),
    }