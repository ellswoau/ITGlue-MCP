"""Tool definition functions for the IT Glue MCP server.

Each ``register_*`` function wires fastmcp ``@tool`` decorators bound to a
resolved :class:`ITGlueConfig`. Splitting imports keeps the server lean and
allows tests to build clients on demand.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from fastmcp import FastMCP
    from ..config import ITGlueConfig

from . import document_tools, organization_tools


def register_all(mcp: "FastMCP", config: "ITGlueConfig") -> None:
    organization_tools.register(mcp, config)
    document_tools.register(mcp, config)
