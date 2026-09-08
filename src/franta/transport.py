"""Compatibility alias for :mod:`franta.execution_gateway.transport`.

The module-object alias preserves legacy monkeypatch seams as well as public
class identity during the Block 6 extraction.
"""

from __future__ import annotations

import sys as _sys

from .execution_gateway import transport as _implementation


_sys.modules[__name__] = _implementation
