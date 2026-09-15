from __future__ import annotations


def slug(name: str) -> str:
    """A config-key/object-id-safe slug (lowercase, non-alnum -> '_')."""
    out = "".join(c if c.isalnum() else "_" for c in str(name).lower())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


def titleize(key: str) -> str:
    """Human-friendly label from a zone key: 'front_slope' -> 'Front Slope'.

    Zones are stored only by their slug key, so this is what turns that key
    into a readable device name. Falls back to the raw key if titling would
    yield nothing (e.g. an all-punctuation key)."""
    label = str(key).replace("_", " ").replace("-", " ").strip().title()
    return label or str(key)
