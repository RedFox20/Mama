"""Compare two mamabuild version strings. See build_names.py for the version of a PACKAGE."""


def parse_version(version: str) -> tuple:
    """'0.13.01' -> (0, 13, 1). Segments parse as ints so zero-padding is irrelevant and 0.13 ranks
    ABOVE 0.9 (a plain string compare gets that backwards). The parse drops non-numeric junk in a segment."""
    parts = []
    for segment in str(version).split('.'):
        digits = ''.join(c for c in segment if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) if parts else (0,)


def version_at_least(current: str, required: str) -> bool:
    """True if `current` >= `required`, comparing segment-wise and zero-padding the shorter one
    (so '0.13' < '0.13.01')."""
    cur, req = parse_version(current), parse_version(required)
    width = max(len(cur), len(req))
    return cur + (0,) * (width - len(cur)) >= req + (0,) * (width - len(req))
