"""Fill in dictionary data (inflected forms, phrase alternatives, gender) for saved cards."""

import asyncio
import logging

from sqlalchemy import select

from app.services.external_api import fetch_phrase_forms, fetch_word_info
from app.services.text_processor import tokenize
from database import SessionLocal
from models import Card

logger = logging.getLogger(__name__)


def is_phrase(word: str) -> bool:
    return len(tokenize(word)) > 1


async def fetch_card_data(word: str) -> dict | None:
    """Gender, forms and (for multi-word cards) phrase alternatives; None if the dictionary was unreachable."""
    if not is_phrase(word):
        info = await fetch_word_info(word)
        return info and {**info, "phrase_forms": None}
    info, phrase_forms = await asyncio.gather(fetch_word_info(word), fetch_phrase_forms(word))
    if info is None or phrase_forms is None:
        return None
    return {**info, "phrase_forms": phrase_forms}


def apply_card_data(card: Card, data: dict | None, fill_gender: bool) -> None:
    """Store fetched data on a card; with data=None it stays unfetched (NULL) for a retry at startup."""
    if data is None:
        return
    card.forms = data["forms"]
    card.phrase_forms = data["phrase_forms"]
    if fill_gender and card.gender is None:
        card.gender = data["gender"]


def needs_data(card_word: str, forms, phrase_forms) -> bool:
    return forms is None or (phrase_forms is None and is_phrase(card_word))


async def enrich_missing_cards() -> None:
    """Fetch data for cards that don't have it yet (older cards, or saves made while offline)."""
    with SessionLocal() as db:
        rows = db.execute(select(Card.id, Card.word, Card.forms, Card.phrase_forms)).all()
    pending = [(card_id, word) for card_id, word, forms, phrase_forms in rows if needs_data(word, forms, phrase_forms)]
    if pending:
        logger.info("Fetching dictionary data for %d card(s)", len(pending))

    for card_id, word in pending:
        data = await fetch_card_data(word)
        if data is None:
            return  # dictionary unreachable; try again next startup
        with SessionLocal() as db:
            card = db.get(Card, card_id)
            if card is None or card.word != word:
                continue  # deleted or renamed meanwhile
            apply_card_data(card, data, fill_gender=True)
            db.commit()
