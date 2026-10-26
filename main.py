import pathlib
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Path
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

import models
import schemas
from app.services.external_api import ExternalAPIError, lookup_word
from app.services.text_processor import process_text
from database import Base, engine, get_db, utcnow
from srs import calculate_sm2


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="French Vocabulary SRS", lifespan=lifespan)

INDEX_HTML = pathlib.Path(__file__).parent / "app" / "static" / "index.html"


@app.get("/", include_in_schema=False)
def reader():
    return FileResponse(INDEX_HTML)


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


@app.post("/api/process-text", response_model=list[schemas.TokenResponse])
def process_text_endpoint(payload: schemas.ProcessTextRequest, db: Session = Depends(get_db)):
    return process_text(db, payload.text)


@app.get("/api/lookup/{word}", response_model=schemas.LookupResponse)
async def lookup(word: str = Path(min_length=1, max_length=100)):
    try:
        result = await lookup_word(word)
    except ExternalAPIError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream lookup failed: {exc}")
    if not result["meanings"] and not result["examples"]:
        raise HTTPException(status_code=404, detail=f"No definitions or examples found for '{word}'")
    return result
