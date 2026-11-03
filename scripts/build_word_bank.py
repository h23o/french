"""Build the word lists in app/data/.

word_bank.json: the most frequent French base forms (lemmas), from Lexique 3.83
(New, Pallier et al., http://www.lexique.org), CC BY-SA 4.0. Frequency is the
average of its film-subtitle and book lemma frequencies.

levels.json: words graded A1–C2, from FLELex (François et al., UCLouvain,
https://cental.uclouvain.be/cefrlex/flelex/), CC BY-NC-SA 4.0. FLELex counts how
often each word appears in French-as-a-foreign-language textbooks at each CEFR
level; its "level" column (the Beacco variant) is the level a word is taught at.
Within a level, words are ordered by that textbook frequency.

    python scripts/build_word_bank.py                                      # downloads both (~25 MB + 1 MB)
    python scripts/build_word_bank.py Lexique383.tsv                       # or use local copies
    python scripts/build_word_bank.py Lexique383.tsv FleLex_TT_Beacco.tsv
"""

import csv
import json
import sys
import tempfile
import urllib.request
from collections import defaultdict
from pathlib import Path

LEXIQUE_URL = "http://www.lexique.org/databases/Lexique383/Lexique383.tsv"
FLELEX_URL = "https://cental.uclouvain.be/cefrlex/static/resources/fr/FleLex_TT_Beacco.tsv"
DATA_DIR = Path(__file__).resolve().parent.parent / "app" / "data"
LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")
SIZE = 5000
# Ignore rare uses ("je" tagged as a noun) when listing a word's parts of speech.
MIN_POS_SHARE = 0.10
# Lexique counts "un peu" / "à nouveau" as noun uses; as vocabulary these are mainly the adverb/adjective.
POS_OVERRIDES = {"peu": ["adverb"], "nouveau": ["adjective", "noun"]}


def with_ligature(word: str) -> str:
    """Lexique spells œ as "oe" (coeur, oeil); restore the standard spelling so cards match."""
    return word.replace("oeu", "œu").replace("oei", "œi")

POS_NAMES = {
    "NOM": "noun",
    "VER": "verb",
    "AUX": "verb",  # avoir/être as auxiliaries
    "ADJ": "adjective",
    "ADV": "adverb",
    "PRE": "preposition",
    "CON": "conjunction",
    "PRO": "pronoun",
    "ART": "article",
    "ONO": "interjection",
}

# FLELex is tagged with TreeTagger.
FLELEX_POS_NAMES = {
    "NOM": "noun",
    "VER": "verb",
    "ADJ": "adjective",
    "ADV": "adverb",
    "PRP": "preposition",
    "PRP:det": "preposition",  # au, du
    "KON": "conjunction",
    "PRO": "pronoun",
    "DET:ART": "article",
    "DET:POS": "determiner",
    "INT": "interjection",
}


class Lexique:
    def __init__(self, tsv_path: Path):
        self.freq: dict[str, float] = defaultdict(float)
        self.pos_freq: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.genders: dict[str, set[str]] = defaultdict(set)
        # Every spelling used as a noun, with its gender: "copine" f, though its lemma is "copain".
        self.noun_spellings: dict[str, set[str]] = defaultdict(set)
        seen: set[tuple[str, str]] = set()

        with tsv_path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                lemma, cgram = row["lemme"].strip(), row["cgram"].strip()
                pos = POS_NAMES.get(cgram.split(":")[0])
                if not lemma or not pos or any(ch.isdigit() for ch in lemma):
                    continue
                if lemma.endswith("'"):
                    continue  # elided duplicates: l' / d' / s' are counted under le / de / se
                if pos == "noun" and len(lemma) == 1:
                    continue  # letter names ("a", "b"...)
                if pos == "noun":
                    self.noun_spellings[row["ortho"]].update(g for g in (row["genre"],) if g in ("m", "f"))
                # Gender comes from the lemma itself or its plural (often the only row that has it:
                # "maisons" f). Other rows can be the feminine counterpart ("amie" for "ami").
                plurals = (lemma + "s", lemma + "x", lemma[:-2] + "aux")
                if pos == "noun" and row["genre"] in ("m", "f") and (row["ortho"] == lemma or row["ortho"] in plurals):
                    self.genders[lemma].add(row["genre"])
                # Lemma frequency repeats on every inflected row; count it once per (lemma, category).
                if (lemma, cgram) in seen:
                    continue
                seen.add((lemma, cgram))
                lemma_freq = (float(row["freqlemfilms2"] or 0) + float(row["freqlemlivres"] or 0)) / 2
                self.freq[lemma] += lemma_freq
                self.pos_freq[lemma][pos] += lemma_freq

    def gender(self, word: str) -> str | None:
        gender = self.genders.get(word) or self.noun_spellings.get(word) or set()
        return "mf" if len(gender) == 2 else next(iter(gender), None)


def main_parts(pos_freq: dict[str, float]) -> list[str]:
    """Parts of speech, most common first, leaving out rare uses."""
    total = sum(pos_freq.values())
    parts = sorted(pos_freq, key=lambda p: -pos_freq[p])
    return [p for p in parts if pos_freq[p] >= MIN_POS_SHARE * total] or parts[:1]


def build_bank(lexique: Lexique) -> list[dict]:
    freq = lexique.freq
    ranked = sorted(freq, key=lambda lemma: -freq[lemma])[:SIZE]
    bank = []
    for rank, lemma in enumerate(ranked, start=1):
        parts = POS_OVERRIDES.get(lemma, main_parts(lexique.pos_freq[lemma]))
        gender = lexique.genders.get(lemma, set())
        bank.append(
            {
                "rank": rank,
                "word": with_ligature(lemma),
                "pos": parts,  # most common use first
                # Only for words mainly used as nouns ("été" is mostly the verb, not "summer").
                "gender": ("mf" if len(gender) == 2 else next(iter(gender))) if gender and parts[0] == "noun" else None,
                "per_million": round(freq[lemma], 1),
            }
        )
    return bank


def build_levels(flelex_path: Path, lexique: Lexique) -> list[dict]:
    # word -> part of speech -> (level, textbook frequency per million)
    entries: dict[str, dict[str, tuple[str, float]]] = defaultdict(dict)
    with flelex_path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            word, pos, level = row["word"].strip().lower(), FLELEX_POS_NAMES.get(row["tag"]), row["level"].strip()
            if not pos or level not in LEVELS:
                continue
            # FLELex is lemmatized automatically, so it has proper names ("zoé") and stray
            # forms ("chefs-d'oeuvre"). Keep words Lexique knows as a lemma, or as a noun
            # ("copine", which Lexique files under "copain").
            if word not in lexique.freq and not (pos == "noun" and word in lexique.noun_spellings):
                continue
            old_level, old_freq = entries[word].get(pos, (level, 0.0))
            entries[word][pos] = (min(old_level, level, key=LEVELS.index), old_freq + float(row["freq_total"]))

    graded = []
    for word, by_pos in entries.items():
        parts = POS_OVERRIDES.get(word, main_parts({pos: freq for pos, (_, freq) in by_pos.items()}))
        parts = [p for p in parts if p in by_pos] or parts
        # The level of its main uses: "être" is A1 as a verb, not B1 as the noun.
        level = min((by_pos[p][0] for p in parts if p in by_pos), key=LEVELS.index)
        graded.append(
            {
                "word": with_ligature(word),
                "pos": parts,
                "gender": lexique.gender(word) if parts[0] == "noun" else None,
                "level": level,
                "freq": sum(freq for _, freq in by_pos.values()),
            }
        )

    graded.sort(key=lambda w: (LEVELS.index(w["level"]), -w["freq"], w["word"]))
    rank = {level: 0 for level in LEVELS}
    for entry in graded:
        rank[entry["level"]] += 1
        entry["rank"] = rank[entry["level"]]  # within its level
        del entry["freq"]
    return graded


def download(url: str, folder: Path) -> Path:
    path = folder / url.rsplit("/", 1)[-1]
    print(f"Downloading {url} …")
    urllib.request.urlretrieve(url, path)
    return path


def write(name: str, data: list[dict]) -> None:
    path = DATA_DIR / name
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {len(data)} words to {path}")


def main() -> None:
    args = [Path(arg) for arg in sys.argv[1:3]]
    with tempfile.TemporaryDirectory() as tmp:
        lexique_path = args[0] if len(args) > 0 else download(LEXIQUE_URL, Path(tmp))
        flelex_path = args[1] if len(args) > 1 else download(FLELEX_URL, Path(tmp))
        lexique = Lexique(lexique_path)
        bank, levels = build_bank(lexique), build_levels(flelex_path, lexique)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write("word_bank.json", bank)
    write("levels.json", levels)


if __name__ == "__main__":
    main()
