"""PopTracker map tracker for OoTRR, generated from the randomizer's own data and logic.

The generator imports the `ootrr` package of an OOTRR checkout (https://github.com/ItZPomme/OOTRR). Importing
`ootrr_tracker.poptracker` puts that checkout on `sys.path`, looked up in this order:
  1. the path given to `use_ootrr()` (the `--ootrr PATH` argument of `python -m ootrr_tracker`);
  2. the `OOTRR_PATH` environment variable;
  3. an `OOTRR` folder next to this repository (`../OOTRR`);
  4. an `ootrr` package that is already importable (e.g. `pip install -e` of OOTRR).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent


class OotrrNotFound(RuntimeError):
    pass


def _is_checkout(path: Path) -> bool:
    return (path / "ootrr" / "__init__.py").is_file() and (path / "ootrr" / "data").is_dir()


_chosen: list[Path | None] = []


def use_ootrr(path: str | os.PathLike | None = None) -> Path | None:
    """Puts the OOTRR checkout on sys.path and returns it (None when an installed `ootrr` package is used).
    Only the first call chooses; later calls without a path return that choice."""
    if _chosen and path is None:
        return _chosen[0]
    _chosen[:] = [_find(path)]
    return _chosen[0]


def _find(path: str | os.PathLike | None) -> Path | None:
    explicit = path or os.environ.get("OOTRR_PATH")
    candidates = [Path(explicit)] if explicit else [REPO_DIR.parent / "OOTRR"]
    for cand in candidates:
        cand = cand.expanduser().resolve()
        if _is_checkout(cand):
            if "ootrr" in sys.modules:
                loaded = Path(sys.modules["ootrr"].__file__).resolve().parent.parent
                if loaded != cand:
                    raise OotrrNotFound(f"ootrr already imported from {loaded}, not {cand}")
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            return cand
        if explicit:
            raise OotrrNotFound(f"{cand} is not an OOTRR checkout (no ootrr/ package with ootrr/data)")
    if importlib.util.find_spec("ootrr") is not None:
        return None
    raise OotrrNotFound("OOTRR not found: clone https://github.com/ItZPomme/OOTRR next to this repository, "
                        "or pass --ootrr PATH / set OOTRR_PATH")

