"""Fill in dictionary data (inflected forms, gender) for saved cards."""

import logging

from sqlalchemy import select

from app.services.external_api import fetch_word_info
from database import SessionLocal
from models import Card

logger = logging.getLogger(__name__)


def apply_word_info(card: Card, info: dict | None, fill_gender: bool) -> None:
    """Store fetched forms on a card; with info=None (dictionary unreachable) forms stay NULL for a later retry."""
    if info is None:
        return
    card.forms = info["forms"]
    if fill_gender and card.gender is None:
        card.gender = info["gender"]


async def enrich_missing_cards() -> None:
    """Fetch forms for cards that don't have them yet (older cards, or saves made while offline)."""
    with SessionLocal() as db:
        pending = db.execute(select(Card.id, Card.word).where(Card.forms.is_(None))).all()
    if pending:
        logger.info("Fetching dictionary forms for %d card(s)", len(pending))

    for card_id, word in pending:
        info = await fetch_word_info(word)
        if info is None:
            return  # dictionary unreachable; try again next startup
        with SessionLocal() as db:
            card = db.get(Card, card_id)
            if card is None or card.word != word:
                continue  # deleted or renamed meanwhile
            apply_word_info(card, info, fill_gender=True)
            db.commit()
