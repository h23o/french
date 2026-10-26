"""Reading stats for texts saved in the library."""

from sqlalchemy.orm import Session

from app.services.text_processor import CardIndex, process_text
from models import SavedText

PREVIEW_CHARS = 160


def summarize(db: Session, text: SavedText, index: CardIndex) -> dict:
    # Elided clitics (l', d', qu'...) aren't vocabulary on their own; leave them out of the counts.
    tokens = [t for t in process_text(db, text.body, index) if not t["normalized"].endswith("'")]
    known = [t["in_database"] or t["phrase"] is not None for t in tokens]
    new_words = {t["normalized"] for t, k in zip(tokens, known) if not k}
    preview = " ".join(text.body.split())
    return {
        "id": text.id,
        "title": text.title,
        "preview": preview if len(preview) <= PREVIEW_CHARS else preview[:PREVIEW_CHARS].rsplit(" ", 1)[0] + "…",
        "word_count": len(tokens),
        "known_pct": round(100 * sum(known) / len(tokens)) if tokens else 0,
        "new_words": len(new_words),
        "updated_at": text.updated_at,
    }
