"""Where configuration files are read from (Step AC).

Default: config/<name> (development / calibration). A production run activates an immutable production
config set (config/production/vN/) for its duration; every module loader resolves through `path()`, so one
run never mixes files from two config sets. Missing files in an active set raise (never silently fall back).
"""
import contextlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# development file name -> production file name
ALIASES = {"decision.yaml": "decision_rules.yaml", "suppliers.yaml": "supplier.yaml",
           "competitors.yaml": "competitor.yaml", "creatives.yaml": "creative.yaml"}
_ACTIVE = []


def active_dir():
    return _ACTIVE[-1] if _ACTIVE else None


def path(name):
    d = active_dir()
    if d is None:
        return ROOT / "config" / name
    p = Path(d) / ALIASES.get(name, name)
    if not p.exists():
        raise FileNotFoundError(f"config {name} missing from the active config set {d}")
    return p


@contextlib.contextmanager
def active(config_dir):
    _ACTIVE.append(Path(config_dir))
    try:
        yield Path(config_dir)
    finally:
        _ACTIVE.pop()
