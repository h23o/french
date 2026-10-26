from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import Base, utcnow


class Card(Base):
    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    word: Mapped[str] = mapped_column(String, nullable=False, index=True)
    translation: Mapped[str] = mapped_column(String, nullable=False)
    example_sentence: Mapped[str | None] = mapped_column(String, nullable=True)
    gender: Mapped[str | None] = mapped_column(String, nullable=True)  # "m", "f", "mf" for nouns

    # Inflected forms from the dictionary ("maisons", "vais"...) so the reader can match them.
    # NULL means not fetched yet; [] means the dictionary had none.
    forms: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True), nullable=True)

    # SM-2 state
    repetitions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    interval: Mapped[int] = mapped_column(Integer, default=1, nullable=False)  # days
    easiness: Mapped[float] = mapped_column(Float, default=2.5, nullable=False)
    next_review: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class SavedText(Base):
    """A text saved to the library for reading."""

    __tablename__ = "texts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
