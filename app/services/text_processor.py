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


Match = tuple[int, str, bool]  # (card_id, card_word, is_due)


class CardIndex:
    """Saved cards, indexed for matching text against them.

    A token matches a card's own word first, then any of its inflected forms
    ("maisons", "vais"). Multi-word cards ("il y a") match as token sequences.
    Built once per request: SQLite's lower() only folds ASCII, so matching
    happens in Python to handle "École" == "école".
    """

    def __init__(self, db: Session):
        self.exact: dict[str, Match] = {}
        self.by_form: dict[str, Match] = {}
        self.phrases: dict[tuple[str, ...], Match] = {}
        now = utcnow()
        # Ordered by next_review so duplicate cards resolve to the most overdue one.
        rows = db.execute(
            select(Card.id, Card.word, Card.next_review, Card.forms).order_by(Card.next_review, Card.id)
        )
        for card_id, word, next_review, forms in rows:
            match = (card_id, word, next_review <= now)
            self.exact.setdefault(normalize_word(word), match)
            for form in forms or []:
                self.by_form.setdefault(form, match)
            parts = tuple(normalize_word(t) for t in tokenize(word))
            if len(parts) > 1:
                self.phrases.setdefault(parts, match)

    def match_word(self, normalized: str) -> Match | None:
        return self.exact.get(normalized) or self.by_form.get(normalized)

    def match_text(self, text: str, *, forms: bool = True) -> Match | None:
        """Card for a whole word or phrase, ignoring punctuation and spacing ("comment ça va ?").

        forms=False matches only a card for that exact word: the word bank lists base forms,
        and "été" (summer) shouldn't count as known just because it's a form of "être".
        """
        parts = tuple(normalize_word(t) for t in tokenize(text))
        if len(parts) == 1:
            return self.match_word(parts[0]) if forms else self.exact.get(parts[0])
        return self.phrases.get(parts)


def match_status(match: Match | None) -> dict:
    card_id, card_word, is_due = match or (None, None, False)
    return {"in_database": card_id is not None, "card_id": card_id, "card_word": card_word, "is_due": is_due}


def process_text(db: Session, text: str, index: CardIndex | None = None) -> list[dict]:
    tokens = tokenize(text)
    if not tokens:
        return []
    index = index or CardIndex(db)

    normalized_tokens = [normalize_word(t) for t in tokens]
    phrase_at = _match_phrases(normalized_tokens, index.phrases)
    return [
        {"token": token, "normalized": normalized, **match_status(index.match_word(normalized)), "phrase": phrase}
        for token, normalized, phrase in zip(tokens, normalized_tokens, phrase_at)
    ]


def _match_phrases(tokens: list[str], phrases: dict[tuple[str, ...], Match]) -> list[dict | None]:
    """For each token, the phrase card it belongs to (exact token sequence), if any.

    Longer phrases are matched first so "il y a" wins over "il y"; phrases never overlap.
    """
    phrase_at: list[dict | None] = [None] * len(tokens)
    for parts, (card_id, card_word, is_due) in sorted(phrases.items(), key=lambda p: -len(p[0])):
        n = len(parts)
        for i in range(len(tokens) - n + 1):
            if tuple(tokens[i : i + n]) == parts and all(p is None for p in phrase_at[i : i + n]):
                for j in range(i, i + n):
                    phrase_at[j] = {
                        "card_id": card_id,
                        "card_word": card_word,
                        "is_due": is_due,
                        "first": j == i,
                        "last": j == i + n - 1,
                    }
    return phrase_at
