import pathlib
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

import models
import schemas
from app.services import tts_cache
from app.services.external_api import ExternalAPIError, lookup_word
from app.services.text_processor import process_text
from database import Base, engine, get_db, utcnow
from srs import calculate_sm2


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="French Vocabulary SRS", lifespan=lifespan)

STATIC_DIR = pathlib.Path(__file__).parent / "app" / "static"


@app.get("/", include_in_schema=False)
def reader():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/review", include_in_schema=False)
def review_page():
    return FileResponse(STATIC_DIR / "review.html")


@app.get("/my-cards", include_in_schema=False)
def cards_page():
    return FileResponse(STATIC_DIR / "cards.html")


@app.post("/cards/", response_model=schemas.CardResponse, status_code=201)
def create_card(card: schemas.CardCreate, db: Session = Depends(get_db)):
    db_card = models.Card(**card.model_dump())
    db.add(db_card)
    db.commit()
    db.refresh(db_card)
    return db_card


@app.get("/cards/", response_model=list[schemas.CardResponse])
def list_cards(db: Session = Depends(get_db)):
    stmt = select(models.Card).order_by(models.Card.created_at.desc(), models.Card.id.desc())
    return db.scalars(stmt).all()


@app.patch("/cards/{card_id}", response_model=schemas.CardResponse)
def update_card(card_id: int, changes: schemas.CardUpdate, db: Session = Depends(get_db)):
    card = db.get(models.Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")

    updates = changes.model_dump(exclude_unset=True)
    if "example_sentence" in updates:
        updates["example_sentence"] = updates["example_sentence"] or None  # "" clears it
    for field, value in updates.items():
        setattr(card, field, value)
    db.commit()
    db.refresh(card)
    return card


@app.delete("/cards/{card_id}", status_code=204)
def delete_card(card_id: int, db: Session = Depends(get_db)):
    card = db.get(models.Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    db.delete(card)
    db.commit()


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


# The same text always produces the same audio, so browsers may cache it indefinitely.
AUDIO_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


# Path parameters can't be optional, so /api/tts (no lang) is the "defaults to fr" form.
@app.get("/api/tts", response_class=StreamingResponse, responses={200: {"content": {"audio/mpeg": {}}}})
@app.get("/api/tts/{lang}", response_class=StreamingResponse, responses={200: {"content": {"audio/mpeg": {}}}})
async def text_to_speech(text: str = Query(min_length=1, max_length=500), lang: str = "fr"):
    voice = tts_cache.VOICES.get(lang)
    if voice is None:
        raise HTTPException(status_code=404, detail=f"Unsupported language '{lang}'")
    if not tts_cache.normalize_text(text):
        raise HTTPException(status_code=422, detail="text must not be blank")

    path = tts_cache.cache_path(text, voice)
    if path.exists():
        return FileResponse(path, media_type="audio/mpeg", headers=AUDIO_HEADERS)

    try:
        stream = await tts_cache.open_audio_stream(text, voice, path)
    except tts_cache.TTSError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return StreamingResponse(stream, media_type="audio/mpeg", headers=AUDIO_HEADERS)
