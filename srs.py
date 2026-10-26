"""SuperMemo-2 (SM-2) spaced repetition scheduling.

Quality ratings:
    5: Perfect recall
    4: Correct after hesitation
    3: Correct with serious difficulty
    2: Incorrect, but easy to remember
    1: Incorrect, remembered
    0: Complete blackout
"""

from datetime import datetime, timedelta

from database import utcnow

MIN_EASINESS = 1.3


def calculate_sm2(
    quality: int,
    repetitions: int,
    interval: int,
    easiness: float,
    now: datetime | None = None,
) -> tuple[int, int, float, datetime]:
    """Return (new_repetitions, new_interval, new_easiness, next_review_datetime)."""
    if not 0 <= quality <= 5:
        raise ValueError(f"quality must be between 0 and 5, got {quality}")

    if quality < 3:
        # Failed recall: start the card over, easiness unchanged.
        repetitions = 0
        interval = 1
    else:
        if repetitions == 0:
            interval = 1
        elif repetitions == 1:
            interval = 6
        else:
            interval = round(interval * easiness)
        repetitions += 1

        easiness += 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)
        easiness = max(MIN_EASINESS, easiness)

    next_review = (now or utcnow()) + timedelta(days=interval)
    return repetitions, interval, easiness, next_review
