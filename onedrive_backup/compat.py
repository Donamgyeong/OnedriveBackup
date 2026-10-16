"""Compatibility helpers for the supported Python 3.8+ runtime."""

from pathlib import Path


def is_path_within(path, root):
    """Return True when path is root or a descendant (Python 3.8 compatible)."""
    path = Path(path)
    root = Path(root)
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
