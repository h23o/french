"""Tokenize French text and flag which words are already saved as flashcards."""

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from database import utcnow
from models import Card

# A run of letters (any script, so é/à/ç/œ are included; digits and _ excluded),
# optionally joined by internal apostrophes or hyphens: "aujourd'hui", "peut-être".
WORD_RE = re.compile(r"[^\W\d_]+(?:['-][^\W\d_]+)*")

# Elided clitics split off the front of a word: "l'homme" -> "l'", "homme".
ELISION_RE = re.compile(r"^(qu|jusqu|lorsqu|puisqu|[cdjlmnst])'(?=.)", re.IGNORECASE)

APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "‘": "'"})


def normalize_word(word: str) -> str:
    """Lookup key for matching tokens against card words (case-insensitive, accents kept)."""
    return unicodedata.normalize("NFC", word).translate(APOSTROPHES).strip().lower()


def tokenize(text: str) -> list[str]:
    # NFC so decomposed accents (e + U+0301) count as letters for the regex.
    text = unicodedata.normalize("NFC", text).translate(APOSTROPHES)
    tokens = []
    for match in WORD_RE.finditer(text):
        word = match.group()
        elision = ELISION_RE.match(word)
        if elision:
            tokens.append(elision.group())
            word = word[elision.end():]
        tokens.append(word)
    return tokens


def process_text(db: Session, text: str) -> list[dict]:
    tokens = tokenize(text)
    if not tokens:
        return []

    # SQLite's lower() only folds ASCII, so match in Python to handle "École" == "école".
    # Ordered by next_review so duplicate cards resolve to the most overdue one.
    cards: dict[str, tuple[int, bool]] = {}
    now = utcnow()
    rows = db.execute(select(Card.id, Card.word, Card.next_review).order_by(Card.next_review, Card.id))
    for card_id, word, next_review in rows:
        cards.setdefault(normalize_word(word), (card_id, next_review <= now))

    result = []
    for token in tokens:
        normalized = normalize_word(token)
        card_id, is_due = cards.get(normalized, (None, False))
        result.append(
            {
                "token": token,
                "normalized": normalized,
                "in_database": card_id is not None,
                "card_id": card_id,
                "is_due": is_due,
            }
        )
    return result
