"""Look up French words in external dictionary and example-sentence APIs.

Definitions come from the Free Dictionary API (freedictionaryapi.com, built on
Wiktionary data), falling back to Wiktionary's own REST API if it is down.
Example sentences with human English translations come from Tatoeba.

Besides definitions, the Free Dictionary data gives us:
  - base forms: "belle" is tagged "form of" with the definition "feminine singular of beau"
  - gender: noun senses are tagged "masculine" / "feminine"
  - inflected forms: plurals, adjective agreement, full verb conjugation tables
The Wiktionary fallback only provides definitions.
"""

import asyncio
import html
import logging
import re
import unicodedata
from urllib.parse import quote

import httpx

from app.services.text_processor import normalize_word, tokenize

logger = logging.getLogger(__name__)

FREE_DICTIONARY_URL = "https://freedictionaryapi.com/api/v1/entries/fr/{word}"
WIKTIONARY_URL = "https://en.wiktionary.org/api/rest_v1/page/definition/{word}"
TATOEBA_URL = "https://api.tatoeba.org/v1/sentences"

# Wikimedia rejects requests without a descriptive User-Agent.
HEADERS = {"User-Agent": "french-srs/0.1 (personal vocabulary app)"}
TIMEOUT = httpx.Timeout(8.0)

MAX_DEFINITIONS_PER_POS = 5
MAX_BASE_FORMS = 3
EXAMPLE_COUNT = 3
# Tatoeba treats hyphens as spaces ("peut-être" also hits "peut être"), so fetch
# extra candidates and prefer the ones containing the exact word.
EXAMPLE_CANDIDATES = 10

TAG_RE = re.compile(r"<[^>]+>")
# The base form is the last "of X" in a form-of definition: "plural of maison",
# "inflection of manger:", "plural of œil, eyes", and multi-word ones like "… of y avoir: there is".
FORM_OF_RE = re.compile(r"\bof\s+([^,;:()“”\"]+?)\s*(?=[,;:(“\"]|$)")
# A single French word; rejects conjugation-table junk ("fr-conj-auto", "avoir + past participle", IPA).
FRENCH_WORD_RE = re.compile(r"^[a-zàâäæçéèêëîïôöœùûüÿ]+(?:['-][a-zàâäæçéèêëîïôöœùûüÿ]+)*$")
TABLE_TAGS = {"table-tags", "inflection-template"}

# Unexpected JSON shapes surface as KeyError/TypeError; bad JSON as ValueError.
UPSTREAM_ERRORS = (httpx.HTTPError, ValueError, KeyError, TypeError)


class ExternalAPIError(Exception):
    """An upstream API could not be reached or returned an unusable response."""


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)


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


def _empty_entry() -> dict:
    return {"meanings": [], "part_of_speech": None, "definition": None, "gender": None, "base_forms": [], "forms": []}


# ---- Free Dictionary parsing ----


def _is_form_of(sense: dict) -> bool:
    return "form of" in sense.get("tags", []) or sense["definition"].startswith("inflection of ")


def _base_forms(entries: list[dict], word: str) -> list[dict]:
    """Words this one is an inflection of, e.g. suis -> être, suivre."""
    found: dict[str, dict] = {}
    for entry in entries:
        for sense in entry["senses"]:
            definition = sense["definition"]
            # Skip qualified readings like "(Louisiana) past participle of aller".
            if not _is_form_of(sense) or definition.startswith("("):
                continue
            matches = FORM_OF_RE.findall(definition)
            if not matches:
                continue
            base = normalize_word(matches[-1])
            if base != normalize_word(word) and base not in found:
                found[base] = {
                    "word": base,
                    "description": definition[: definition.rfind(" of ")].strip(),
                    "part_of_speech": entry["partOfSpeech"].lower(),
                }
    return list(found.values())[:MAX_BASE_FORMS]


def _gender(entries: list[dict]) -> str | None:
    """'m', 'f' or 'mf' when the word's primary entry is a noun; otherwise None."""
    if not entries or entries[0]["partOfSpeech"].lower() != "noun":
        return None
    for sense in entries[0]["senses"]:
        tags = set(sense.get("tags", []))
        masculine, feminine = "masculine" in tags, "feminine" in tags
        if masculine or feminine:
            return "mf" if masculine and feminine else "m" if masculine else "f"
    return None


def _forms(entries: list[dict]) -> list[str]:
    """Inflected forms (plurals, agreement, conjugations) that should count as this word."""
    forms: list[str] = []
    for entry in entries:
        is_noun = entry["partOfSpeech"].lower() == "noun"
        for form in entry.get("forms", []):
            tags = set(form.get("tags", []))
            # A noun's non-plural "forms" are separate words (cheval -> jument).
            if tags & TABLE_TAGS or (is_noun and "plural" not in tags):
                continue
            normalized = normalize_word(form["word"])
            if FRENCH_WORD_RE.match(normalized) and normalized not in forms:
                forms.append(normalized)
    return forms


def _primary_definition(sense: dict) -> str | None:
    """The sense's definition, or None for a bare inflection ("feminine singular of beau" is a poor translation).

    Some form-of senses carry a gloss after a colon, e.g. "… of y avoir: there is, there are".
    """
    definition = sense["definition"].strip()
    if not _is_form_of(sense):
        return definition
    return definition.partition(": ")[2].strip() or None


def _parse_free_dictionary(entries: list[dict], word: str) -> dict:
    if not entries or not entries[0]["senses"]:
        return _empty_entry()
    first_sense = entries[0]["senses"][0]
    return {
        "meanings": _group_meanings(
            (entry["partOfSpeech"].lower(), sense["definition"].strip())
            for entry in entries
            for sense in entry["senses"]
        ),
        "part_of_speech": entries[0]["partOfSpeech"].lower(),
        "definition": _primary_definition(first_sense),
        "gender": _gender(entries),
        "base_forms": _base_forms(entries, word),
        "forms": _forms(entries),
    }


async def _free_dictionary_entries(client: httpx.AsyncClient, word: str) -> list[dict]:
    resp = await client.get(FREE_DICTIONARY_URL.format(word=quote(word, safe="")))
    resp.raise_for_status()
    # Unknown words come back as 200 with an empty "entries" list.
    return [
        entry
        for entry in resp.json().get("entries", [])
        if entry.get("language", {}).get("code") == "fr"
    ]


async def _wiktionary(client: httpx.AsyncClient, word: str) -> dict:
    resp = await client.get(WIKTIONARY_URL.format(word=quote(word, safe="")))
    if resp.status_code == 404:
        return _empty_entry()
    resp.raise_for_status()
    # Definitions are HTML fragments; entries are keyed by language code.
    meanings = _group_meanings(
        (entry["partOfSpeech"].lower(), _strip_html(d["definition"]))
        for entry in resp.json().get("fr", [])
        for d in entry.get("definitions", [])
    )
    entry = _empty_entry()
    if meanings:
        entry.update(
            meanings=meanings,
            part_of_speech=meanings[0]["part_of_speech"],
            definition=meanings[0]["definitions"][0],
        )
    return entry


async def fetch_definitions(client: httpx.AsyncClient, word: str) -> dict:
    try:
        return _parse_free_dictionary(await _free_dictionary_entries(client, word), word)
    except UPSTREAM_ERRORS as exc:
        logger.warning("Free Dictionary API failed for %r (%r); falling back to Wiktionary", word, exc)
    try:
        return await _wiktionary(client, word)
    except UPSTREAM_ERRORS as exc:
        raise ExternalAPIError(f"Dictionary lookup failed: {exc!r}") from exc


async def fetch_word_info(word: str) -> dict | None:
    """Gender and inflected forms for a card, or None if the dictionary couldn't be reached."""
    try:
        async with _client() as client:
            entries = await _free_dictionary_entries(client, word)
    except UPSTREAM_ERRORS as exc:
        logger.warning("Couldn't fetch forms for %r: %r", word, exc)
        return None
    return {"gender": _gender(entries), "forms": _forms(entries)}


# ---- Phrase alternatives ----

# Elided and full spellings of the same little word: "avoir besoin de" also matches "besoin d'aide".
ELISIONS = {"de": "d'", "le": "l'", "la": "l'", "je": "j'", "me": "m'", "te": "t'", "se": "s'", "ne": "n'", "que": "qu'", "ce": "c'"}
ELISIONS |= {elided: full for full, elided in ELISIONS.items() if elided != "l'"}
# A reflexive "se" in a saved phrase is "me/te/nous…" once conjugated ("se rendre compte" -> "je me rends compte").
REFLEXIVE_PRONOUNS = ["me", "m'", "te", "t'", "se", "s'", "nous", "vous"]
# The dictionary API has no entry for the single letter "a", which is the "a" of "il y a".
VERB_FORM_FALLBACK = {"a": "avoir"}


def _verb_entries(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e["partOfSpeech"].lower() == "verb"]


async def _verb_forms(client: httpx.AsyncClient, word: str) -> list[str]:
    """Every form of the verb this word is, or is a form of ("a" -> avoir, avait, aura…); [] if not a verb."""
    if word in VERB_FORM_FALLBACK:
        base = VERB_FORM_FALLBACK[word]
        return [base, *_forms(_verb_entries(await _free_dictionary_entries(client, base)))]
    entries = _verb_entries(await _free_dictionary_entries(client, word))
    if not entries:
        return []
    if any(not _is_form_of(sense) for entry in entries for sense in entry["senses"]):
        return [word, *_forms(entries)]
    bases = [b["word"] for b in _base_forms(entries, word) if b["part_of_speech"] == "verb"]
    if not bases:
        return []
    return [bases[0], *_forms(_verb_entries(await _free_dictionary_entries(client, bases[0])))]


async def fetch_phrase_forms(phrase: str) -> list[list[str]] | None:
    """Per-token alternatives for a multi-word card, or None if the dictionary couldn't be reached.

    Only the phrase's first verb is conjugated (plus elision, and a reflexive "se" right
    before that verb), so "il y a" matches "il y avait" and "se rendre compte" matches
    "je me rends compte", but nouns like "compte" don't turn into verbs.
    """
    parts = [normalize_word(t) for t in tokenize(phrase)]
    try:
        async with _client() as client:
            verb_forms = await asyncio.gather(*(_verb_forms(client, part) for part in parts))
    except UPSTREAM_ERRORS as exc:
        logger.warning("Couldn't fetch phrase forms for %r: %r", phrase, exc)
        return None
    verb_at = next((i for i, forms in enumerate(verb_forms) if forms), None)
    alternatives = []
    for i, part in enumerate(parts):
        options = {part}
        if i == verb_at:
            options.update(verb_forms[i])
        if part in ELISIONS:
            options.add(ELISIONS[part])
        if part in ("se", "s'") and verb_at == i + 1:
            options.update(REFLEXIVE_PRONOUNS)
        alternatives.append(sorted(options))
    return alternatives


# ---- Conjugation tables ----

TENSES = [
    ("Présent", {"indicative", "present"}),
    ("Imparfait", {"indicative", "imperfect"}),
    ("Futur", {"indicative", "future"}),
    ("Conditionnel", {"conditional"}),
    ("Subjonctif présent", {"subjunctive", "present"}),
    ("Passé simple", {"historic", "past"}),
]
PERSONS = [
    ("je", {"first-person", "singular"}),
    ("tu", {"second-person", "singular"}),
    ("il/elle", {"third-person", "singular"}),
    ("nous", {"first-person", "plural"}),
    ("vous", {"second-person", "plural"}),
    ("ils/elles", {"third-person", "plural"}),
]
AUXILIARY_PRESENT = {
    "avoir": ["ai", "as", "a", "avons", "avez", "ont"],
    "être": ["suis", "es", "est", "sommes", "êtes", "sont"],
}
# Verbs starting with an aspirated h don't elide: "je hais", not "j'hais".
H_ASPIRE = {"haïr", "hacher", "harceler", "hanter", "hâter", "hausser", "héler", "heurter", "hisser", "huer", "hurler"}
VOWELS = "aeiouyàâäéèêëîïôöùûüœæ"


def _subject(pronoun: str, form: str, infinitive: str, subjunctive: bool = False) -> str:
    """"je" + "aime" -> "j'"; subjunctive rows get "que": "que je", "qu'il/elle"."""
    elides = form[:1] in VOWELS or (form[:1] == "h" and infinitive not in H_ASPIRE)
    subject = "j'" if pronoun == "je" and elides else pronoun
    if subjunctive:
        subject = f"qu'{subject}" if subject[0] in "ie" else f"que {subject}"
    return subject


async def fetch_conjugation(verb: str) -> dict | None:
    """Main conjugation tables for a verb, or None if the dictionary has no conjugation for it."""
    try:
        async with _client() as client:
            entries = await _free_dictionary_entries(client, verb)
    except UPSTREAM_ERRORS as exc:
        raise ExternalAPIError(f"Dictionary lookup failed: {exc!r}") from exc
    # A conjugated verb entry has dozens of forms; others ("aller" the noun) have a plural at most.
    entry = next((e for e in _verb_entries(entries) if len(e.get("forms", [])) > 20), None)
    if entry is None:
        return None

    raw = entry["forms"]
    # Plain single words only: drops IPA ("mɑ̃ʒ"), pronominal variants ("me suis", "t'es", "sois-toi") and templates.
    forms = [
        f
        for f in raw
        if not set(f.get("tags", [])) & TABLE_TAGS
        and FRENCH_WORD_RE.match(normalize_word(f["word"]))
        and not set("-'") & set(normalize_word(f["word"]))
    ]

    def find(required: set[str]) -> str:
        found = []
        for f in forms:
            if required <= set(f.get("tags", [])) and f["word"] not in found:
                found.append(f["word"])
        return " / ".join(found)  # some cells have variants: "paie / paye"

    infinitive = normalize_word(verb)
    auxiliary = "être" if any(f["word"].startswith("être +") for f in raw) else "avoir"
    past_participle = find({"participle", "past"}).split(" / ")[0]
    tenses = []
    for name, tags in TENSES:
        rows = []
        for pronoun, person in PERSONS:
            form = find(tags | person)
            rows.append({"subject": _subject(pronoun, form, infinitive, subjunctive="subjunctive" in tags), "form": form})
        tenses.append({"name": name, "rows": rows})

    if past_participle:
        # Passé composé = auxiliary + past participle, which agrees with the subject after être.
        endings = ["(e)", "(e)", "(e)", "(e)s", "(e)s", "(e)s"] if auxiliary == "être" else [""] * 6
        rows = [
            {"subject": _subject(pronoun, aux, infinitive), "form": f"{aux} {past_participle}{ending}"}
            for (pronoun, _), aux, ending in zip(PERSONS, AUXILIARY_PRESENT[auxiliary], endings)
        ]
        tenses.insert(1, {"name": "Passé composé", "rows": rows})

    imperative = [
        {"subject": "", "form": find({"imperative"} | person)}  # no subject: "va", "allons", "allez"
        for pronoun, person in PERSONS
        if pronoun in ("tu", "nous", "vous")
    ]
    if any(row["form"] for row in imperative):
        tenses.append({"name": "Impératif", "rows": imperative})

    return {
        "infinitive": infinitive,
        "auxiliary": auxiliary,
        "present_participle": find({"participle", "present"}).split(" / ")[0] or None,
        "past_participle": past_participle or None,
        "tenses": [t for t in tenses if any(row["form"] for row in t["rows"])],
    }


# ---- Tatoeba ----


async def fetch_examples(client: httpx.AsyncClient, word: str) -> list[dict]:
    params = {
        # Quotes make multi-word lookups ("il y a") an exact-phrase search instead of "all of these words".
        "q": f'"{word}"' if " " in word else word,
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


# ---- Combined lookup ----


async def lookup_word(word: str) -> dict:
    """Combine definitions and example sentences for a French word.

    If one upstream fails the other's results are still returned; raises
    ExternalAPIError only when both fail.
    """
    word = unicodedata.normalize("NFC", word).strip()
    async with _client() as client:
        entry, examples = await asyncio.gather(
            fetch_definitions(client, word), fetch_examples(client, word), return_exceptions=True
        )

    for result in (entry, examples):
        if isinstance(result, BaseException) and not isinstance(result, ExternalAPIError):
            raise result
    if isinstance(entry, ExternalAPIError) and isinstance(examples, ExternalAPIError):
        raise ExternalAPIError(f"{entry} / {examples}")
    if isinstance(entry, ExternalAPIError):
        logger.warning("%s", entry)
        entry = _empty_entry()
    if isinstance(examples, ExternalAPIError):
        logger.warning("%s", examples)
        examples = []

    return {
        "word": word,
        "part_of_speech": entry["part_of_speech"],
        "definition": entry["definition"],
        "gender": entry["gender"],
        "base_forms": entry["base_forms"],
        "meanings": entry["meanings"],
        "examples": examples,
    }
