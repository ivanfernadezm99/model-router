"""LoopGuard — Anti-loop protection for async tool calls and model requests.

This module prevents infinite loops in:
1. Tool calls (read_file, http requests, etc.)
2. Model inference on llama.cpp with repeating patterns
3. Failed path resolution attempts
4. Cascading error scenarios

Usage:
    guard = LoopGuard(session_id="abc123")
    
    for attempt in range(max_attempts):
        if not guard.can_proceed():
            break
        result = await some_tool_call()
        if result.failed:
            guard.record_failure(result.error)
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ToolErrorType(Enum):
    """Categorization of tool failures for loop detection."""
    FILE_NOT_FOUND = "file_not_found"
    PERMISSION_DENIED = "permission_denied"
    CONTEXT_OVERFLOW = "context_overflow"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    MODEL_ERROR = "model_error"
    LOOP_DETECTED = "loop_detected"
    UNKNOWN = "unknown"


@dataclass
class ErrorSignature:
    """Normalized error signature for semantic comparison."""
    error_type: str
    normalized_message: str
    tool_name: str
    
    def __hash__(self) -> int:
        return hash((self.error_type, self.normalized_message, self.tool_name))
    
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ErrorSignature):
            return False
        return (self.error_type == other.error_type and 
                self.normalized_message == other.normalized_message and
                self.tool_name == other.tool_name)


@dataclass
class ToolResult:
    """Structured tool result with success/failure state."""
    success: bool
    error_type: Optional[ToolErrorType] = None
    retryable: bool = True
    message: str = ""
    attempt: int = 1
    data: Any = None
    headers: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "error_type": self.error_type.value if self.error_type else None,
            "retryable": self.retryable,
            "message": self.message,
            "attempt": self.attempt,
            "data": self.data,
        }


def normalize_error_message(error_msg: str) -> str:
    """Normalize error messages for semantic loop detection."""
    if not isinstance(error_msg, str):
        error_msg = str(error_msg)
    
    error_msg = error_msg.lower().strip()
    
    # Pattern replacements for semantic normalization
    replacements = [
        (r"(could not find|could not locate|file not found|no such file|does not exist|cannot find|not found|unable to locate|unable to find)", "file_not_found"),
        (r"(permission denied|access denied|unauthorized|permission denied)", "permission_denied"),
        (r"(context.*overflow|too many tokens|out of context|context window|token limit)", "context_overflow"),
        (r"(timeout|timed out|time limit|exceeded time)", "timeout"),
        (r"(connection|network|offline|unreachable|refused)", "network_error"),
        (r"(loop|recycling|repeating|infinite|circular|pattern)", "loop_detected"),
        (r"(model.*error|llama.*error|context.*mismatch|bad parameters)", "model_error"),
    ]
    
    for pattern, replacement in replacements:
        error_msg = re.sub(pattern, replacement, error_msg)
    
    # Normalize whitespace and punctuation
    error_msg = re.sub(r'[^\w\s]', '', error_msg)
    error_msg = ' '.join(error_msg.split())
    
    return error_msg


def map_http_status_to_error(status_code: int, message: str = "") -> ToolErrorType:
    """Map HTTP status codes to error types."""
    if status_code in (404,):
        return ToolErrorType.FILE_NOT_FOUND
    if status_code in (401, 403,):
        return ToolErrorType.PERMISSION_DENIED
    if status_code in (408, 504,):
        return ToolErrorType.TIMEOUT
    if status_code in (502, 503,):
        return ToolErrorType.NETWORK_ERROR
    if status_code >= 500:
        return ToolErrorType.MODEL_ERROR
    return ToolErrorType.UNKNOWN


class LoopGuard:
    """Anti-loop protection for orchestrator operations.
    
    Guards against:
    - Repeated tool call failures
    - Cascading error patterns
    - Model inference loops
    - Path resolution cycles
    """
    
    # Default limits
    DEFAULT_MAX_ITERATIONS = 12
    DEFAULT_MAX_SAME_ERROR = 2
    DEFAULT_MAX_TOOL_ATTEMPTS = 3
    DEFAULT_MAX_PATH_RESOLUTION = 2
    DEFAULT_MAX_TOTAL_RETRIES = 15
    
    def __init__(
        self,
        session_id: str = "",
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        max_same_error: int = DEFAULT_MAX_SAME_ERROR,
        max_tool_attempts: int = DEFAULT_MAX_TOOL_ATTEMPTS,
        max_path_resolution: int = DEFAULT_MAX_PATH_RESOLUTION,
        max_total_retries: int = DEFAULT_MAX_TOTAL_RETRIES,
    ):
        self.session_id = session_id
        self.max_iterations = max_iterations
        self.max_same_error = max_same_error
        self.max_tool_attempts = max_tool_attempts
        self.max_path_resolution = max_path_resolution
        self.max_total_retries = max_total_retries
        
        # Tracking state
        self._error_signatures: List[ErrorSignature] = []
        self._tool_attempts: Dict[str, int] = {}
        self._iteration_count = 0
        self._total_retries = 0
        self._path_resolution_attempts: Dict[str, int] = {}
        self._last_tool_result: Optional[ToolResult] = None
        self._start_time = time.monotonic()
        
        # Context reduction markers
        self._context_entries: List[str] = []
        
    def create_error_signature(
        self, error_msg: str, tool_name: str, error_type: Optional["ToolErrorType"] = None
    ) -> ErrorSignature:
        """Create a normalized error signature for comparison.

        Uses the classified ToolErrorType (not the first word of the message)
        so semantically identical errors share one signature.
        """
        normalized = normalize_error_message(error_msg)
        return ErrorSignature(
            error_type=error_type.value if error_type else "unknown",
            normalized_message=normalized,
            tool_name=tool_name,
        )
    
    def can_proceed(self) -> bool:
        """Check if we can proceed without hitting loop limits."""
        self._iteration_count += 1
        
        # Check total iterations
        if self._iteration_count > self.max_iterations:
            logger.warning(json.dumps({
                "event": "loop_guard_iteration_limit",
                "session_id": self.session_id,
                "iteration": self._iteration_count,
                "limit": self.max_iterations,
            }))
            return False
        
        # Check total retries
        if self._total_retries > self.max_total_retries:
            logger.warning(json.dumps({
                "event": "loop_guard_retry_limit",
                "session_id": self.session_id,
                "retries": self._total_retries,
                "limit": self.max_total_retries,
            }))
            return False
        
        return True
    
    def record_failure(
        self,
        error_msg: str,
        tool_name: str,
        status_code: Optional[int] = None,
    ) -> ToolResult:
        """Record a failure and check if it represents a loop."""
        self._total_retries += 1
        
        # Map to error type
        if status_code:
            error_type = map_http_status_to_error(status_code, error_msg)
        else:
            normalized = normalize_error_message(error_msg)
            if "loop_detected" in normalized or "loop" in normalized.split():
                error_type = ToolErrorType.LOOP_DETECTED
            elif "timeout" in normalized:
                error_type = ToolErrorType.TIMEOUT
            elif "file_not_found" in normalized:
                error_type = ToolErrorType.FILE_NOT_FOUND
            elif "permission_denied" in normalized:
                error_type = ToolErrorType.PERMISSION_DENIED
            elif "context_overflow" in normalized:
                error_type = ToolErrorType.CONTEXT_OVERFLOW
            elif "network_error" in normalized:
                error_type = ToolErrorType.NETWORK_ERROR
            elif "model_error" in normalized:
                error_type = ToolErrorType.MODEL_ERROR
            else:
                error_type = ToolErrorType.UNKNOWN
        
        # Determine if retryable
        retryable = error_type not in (
            ToolErrorType.FILE_NOT_FOUND,
            ToolErrorType.LOOP_DETECTED,
            ToolErrorType.PERMISSION_DENIED,
        )
        
        # Create signature (classified type, not first word of message)
        sig = self.create_error_signature(error_msg, tool_name, error_type)

        # Check for identical error repetition (last signature match -> stop)
        if self._error_signatures:
            last_sig = self._error_signatures[-1]
            if last_sig == sig:
                logger.warning(json.dumps({
                    "event": "loop_guard_identical_error",
                    "session_id": self.session_id,
                    "error_type": error_type.value,
                    "tool": tool_name,
                    "iteration": self._iteration_count,
                    "repeated_count": len([e for e in self._error_signatures
                                          if e == sig]),
                }))

                return ToolResult(
                    success=False,
                    error_type=error_type,
                    retryable=False,
                    message=f"{error_type.value} / retryable=false - STOP. Identical error repeated.",
                    attempt=len([e for e in self._error_signatures if e == sig]) + 1,
                )
        
        # Record the error
        self._error_signatures.append(sig)
        
        # Check recent errors for semantic repetition (stable key, not hash())
        recent = self._error_signatures[-self.max_same_error:]
        if len(recent) >= self.max_same_error:
            unique_signatures = {
                (e.error_type, e.normalized_message, e.tool_name) for e in recent
            }
            if len(unique_signatures) == 1:
                return ToolResult(
                    success=False,
                    error_type=error_type,
                    retryable=False,
                    message=f"{error_type.value} / retryable=false - STOP. Repeated identical error pattern.",
                    attempt=len(recent),
                )
        
        return ToolResult(
            success=False,
            error_type=error_type,
            retryable=retryable,
            message=error_msg,
            attempt=self._tool_attempts.get(tool_name, 0),
        )
    
    def record_tool_attempt(self, tool_name: str) -> bool:
        """Record a tool attempt and check limits."""
        attempts = self._tool_attempts.get(tool_name, 0) + 1
        self._tool_attempts[tool_name] = attempts
        
        if attempts > self.max_tool_attempts:
            logger.warning(json.dumps({
                "event": "loop_guard_tool_limit",
                "session_id": self.session_id,
                "tool": tool_name,
                "attempts": attempts,
                "limit": self.max_tool_attempts,
            }))
            return False
        return True
    
    def record_path_resolution_attempt(self, path: str) -> bool:
        """Record a path resolution attempt and check limits."""
        attempts = self._path_resolution_attempts.get(path, 0) + 1
        self._path_resolution_attempts[path] = attempts
        
        if attempts > self.max_path_resolution:
            logger.warning(json.dumps({
                "event": "loop_guard_path_limit",
                "session_id": self.session_id,
                "path": path,
                "attempts": attempts,
                "limit": self.max_path_resolution,
            }))
            return False
        return True
    
    def add_context_entry(self, entry: str) -> None:
        """Add a context entry (for tracking what's being added)."""
        self._context_entries.append(entry)

    def should_reduce_context(self) -> bool:
        """Check if we should reduce context to prevent memory issues.

        Fires early: 2+ classified errors OR 10+ context entries with any
        error. Callers should replace the repeated tool+error history with
        get_compacted_summary() instead of appending more turns.
        """
        classified = sum(1 for e in self._error_signatures if e.error_type != "unknown")
        if classified >= 2:
            return True
        return len(self._error_signatures) >= 2 and len(self._context_entries) >= 10

    def get_compacted_summary(self) -> str:
        """Single-line replacement for a repeated failure history (context pruning)."""
        if not self._error_signatures:
            return "No errors recorded."
        last = self._error_signatures[-1]
        return (
            f"[{last.tool_name} failed x{len(self._error_signatures)}: "
            f"{last.error_type} / retryable=false. "
            f"Continue with available info, do not retry identical call.]"
        )
    
    def get_last_result(self) -> Optional[ToolResult]:
        """Get the last recorded tool result."""
        return self._last_tool_result
    
    def set_last_result(self, result: ToolResult) -> None:
        """Set the last tool result."""
        self._last_tool_result = result
    
    def get_status(self) -> Dict[str, Any]:
        """Get current loop guard status for debugging."""
        return {
            "iteration_count": self._iteration_count,
            "total_retries": self._total_retries,
            "tool_attempts": dict(self._tool_attempts),
            "path_resolution_attempts": dict(self._path_resolution_attempts),
            "error_count": len(self._error_signatures),
            "unique_errors": len(
                {(e.error_type, e.normalized_message, e.tool_name) for e in self._error_signatures}
            ),
            "session_id": self.session_id,
            "max_iterations": self.max_iterations,
            "max_retries": self.max_total_retries,
            "compacted_summary": self.get_compacted_summary(),
        }


# Global instance storage (per process, not per call)
_guards: Dict[str, LoopGuard] = {}


def get_session_guard(session_id: str, **kwargs) -> LoopGuard:
    """Get or create a LoopGuard for a session."""
    if session_id not in _guards:
        _guards[session_id] = LoopGuard(session_id=session_id, **kwargs)
    return _guards[session_id]


def clear_session_guard(session_id: str) -> None:
    """Clear a session's LoopGuard (on session end)."""
    if session_id in _guards:
        del _guards[session_id]


# Decorator for protecting tool calls
def loop_protected(
    max_same_error: int = 2,
    max_tool_attempts: int = 3,
    retryable_error_types: Tuple[ToolErrorType, ...] = (),
    session_id: str = "default",
):
    """Decorator that adds loop protection to tool functions.

    Uses the shared per-session guard so repeated calls across invocations
    accumulate instead of starting from zero each time.

    Usage:
        @loop_protected(max_same_error=2, max_tool_attempts=3)
        def read_skill(path: str) -> str:
            ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            guard = get_session_guard(
                session_id=kwargs.pop("guard_session_id", session_id),
                max_same_error=max_same_error,
                max_tool_attempts=max_tool_attempts,
            )
            
            # Get path argument if it's the first positional arg
            path_arg = args[0] if args else kwargs.get('path', '')
            
            for attempt in range(max_tool_attempts):
                if not guard.can_proceed():
                    return ToolResult(
                        success=False,
                        retryable=False,
                        message="Maximum iterations reached",
                        attempt=attempt,
                    )
                
                if not guard.record_tool_attempt(func.__name__):
                    return ToolResult(
                        success=False,
                        retryable=False,
                        message=f"Tool {func.__name__} exceeded max attempts",
                        attempt=attempt,
                    )
                
                try:
                    result = func(*args, **kwargs)
                    if result.success:
                        return result
                    
                    # Record failure
                    fail_result = guard.record_failure(
                        result.message, func.__name__
                    )
                    if not fail_result.retryable:
                        return fail_result
                        
                except Exception as e:
                    fail_result = guard.record_failure(str(e), func.__name__)
                    return fail_result
            
            return ToolResult(
                success=False,
                retryable=False,
                message="Maximum tool attempts reached",
                attempt=max_tool_attempts,
            )
        return wrapper
    return decorator