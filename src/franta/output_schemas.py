"""Compatibility facade for structured final-response contracts."""

from __future__ import annotations

from .contracts.responses import SCHEMAS, STRICT_SCHEMA_BLOCKERS, write_schemas

__all__ = ["SCHEMAS", "STRICT_SCHEMA_BLOCKERS", "write_schemas"]
