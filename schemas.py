from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, computed_field, field_validator

# Noun gender: masculine, feminine, or either ("un/une élève").
Gender = Literal["m", "f", "mf"]
INDEFINITE_ARTICLES = {"m": "un", "f": "une"}

# SQLite returns naive datetimes; they are stored as UTC, so say so in the JSON ("...Z").
UTCDateTime = Annotated[datetime, AfterValidator(lambda v: v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v)]


class CardCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    word: str = Field(min_length=1)
    translation: str = Field(min_length=1)
    example_sentence: str | None = None
    # Omit to take the dictionary's gender; send null to save explicitly without one.
    gender: Gender | None = None


class CardUpdate(BaseModel):
    """Partial update: omitted fields are left unchanged."""

    model_config = ConfigDict(str_strip_whitespace=True)

    word: str | None = Field(default=None, min_length=1)
    translation: str | None = Field(default=None, min_length=1)
    example_sentence: str | None = None
    gender: Gender | None = None

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
    gender: Gender | None
    repetitions: int
    interval: int
    easiness: float
    next_review: UTCDateTime
    created_at: UTCDateTime

    @computed_field
    @property
    def audio_url(self) -> str:
        # Say nouns with their article ("une maison") so the gender is heard too.
        article = INDEFINITE_ARTICLES.get(self.gender or "")
        spoken = f"{article} {self.word}" if article else self.word
        return f"/api/tts/fr?text={quote(spoken)}"


class ProcessTextRequest(BaseModel):
    text: str


class PhraseMatch(BaseModel):
    """A multi-word card ("il y a") that this token is part of."""

    card_id: int
    card_word: str
    is_due: bool
    first: bool  # first / last token of the phrase in the text
    last: bool


class TokenResponse(BaseModel):
    token: str
    normalized: str
    in_database: bool
    card_id: int | None
    card_word: str | None  # the saved word that matched; differs from the token for forms ("maisons" -> "maison")
    is_due: bool
    phrase: PhraseMatch | None


class Meaning(BaseModel):
    part_of_speech: str
    definitions: list[str]


class ExampleSentence(BaseModel):
    french: str
    english: str
    tatoeba_id: int


class BaseForm(BaseModel):
    word: str  # "beau"
    description: str  # "feminine singular"
    part_of_speech: str


class LookupResponse(BaseModel):
    word: str
    part_of_speech: str | None  # of the primary (first) meaning
    definition: str | None  # primary definition; null when the word is only an inflection
    gender: Gender | None  # for nouns
    base_forms: list[BaseForm]  # what this word is an inflection of, e.g. suis -> être
    meanings: list[Meaning]
    examples: list[ExampleSentence]


# ---- Text library ----


class TextCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)


class TextUpdate(BaseModel):
    """Partial update: omitted fields are left unchanged."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1)

    @field_validator("title", "body")
    @classmethod
    def not_null(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("cannot be null")
        return value


class TextResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    body: str
    created_at: UTCDateTime
    updated_at: UTCDateTime


class TextSummary(BaseModel):
    id: int
    title: str
    preview: str  # opening of the text
    word_count: int
    known_pct: int  # share of running words covered by your cards (words, forms or phrases)
    new_words: int  # distinct words not yet in your cards
    updated_at: UTCDateTime


# ---- Word bank ----


class CardStatus(BaseModel):
    in_database: bool
    card_id: int | None
    card_word: str | None
    is_due: bool


class BankWord(CardStatus):
    rank: int  # 1 = most frequent
    word: str
    pos: list[str]  # parts of speech, most common use first
    gender: Gender | None
    per_million: float


class Coverage(BaseModel):
    top: int  # the N most frequent words…
    known: int  # …of which this many are in your cards


class BankWordsResponse(BaseModel):
    total: int  # matching the filters
    items: list[BankWord]
    coverage: list[Coverage]


class BankPhrase(CardStatus):
    text: str
    translation: str


class PhraseCategory(BaseModel):
    category: str
    phrases: list[BankPhrase]
