from __future__ import annotations


def slug(name: str) -> str:
    """A config-key/object-id-safe slug (lowercase, non-alnum -> '_')."""
    out = "".join(c if c.isalnum() else "_" for c in str(name).lower())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")
