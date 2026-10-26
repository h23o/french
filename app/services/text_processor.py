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


def tokenize_spans(text: str) -> list[tuple[str, int, int]]:
    """Tokens with their [start, end) offsets in the NFC-normalized text."""
    # NFC so decomposed accents (e + U+0301) count as letters for the regex. The
    # apostrophe swap is one-for-one, so offsets also index the NFC text.
    text = unicodedata.normalize("NFC", text).translate(APOSTROPHES)
    spans = []
    for match in WORD_RE.finditer(text):
        word, start = match.group(), match.start()
        elision = ELISION_RE.match(word)
        if elision:
            spans.append((elision.group(), start, start + elision.end()))
            word, start = word[elision.end():], start + elision.end()
        spans.append((word, start, start + len(word)))
    return spans


def tokenize(text: str) -> list[str]:
    return [token for token, _, _ in tokenize_spans(text)]


def phrase_alternatives(word: str, phrase_forms: list[list[str]] | None) -> list[set[str]]:
    """What each token of a card may look like in text.

    Multi-word cards store per-token alternatives from the dictionary, so "avoir besoin de"
    also matches "ai besoin de" and "il y a" matches "il y avait". Without them, exact tokens.
    """
    if phrase_forms:
        return [set(alternatives) for alternatives in phrase_forms]
    return [{normalize_word(t)} for t in tokenize(word)]


def find_cloze(sentence: str, word: str, forms: list[str] | None, phrase_forms: list[list[str]] | None) -> dict | None:
    """Split an example sentence around the card's word (or one of its forms) for a fill-in-the-blank.

    "Je vais au marché." for the card "aller" -> {"before": "Je ", "answer": "vais", "after": " au marché."}
    """
    text = unicodedata.normalize("NFC", sentence)
    spans = tokenize_spans(text)
    normalized = [normalize_word(token) for token, _, _ in spans]
    alternatives = phrase_alternatives(word, phrase_forms)
    if len(alternatives) == 1:
        alternatives = [alternatives[0] | set(forms or [])]
    n = len(alternatives)
    for i in range(len(spans) - n + 1):
        if all(normalized[i + k] in alternatives[k] for k in range(n)):
            start, end = spans[i][1], spans[i + n - 1][2]
            return {"before": text[:start], "answer": text[start:end], "after": text[end:]}
    return None


Match = tuple[int, str, bool]  # (card_id, card_word, is_due)


class CardIndex:
    """Saved cards, indexed for matching text against them.

    A token matches a card's own word first, then any of its inflected forms
    ("maisons", "vais"). Multi-word cards ("il y a") match as token sequences,
    each token allowing its dictionary alternatives (see phrase_alternatives).
    Built once per request: SQLite's lower() only folds ASCII, so matching
    happens in Python to handle "École" == "école".
    """

    def __init__(self, db: Session):
        self.exact: dict[str, Match] = {}
        self.by_form: dict[str, Match] = {}
        self.phrases: list[tuple[list[set[str]], Match]] = []
        self.phrase_keys: dict[tuple[str, ...], Match] = {}  # exact token sequence, for match_text
        now = utcnow()
        # Ordered by next_review so duplicate cards resolve to the most overdue one.
        rows = db.execute(
            select(Card.id, Card.word, Card.next_review, Card.forms, Card.phrase_forms).order_by(Card.next_review, Card.id)
        )
        for card_id, word, next_review, forms, phrase_forms in rows:
            match = (card_id, word, next_review <= now)
            self.exact.setdefault(normalize_word(word), match)
            for form in forms or []:
                self.by_form.setdefault(form, match)
            parts = tuple(normalize_word(t) for t in tokenize(word))
            if len(parts) > 1:
                self.phrases.append((phrase_alternatives(word, phrase_forms), match))
                self.phrase_keys.setdefault(parts, match)

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
        return self.phrase_keys.get(parts)


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


def _match_phrases(tokens: list[str], phrases: list[tuple[list[set[str]], Match]]) -> list[dict | None]:
    """For each token, the phrase card it belongs to, if any.

    Longer phrases are matched first so "il y a" wins over "il y"; phrases never overlap.
    """
    phrase_at: list[dict | None] = [None] * len(tokens)
    for alternatives, (card_id, card_word, is_due) in sorted(phrases, key=lambda p: -len(p[0])):
        n = len(alternatives)
        for i in range(len(tokens) - n + 1):
            if all(tokens[i + k] in alternatives[k] for k in range(n)) and all(p is None for p in phrase_at[i : i + n]):
                for j in range(i, i + n):
                    phrase_at[j] = {
                        "card_id": card_id,
                        "card_word": card_word,
                        "is_due": is_due,
                        "first": j == i,
                        "last": j == i + n - 1,
                    }
    return phrase_at
