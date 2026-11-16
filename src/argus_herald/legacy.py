"""The pip-release names, still honoured for one minor version (removed in 0.3).

Each fallback logs a warning that names its replacement:
- ``PIP_RELEASE_*`` environment variables (``ARGUS_HERALD_*`` wins when both are set);
- the ``.pip-release/`` data directory, moved to ``.argus-herald/`` on first run;
- the deny-list at ``~/.config/pip-release/``, read when ``~/.config/argus-herald/`` has none;
- the ``pip_release.platforms`` entry-point group and the ``pip_release`` import name;
- the ``pip-release`` command.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from pathlib import Path

log = logging.getLogger("argus_herald")

ENV_PREFIX = "ARGUS_HERALD_"
LEGACY_ENV_PREFIX = "PIP_RELEASE_"
DATA_DIR = ".argus-herald"
LEGACY_DATA_DIR = ".pip-release"
LEGACY_PLATFORM_GROUP = "pip_release.platforms"

_warned: set[str] = set()


def warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning("%s", message)


def adopt_legacy_env(environ: MutableMapping[str, str]) -> list[str]:
    """Copy each PIP_RELEASE_X to ARGUS_HERALD_X unless that is already set. Returns the adopted names."""
    adopted = []
    for name in sorted(k for k in environ if k.startswith(LEGACY_ENV_PREFIX)):
        new = ENV_PREFIX + name[len(LEGACY_ENV_PREFIX) :]
        if new in environ:
            continue
        environ[new] = environ[name]
        adopted.append(name)
        warn_once(f"env:{name}", f"{name} is deprecated and stops working in 0.3: rename it to {new}.")
    return adopted


def migrate_data_dir(store_path: Path) -> bool:
    """Move a .pip-release/ data directory to .argus-herald/ the first time the new name is used.

    Only a store under a directory named .argus-herald is migrated, and only when that directory
    doesn't exist yet: an explicitly configured path is left where it is.
    """
    new_dir = store_path.parent
    if new_dir.name != DATA_DIR or new_dir.exists():
        return False
    old_dir = new_dir.with_name(LEGACY_DATA_DIR)
    if not (old_dir / store_path.name).is_file():
        return False
    try:
        old_dir.rename(new_dir)
    except OSError:  # the UI and the watcher start together: the other one moved it first
        return False
    log.warning("Moved %s to %s (the data directory's new name).", old_dir, new_dir)
    return True
