"""Compatibility entry point for :mod:`franta.execution_gateway.skills`."""

from __future__ import annotations

if __name__ == "__main__":  # preserve ``python -m franta.skill_runtime``
    from .execution_gateway.skills import main as _main

    raise SystemExit(_main())
else:
    import sys as _sys

    from .execution_gateway import skills as _implementation

    # Returning the implementation module itself preserves legacy patches of
    # private process seams used by the existing fault tests.
    _sys.modules[__name__] = _implementation
