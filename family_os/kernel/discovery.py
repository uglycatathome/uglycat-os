"""Plugin auto-discovery for Family AI OS.

``discover_plugins()`` scans a directory for plugin folders containing
``plugin.py`` files that export ``Skill`` subclasses.  Folders whose names
begin with ``_`` or ``.`` are skipped.  A single failing plugin never
blocks discovery of the rest -- a warning is logged and discovery continues.
"""

import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from typing import List, Type, Union

from family_os.kernel.skill import Skill


__all__ = ["discover_plugins"]

logger = logging.getLogger(__name__)


def discover_plugins(plugin_dir: Union[str, Path]) -> List[Type[Skill]]:
    """Scan *plugin_dir* for folders containing ``plugin.py`` with ``Skill``
    subclasses.

    Parameters
    ----------
    plugin_dir : str | Path
        Directory to scan for plugin folders.

    Returns
    -------
    list[Type[Skill]]
        All ``Skill`` subclasses found across valid plugin folders.
    """
    found: List[Type[Skill]] = []
    base = Path(plugin_dir)

    if not base.is_dir():
        return found

    for entry in sorted(base.iterdir()):
        # Skip non-directories and hidden/private folders
        if not entry.is_dir():
            continue
        if entry.name.startswith("_") or entry.name.startswith("."):
            continue

        plugin_path = entry / "plugin.py"
        if not plugin_path.is_file():
            continue

        try:
            # Dynamically import the plugin module
            spec = importlib.util.spec_from_file_location(
                f"{entry.name}.plugin", str(plugin_path)
            )
            if spec is None or spec.loader is None:
                logger.warning("Could not load spec for %s", plugin_path)
                continue

            mod = importlib.util.module_from_spec(spec)

            # Ensure parent package is registered in sys.modules so that
            # relative imports (from . import ...) work inside plugin.py.
            # Without this, plugins loaded via spec_from_file_location cannot
            # use relative imports because Python doesn't know about the
            # parent package.
            if entry.name not in sys.modules:
                pkg_init = entry / "__init__.py"
                if pkg_init.is_file():
                    pkg_spec = importlib.util.spec_from_file_location(
                        entry.name, str(pkg_init)
                    )
                    if pkg_spec is not None and pkg_spec.loader is not None:
                        pkg_mod = importlib.util.module_from_spec(pkg_spec)
                        sys.modules[entry.name] = pkg_mod
                        pkg_spec.loader.exec_module(pkg_mod)

            sys.modules[f"{entry.name}.plugin"] = mod
            spec.loader.exec_module(mod)

            # Collect all Skill subclasses in the module
            for _name, obj in inspect.getmembers(mod, inspect.isclass):
                if issubclass(obj, Skill) and obj is not Skill:
                    found.append(obj)

        except Exception:
            logger.warning(
                "Failed to load plugin %s", entry.name, exc_info=True
            )

    return found
