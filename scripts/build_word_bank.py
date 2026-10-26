"""Build app/data/word_bank.json: the most frequent French base forms (lemmas).

Source: Lexique 3.83 (New, Pallier et al., http://www.lexique.org), CC BY-SA 4.0.
Frequency is the average of its film-subtitle and book lemma frequencies.

    python scripts/build_word_bank.py                 # downloads Lexique (~25 MB)
    python scripts/build_word_bank.py Lexique383.tsv  # or use a local copy
"""

import csv
import json
import sys
import tempfile
import urllib.request
from collections import defaultdict
from pathlib import Path

LEXIQUE_URL = "http://www.lexique.org/databases/Lexique383/Lexique383.tsv"
OUTPUT = Path(__file__).resolve().parent.parent / "app" / "data" / "word_bank.json"
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


def build(tsv_path: Path) -> list[dict]:
    freq: dict[str, float] = defaultdict(float)
    pos_freq: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    genders: dict[str, set[str]] = defaultdict(set)
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
            # Gender comes from the lemma itself or its plural (often the only row that has it:
            # "maisons" f). Other rows can be the feminine counterpart ("amie" for "ami").
            plurals = (lemma + "s", lemma + "x", lemma[:-2] + "aux")
            if pos == "noun" and row["genre"] in ("m", "f") and (row["ortho"] == lemma or row["ortho"] in plurals):
                genders[lemma].add(row["genre"])
            # Lemma frequency repeats on every inflected row; count it once per (lemma, category).
            if (lemma, cgram) in seen:
                continue
            seen.add((lemma, cgram))
            lemma_freq = (float(row["freqlemfilms2"] or 0) + float(row["freqlemlivres"] or 0)) / 2
            freq[lemma] += lemma_freq
            pos_freq[lemma][pos] += lemma_freq

    ranked = sorted(freq, key=lambda lemma: -freq[lemma])[:SIZE]
    bank = []
    for rank, lemma in enumerate(ranked, start=1):
        parts = sorted(pos_freq[lemma], key=lambda p: -pos_freq[lemma][p])
        parts = [p for p in parts if pos_freq[lemma][p] >= MIN_POS_SHARE * freq[lemma]] or parts[:1]
        parts = POS_OVERRIDES.get(lemma, parts)
        gender = genders.get(lemma, set())
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


def main() -> None:
    if len(sys.argv) > 1:
        bank = build(Path(sys.argv[1]))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Lexique383.tsv"
            print(f"Downloading {LEXIQUE_URL} …")
            urllib.request.urlretrieve(LEXIQUE_URL, path)
            bank = build(path)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(bank, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {len(bank)} words to {OUTPUT}")


if __name__ == "__main__":
    main()
