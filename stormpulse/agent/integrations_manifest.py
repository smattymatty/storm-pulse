"""The in-tree Integration registration manifest (CORE-005 decision 3/6).

Importing this module imports each Integration's module for its
``register_integration`` side effect, exactly as ``cli/init.py`` imports the
feature ``init`` modules for ``register_init_step``. Adding a fourth
Integration is one line here plus its own module - bootstrap, reconnect,
register, and the loops never change. This single Entry seam is what a future
third-party loader (its own ADR) would replace; nothing else assumes in-tree.
"""

from __future__ import annotations

import stormpulse.caddy.integration  # noqa: F401
import stormpulse.garage.integration  # noqa: F401
