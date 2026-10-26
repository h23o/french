from datetime import datetime
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, computed_field


class CardCreate(BaseModel):
    word: str = Field(min_length=1)
    translation: str = Field(min_length=1)
    example_sentence: str | None = None


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
