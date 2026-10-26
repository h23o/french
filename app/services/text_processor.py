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
    phrases: list[tuple[list[str], tuple[int, str, bool]]] = []  # multi-word cards, e.g. "il y a"
    now = utcnow()
    rows = db.execute(
        select(Card.id, Card.word, Card.next_review, Card.forms).order_by(Card.next_review, Card.id)
    )
    for card_id, word, next_review, forms in rows:
        match = (card_id, word, next_review <= now)
        exact.setdefault(normalize_word(word), match)
        for form in forms or []:
            by_form.setdefault(form, match)
        parts = [normalize_word(t) for t in tokenize(word)]
        if len(parts) > 1:
            phrases.append((parts, match))

    normalized_tokens = [normalize_word(t) for t in tokens]
    phrase_at = _match_phrases(normalized_tokens, phrases)

    result = []
    for token, normalized, phrase in zip(tokens, normalized_tokens, phrase_at):
        card_id, card_word, is_due = exact.get(normalized) or by_form.get(normalized) or (None, None, False)
        result.append(
            {
                "token": token,
                "normalized": normalized,
                "in_database": card_id is not None,
                "card_id": card_id,
                "card_word": card_word,
                "is_due": is_due,
                "phrase": phrase,
            }
        )
    return result


def _match_phrases(tokens: list[str], phrases: list[tuple[list[str], tuple[int, str, bool]]]) -> list[dict | None]:
    """For each token, the phrase card it belongs to (exact token sequence), if any.

    Longer phrases are matched first so "il y a" wins over "il y"; phrases never overlap.
    """
    phrase_at: list[dict | None] = [None] * len(tokens)
    for parts, (card_id, card_word, is_due) in sorted(phrases, key=lambda p: -len(p[0])):
        n = len(parts)
        for i in range(len(tokens) - n + 1):
            if tokens[i : i + n] == parts and all(p is None for p in phrase_at[i : i + n]):
                for j in range(i, i + n):
                    phrase_at[j] = {
                        "card_id": card_id,
                        "card_word": card_word,
                        "is_due": is_due,
                        "first": j == i,
                        "last": j == i + n - 1,
                    }
    return phrase_at
