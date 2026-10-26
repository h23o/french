from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

import models
import schemas
from database import Base, engine, get_db, utcnow
from srs import calculate_sm2


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="French Vocabulary SRS", lifespan=lifespan)


@app.post("/cards/", response_model=schemas.CardResponse, status_code=201)
def create_card(card: schemas.CardCreate, db: Session = Depends(get_db)):
    db_card = models.Card(**card.model_dump())
    db.add(db_card)
    db.commit()
    db.refresh(db_card)
    return db_card


@app.get("/cards/due", response_model=list[schemas.CardResponse])
def get_due_cards(db: Session = Depends(get_db)):
    stmt = (
        select(models.Card)
        .where(models.Card.next_review <= utcnow())
        .order_by(models.Card.next_review)
    )
    return db.scalars(stmt).all()


@app.post("/cards/{card_id}/review", response_model=schemas.CardResponse)
def review_card(card_id: int, review: schemas.CardReview, db: Session = Depends(get_db)):
    card = db.get(models.Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")

    card.repetitions, card.interval, card.easiness, card.next_review = calculate_sm2(
        review.quality, card.repetitions, card.interval, card.easiness
    )
    db.commit()
    db.refresh(card)
    return card
