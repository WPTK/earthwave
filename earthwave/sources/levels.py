"""One tsunami alert level scale across every source."""

WARNING = "WARNING"
ADVISORY = "ADVISORY"
WATCH = "WATCH"
THREAT = "THREAT"
INFORMATION = "INFORMATION"
CANCELLATION = "CANCELLATION"

# Higher wins when two sources report the same product.
RANK = {CANCELLATION: 0, INFORMATION: 1, THREAT: 2, WATCH: 3, ADVISORY: 4, WARNING: 5}
ACTIONABLE = frozenset({WARNING, ADVISORY, WATCH, THREAT})


def level_from_text(text: str) -> str | None:
    """Level named by an event or title string, or None when it names none."""
    low = text.lower()
    if "cancel" in low:
        return CANCELLATION
    for word, level in (
        ("warning", WARNING),
        ("advisory", ADVISORY),
        ("watch", WATCH),
        ("threat", THREAT),
        ("information", INFORMATION),
    ):
        if word in low:
            return level
    return None
