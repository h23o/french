# French

A self-hosted French vocabulary app: read French text, tap words to look them up, save them as flashcards, and review them with spaced repetition (SM-2). It's a FastAPI + SQLite backend with plain HTML pages, and it can be installed on a phone as a PWA that keeps working offline.

## Features

**Reader** (`/`)
- Paste French text and every word is highlighted by status: in your cards, due for review, or part of a saved phrase.
- Tap a word for definitions, example sentences with English translations, and pronunciation (normal or slow).
- Inflected forms resolve to their base form ("belle" → "beau", "vais" → "aller"). Nouns show their gender, and verbs show conjugation tables.
- Save single words or multi-word phrases as cards. Use **Select phrase** and tap the first and last word, or Shift+click on a computer. Phrases are matched in any inflected form ("j'ai besoin de" matches the card "avoir besoin de").
- Save texts to the library and come back to them later.

**Review** (`/review`)
- Four modes: FR → EN, EN → FR, **Fill in** (cloze on the card's example sentence) and **Listen** (type what you hear).
- Optional typed answers, which suggest a rating based on how close you got.
- Cards you miss come back at the end of the session for practice. Practice ratings aren't saved.
- Keyboard: <kbd>Space</kbd> reveal, <kbd>0</kbd>–<kbd>5</kbd> rate, <kbd>P</kbd> play, <kbd>S</kbd> play slowly.

**My Cards** (`/my-cards`)
- Browse, sort, edit and delete cards. Changing a card's word re-fetches its forms and gender.

**Library** (`/library`)
- Saved texts with a count of how many words in each are new to you.
- A bank of the 5,000 most frequent French words, filterable by part of speech, with your coverage of the top 100/500/1,000/2,000/5,000.
- About 13,000 words graded by CEFR level, A1 to C2, with how many of each level are in your cards. Each level can be searched and filtered by part of speech.
- Everyday phrases grouped by category, which you can add as cards in one tap.
- Adding a word from either word list opens a form pre-filled from the dictionary.

**Offline**
- A service worker precaches the pages, keeps the last response from each API read, and stores audio you've played. A review session can run with the server out of reach. Ratings are queued and sent once it's reachable again.

## Setup

Requires Python 3.10+.

```sh
python -m venv venv
source venv/bin/activate         # fish: source venv/bin/activate.fish
pip install -r requirements.txt
uvicorn main:app --reload
```

Then open http://localhost:8000.

On first start the app creates `french_app.db` (SQLite) in the working directory. New columns are added to an existing database automatically, so there are no migrations to run.

### Using it from a phone

Run uvicorn on all interfaces and open the laptop's address from the phone:

```sh
uvicorn main:app --host 0.0.0.0 --port 8000
```

Browsers only run service workers over HTTPS or on `localhost`, so offline mode and "Add to Home Screen" as a standalone app need the server behind HTTPS (for example `tailscale serve` or a reverse proxy with a certificate). Over plain HTTP on the LAN everything else still works.

## How it works

| Piece | Where |
| --- | --- |
| API and page routes | [main.py](main.py) |
| Models: `Card` (word, translation, example, gender, forms, SM-2 state) and `SavedText` | [models.py](models.py) |
| SM-2 scheduling | [srs.py](srs.py) |
| Tokenizing text and matching it against cards (elision, inflected forms, phrases) | [app/services/text_processor.py](app/services/text_processor.py) |
| Dictionary lookups, examples, conjugations | [app/services/external_api.py](app/services/external_api.py) |
| Filling in forms and gender for saved cards | [app/services/enrichment.py](app/services/enrichment.py) |
| Text-to-speech with an on-disk MP3 cache | [app/services/tts_cache.py](app/services/tts_cache.py) |
| Word bank, CEFR levels and phrase list | [app/services/word_bank.py](app/services/word_bank.py) |
| Frontend pages and service worker | [app/static/](app/static/) |

When a card is saved, its inflected forms and gender are fetched from the dictionary so the reader can match "maisons" as soon as "maison" is saved. If the dictionary is unreachable the card is still saved, and the missing data is filled in on the next startup.

The interactive API docs are at http://localhost:8000/docs.

### External services

The app needs internet access for lookups and new audio. Nothing needs an API key.

- [Free Dictionary API](https://freedictionaryapi.com) (Wiktionary data): definitions, base forms, gender, inflections, conjugations. Falls back to the Wiktionary REST API for definitions.
- [Tatoeba](https://tatoeba.org): example sentences with human translations.
- [edge-tts](https://github.com/rany2/edge-tts): speech using the `fr-FR-HenriNeural` voice. Audio is cached under `app/static/audio/`, so each phrase is only synthesized once.

## Word list data

- `app/data/word_bank.json` lists the 5,000 most frequent French lemmas from [Lexique 3.83](http://www.lexique.org) (CC BY-SA 4.0), ranked by the average of their film-subtitle and book frequencies.
- `app/data/levels.json` grades about 13,000 words A1–C2 using [FLELex](https://cental.uclouvain.be/cefrlex/flelex/) (CC BY-NC-SA 4.0, so non-commercial use only). FLELex counts how often each word appears in French-as-a-foreign-language textbooks at each level. Within a level, words are ordered by that frequency. Words that aren't in Lexique are dropped, which removes FLELex's proper names and lemmatizer errors. Lexique also supplies noun genders.

Both are generated by one script:

```sh
python scripts/build_word_bank.py                                      # downloads Lexique (~25 MB) and FLELex (~1 MB)
python scripts/build_word_bank.py Lexique383.tsv FleLex_TT_Beacco.tsv  # or use local copies
```

`app/data/phrases.json` is a hand-written list of everyday phrases.
