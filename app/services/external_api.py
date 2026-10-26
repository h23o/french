"""Look up French words in external dictionary and example-sentence APIs.

Definitions come from the Free Dictionary API (freedictionaryapi.com, built on
Wiktionary data), falling back to Wiktionary's own REST API if it is down.
Example sentences with human English translations come from Tatoeba.
"""

import asyncio
import html
import logging
import re
import unicodedata
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

FREE_DICTIONARY_URL = "https://freedictionaryapi.com/api/v1/entries/fr/{word}"
WIKTIONARY_URL = "https://en.wiktionary.org/api/rest_v1/page/definition/{word}"
TATOEBA_URL = "https://api.tatoeba.org/v1/sentences"

# Wikimedia rejects requests without a descriptive User-Agent.
HEADERS = {"User-Agent": "french-srs/0.1 (personal vocabulary app)"}
TIMEOUT = httpx.Timeout(8.0)

MAX_DEFINITIONS_PER_POS = 5
EXAMPLE_COUNT = 3
# Tatoeba treats hyphens as spaces ("peut-être" also hits "peut être"), so fetch
# extra candidates and prefer the ones containing the exact word.
EXAMPLE_CANDIDATES = 10

TAG_RE = re.compile(r"<[^>]+>")

# Unexpected JSON shapes surface as KeyError/TypeError; bad JSON as ValueError.
UPSTREAM_ERRORS = (httpx.HTTPError, ValueError, KeyError, TypeError)


class ExternalAPIError(Exception):
    """An upstream API could not be reached or returned an unusable response."""


def _strip_html(text: str) -> str:
    return " ".join(html.unescape(TAG_RE.sub("", text)).split())


def _group_meanings(pairs) -> list[dict]:
    """Merge (part_of_speech, definition) pairs into one entry per part of speech, in order."""
    grouped: dict[str, list[str]] = {}
    for pos, definition in pairs:
        definitions = grouped.setdefault(pos, [])
        if definition and definition not in definitions and len(definitions) < MAX_DEFINITIONS_PER_POS:
            definitions.append(definition)
    return [{"part_of_speech": pos, "definitions": defs} for pos, defs in grouped.items() if defs]


async def _free_dictionary(client: httpx.AsyncClient, word: str) -> list[dict]:
    resp = await client.get(FREE_DICTIONARY_URL.format(word=quote(word, safe="")))
    resp.raise_for_status()
    # Unknown words come back as 200 with an empty "entries" list.
    return _group_meanings(
        (entry["partOfSpeech"].lower(), sense["definition"].strip())
        for entry in resp.json().get("entries", [])
        if entry.get("language", {}).get("code") == "fr"
        for sense in entry.get("senses", [])
    )


async def _wiktionary(client: httpx.AsyncClient, word: str) -> list[dict]:
    resp = await client.get(WIKTIONARY_URL.format(word=quote(word, safe="")))
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    # Definitions are HTML fragments; entries are keyed by language code.
    return _group_meanings(
        (entry["partOfSpeech"].lower(), _strip_html(d["definition"]))
        for entry in resp.json().get("fr", [])
        for d in entry.get("definitions", [])
    )


async def fetch_definitions(client: httpx.AsyncClient, word: str) -> list[dict]:
    try:
        return await _free_dictionary(client, word)
    except UPSTREAM_ERRORS as exc:
        logger.warning("Free Dictionary API failed for %r (%r); falling back to Wiktionary", word, exc)
    try:
        return await _wiktionary(client, word)
    except UPSTREAM_ERRORS as exc:
        raise ExternalAPIError(f"Dictionary lookup failed: {exc!r}") from exc


async def fetch_examples(client: httpx.AsyncClient, word: str) -> list[dict]:
    params = {
        "q": word,
        "lang": "fra",
        "sort": "relevance",
        "limit": EXAMPLE_CANDIDATES,
        "word_count": "5-12",  # skip one-word fragments like "Maison !"
        "is_unapproved": "no",
        "is_orphan": "no",
        "trans:lang": "eng",
        "trans:is_direct": "yes",  # translated straight from the French, not via a third language
        "showtrans": "matching",
    }
    try:
        resp = await client.get(TATOEBA_URL, params=params)
        resp.raise_for_status()
        examples = []
        for sentence in resp.json().get("data", []):
            english = next(
                (t["text"] for t in sentence.get("translations", []) if t.get("lang") == "eng"), None
            )
            if english:
                examples.append({"french": sentence["text"], "english": english, "tatoeba_id": sentence["id"]})
    except UPSTREAM_ERRORS as exc:
        raise ExternalAPIError(f"Tatoeba lookup failed: {exc!r}") from exc

    exact = re.compile(rf"(?<!\w){re.escape(word)}(?!\w)", re.IGNORECASE)
    examples.sort(key=lambda ex: not exact.search(ex["french"]))  # stable: keeps relevance order
    return examples[:EXAMPLE_COUNT]


async def lookup_word(word: str) -> dict:
    """Combine definitions and example sentences for a French word.

    If one upstream fails the other's results are still returned; raises
    ExternalAPIError only when both fail.
    """
    word = unicodedata.normalize("NFC", word).strip()
    async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
        meanings, examples = await asyncio.gather(
            fetch_definitions(client, word), fetch_examples(client, word), return_exceptions=True
        )

    for result in (meanings, examples):
        if isinstance(result, BaseException) and not isinstance(result, ExternalAPIError):
            raise result
    if isinstance(meanings, ExternalAPIError) and isinstance(examples, ExternalAPIError):
        raise ExternalAPIError(f"{meanings} / {examples}")
    if isinstance(meanings, ExternalAPIError):
        logger.warning("%s", meanings)
        meanings = []
    if isinstance(examples, ExternalAPIError):
        logger.warning("%s", examples)
        examples = []

    primary = meanings[0] if meanings else None
    return {
        "word": word,
        "part_of_speech": primary["part_of_speech"] if primary else None,
        "definition": primary["definitions"][0] if primary else None,
        "meanings": meanings,
        "examples": examples,
    }
