"""Desktop task automation platform.

A platform where a company operator defines, trains and runs the repetitive
tasks of their daily work. Agents run headless; people supervise from a
dashboard.

The constraint that defines the system: **no skill may span more than three
screens**. Computer-use agents reach ~85% on short tasks and 20.6% on
long-horizon ones, so every executable unit is kept on the short side of that
line, and publication of a skill that exceeds the limit is refused.

Read `docs/ESPECIFICACION.md` before changing anything about the data model, the
execution engine or the certification rules.
"""

__version__ = "1.0.0"

from .config import Config

__all__ = ["Config", "__version__"]
