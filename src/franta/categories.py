"""Compatibility alias for :mod:`franta.trim_category.repository`.

The module-object alias preserves legacy class identity, private diagnostic
access, and monkeypatch seams while production imports use the explicit Block
9 package.
"""

from __future__ import annotations

import sys as _sys

from .trim_category import repository as _implementation


_sys.modules[__name__] = _implementation
