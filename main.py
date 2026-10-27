import asyncio
import pathlib
from contextlib import asynccontextmanager
from datetime import timezone
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

import models
import schemas
from app.services import tts_cache
from app.services.enrichment import apply_card_data, enrich_missing_cards, fetch_card_data
from app.services.external_api import ExternalAPIError, fetch_conjugation, lookup_word
from app.services.library import summarize
from app.services.text_processor import CardIndex, process_text
from app.services.word_bank import bank_phrases, bank_words
from database import Base, add_missing_columns, engine, get_db, utcnow
from srs import calculate_sm2


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    add_missing_columns()
    # Fetch forms/gender for cards saved before that existed, without delaying startup.
    backfill = asyncio.create_task(enrich_missing_cards())
    yield
    backfill.cancel()


app = FastAPI(title="French Vocabulary SRS", lifespan=lifespan)

STATIC_DIR = pathlib.Path(__file__).parent / "app" / "static"


@app.get("/", include_in_schema=False)
def reader():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/manifest.json", include_in_schema=False)
def manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


# Served from the root so the worker's scope covers every page.
@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


app.mount("/icons", StaticFiles(directory=STATIC_DIR / "icons"), name="icons")


@app.get("/review", include_in_schema=False)
def review_page():
    return FileResponse(STATIC_DIR / "review.html")


@app.get("/my-cards", include_in_schema=False)
def cards_page():
    return FileResponse(STATIC_DIR / "cards.html")


@app.get("/library", include_in_schema=False)
def library_page():
    return FileResponse(STATIC_DIR / "library.html")


@app.post("/cards/", response_model=schemas.CardResponse, status_code=201)
async def create_card(card: schemas.CardCreate, db: Session = Depends(get_db)):
    db_card = models.Card(**card.model_dump())
    # Fetch inflected forms now so the reader matches "maisons" as soon as "maison" is saved.
    # If the dictionary is unreachable the card is still saved and forms are retried at startup.
    data = await fetch_card_data(db_card.word)
    apply_card_data(db_card, data, fill_gender="gender" not in card.model_fields_set)
    db.add(db_card)
    db.commit()
    db.refresh(db_card)
    return db_card


@app.get("/cards/", response_model=list[schemas.CardResponse])
def list_cards(db: Session = Depends(get_db)):
    stmt = select(models.Card).order_by(models.Card.created_at.desc(), models.Card.id.desc())
    return db.scalars(stmt).all()


@app.patch("/cards/{card_id}", response_model=schemas.CardResponse)
async def update_card(card_id: int, changes: schemas.CardUpdate, db: Session = Depends(get_db)):
    card = db.get(models.Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")

    updates = changes.model_dump(exclude_unset=True)
    if "example_sentence" in updates:
        updates["example_sentence"] = updates["example_sentence"] or None  # "" clears it
    word_changed = "word" in updates and updates["word"] != card.word
    for field, value in updates.items():
        setattr(card, field, value)
    if word_changed:
        # The old forms belong to the old word; keep whatever gender the user has set.
        card.forms = card.phrase_forms = None
        apply_card_data(card, await fetch_card_data(card.word), fill_gender=False)
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

    reviewed_at = None
    if review.reviewed_at is not None:
        reviewed_at = review.reviewed_at
        if reviewed_at.tzinfo is not None:
            reviewed_at = reviewed_at.astimezone(timezone.utc).replace(tzinfo=None)
        reviewed_at = min(reviewed_at, utcnow())
        # A queued review that was already applied (a resend), or the card was reviewed
        # again since: keep the result that's there.
        if card.next_review > reviewed_at:
            return card

    card.repetitions, card.interval, card.easiness, card.next_review = calculate_sm2(
        review.quality, card.repetitions, card.interval, card.easiness, now=reviewed_at
    )
    db.commit()
    db.refresh(card)
    return card


@app.get("/texts/", response_model=list[schemas.TextSummary])
def list_texts(db: Session = Depends(get_db)):
    index = CardIndex(db)
    texts = db.scalars(select(models.SavedText).order_by(models.SavedText.updated_at.desc())).all()
    return [summarize(db, text, index) for text in texts]


@app.post("/texts/", response_model=schemas.TextResponse, status_code=201)
def create_text(text: schemas.TextCreate, db: Session = Depends(get_db)):
    db_text = models.SavedText(**text.model_dump())
    db.add(db_text)
    db.commit()
    db.refresh(db_text)
    return db_text


@app.get("/texts/{text_id}", response_model=schemas.TextResponse)
def get_text(text_id: int, db: Session = Depends(get_db)):
    text = db.get(models.SavedText, text_id)
    if text is None:
        raise HTTPException(status_code=404, detail="Text not found")
    return text


@app.patch("/texts/{text_id}", response_model=schemas.TextResponse)
def update_text(text_id: int, changes: schemas.TextUpdate, db: Session = Depends(get_db)):
    text = db.get(models.SavedText, text_id)
    if text is None:
        raise HTTPException(status_code=404, detail="Text not found")
    for field, value in changes.model_dump(exclude_unset=True).items():
        setattr(text, field, value)
    db.commit()
    db.refresh(text)
    return text


@app.delete("/texts/{text_id}", status_code=204)
def delete_text(text_id: int, db: Session = Depends(get_db)):
    text = db.get(models.SavedText, text_id)
    if text is None:
        raise HTTPException(status_code=404, detail="Text not found")
    db.delete(text)
    db.commit()


@app.get("/api/bank/words", response_model=schemas.BankWordsResponse)
def get_bank_words(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    q: str = "",
    pos: Literal["noun", "verb", "adjective", "adverb", "other"] | None = None,
    hide_known: bool = False,
    db: Session = Depends(get_db),
):
    return bank_words(db, offset=offset, limit=limit, q=q, pos=pos, hide_known=hide_known)


@app.get("/api/bank/phrases", response_model=list[schemas.PhraseCategory])
def get_bank_phrases(db: Session = Depends(get_db)):
    return bank_phrases(db)


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


@app.get("/api/conjugate/{verb}", response_model=schemas.ConjugationResponse)
async def conjugate(verb: str = Path(min_length=1, max_length=60)):
    try:
        result = await fetch_conjugation(verb)
    except ExternalAPIError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream lookup failed: {exc}")
    if result is None:
        raise HTTPException(status_code=404, detail=f"No conjugation found for '{verb}'")
    return result


# The same text always produces the same audio, so browsers may cache it indefinitely.
AUDIO_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


# Path parameters can't be optional, so /api/tts (no lang) is the "defaults to fr" form.
@app.get("/api/tts", response_class=StreamingResponse, responses={200: {"content": {"audio/mpeg": {}}}})
@app.get("/api/tts/{lang}", response_class=StreamingResponse, responses={200: {"content": {"audio/mpeg": {}}}})
async def text_to_speech(
    text: str = Query(min_length=1, max_length=500),
    lang: str = "fr",
    speed: Literal["normal", "slow"] = "normal",
):
    voice = tts_cache.VOICES.get(lang)
    if voice is None:
        raise HTTPException(status_code=404, detail=f"Unsupported language '{lang}'")
    if not tts_cache.normalize_text(text):
        raise HTTPException(status_code=422, detail="text must not be blank")

    path = tts_cache.cache_path(text, voice, speed)
    if path.exists():
        return FileResponse(path, media_type="audio/mpeg", headers=AUDIO_HEADERS)

    try:
        stream = await tts_cache.open_audio_stream(text, voice, path, speed)
    except tts_cache.TTSError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return StreamingResponse(stream, media_type="audio/mpeg", headers=AUDIO_HEADERS)
