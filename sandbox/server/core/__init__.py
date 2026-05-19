# sandbox/server/core/__init__.py
"""
Core Components Module

Contains:
- ResourceRouter: Resource routing table management
- ToolExecutor: Tool executor
- decorators: Tool decorator (@tool)
"""

from .resource_router import ResourceRouter
from .tool_executor import ToolExecutor
from .decorators import tool, is_tool, get_tool_metadata, scan_tools, TOOL_MARKER
from .backpressure import (
    BackpressureManager,
    Bound,
    LaneGroup,
    OverloadedError,
    build_default_limiter,
    overloaded_response,
    DEFAULT_LIMITS,
)

__all__ = [
    "ResourceRouter", 
    "ToolExecutor",
    # Decorator related
    "tool",
    "is_tool", 
    "get_tool_metadata",
    "scan_tools",
    "TOOL_MARKER",
    # Backpressure (Phase 2S / commit 2S.2)
    "BackpressureManager",
    "Bound",
    "LaneGroup",
    "OverloadedError",
    "build_default_limiter",
    "overloaded_response",
    "DEFAULT_LIMITS",
]

