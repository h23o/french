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
    # A card matches its own word first, then any of its inflected forms ("maisons", "vais").
    # Ordered by next_review so duplicate cards resolve to the most overdue one.
    exact: dict[str, tuple[int, str, bool]] = {}
    by_form: dict[str, tuple[int, str, bool]] = {}
    now = utcnow()
    rows = db.execute(
        select(Card.id, Card.word, Card.next_review, Card.forms).order_by(Card.next_review, Card.id)
    )
    for card_id, word, next_review, forms in rows:
        match = (card_id, word, next_review <= now)
        exact.setdefault(normalize_word(word), match)
        for form in forms or []:
            by_form.setdefault(form, match)

    result = []
    for token in tokens:
        normalized = normalize_word(token)
        card_id, card_word, is_due = exact.get(normalized) or by_form.get(normalized) or (None, None, False)
        result.append(
            {
                "token": token,
                "normalized": normalized,
                "in_database": card_id is not None,
                "card_id": card_id,
                "card_word": card_word,
                "is_due": is_due,
            }
        )
    return result
