from datetime import datetime, timezone
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


class CardCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    word: str = Field(min_length=1)
    translation: str = Field(min_length=1)
    example_sentence: str | None = None


class CardUpdate(BaseModel):
    """Partial update: omitted fields are left unchanged."""

    model_config = ConfigDict(str_strip_whitespace=True)

    word: str | None = Field(default=None, min_length=1)
    translation: str | None = Field(default=None, min_length=1)
    example_sentence: str | None = None

    @field_validator("word", "translation")
    @classmethod
    def not_null(cls, value: str | None) -> str:
        # Only runs for fields actually sent, so this rejects an explicit null.
        if value is None:
            raise ValueError("cannot be null")
        return value


class CardReview(BaseModel):
    quality: int = Field(ge=0, le=5)


class CardResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    word: str
    translation: str
    example_sentence: str | None
    repetitions: int
    interval: int
    easiness: float
    next_review: datetime
    created_at: datetime

    @field_validator("next_review", "created_at")
    @classmethod
    def mark_utc(cls, value: datetime) -> datetime:
        # SQLite returns naive datetimes; they are stored as UTC, so say so in the JSON ("...Z").
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    @computed_field
    @property
    def audio_url(self) -> str:
        return f"/api/tts/fr?text={quote(self.word)}"


class ProcessTextRequest(BaseModel):
    text: str


class TokenResponse(BaseModel):
    token: str
    normalized: str
    in_database: bool
    card_id: int | None
    is_due: bool


class Meaning(BaseModel):
    part_of_speech: str
    definitions: list[str]


class ExampleSentence(BaseModel):
    french: str
    english: str
    tatoeba_id: int


class LookupResponse(BaseModel):
    word: str
    part_of_speech: str | None  # of the primary (first) meaning
    definition: str | None  # primary definition
    meanings: list[Meaning]
    examples: list[ExampleSentence]
