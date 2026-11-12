# AI Reader

Codzienny przegląd artykułów o AI i ich wdrażaniu: zbiera wpisy z kanałów RSS, punktuje je słowami kluczowymi, odrzuca duplikaty i wysyła najlepsze na kanał Discorda. Działa bez AI, w całości deterministycznie.

## Jak to działa

1. **Pobieranie:** każdy feed z `config.yaml` jest pobierany osobno. Zepsuty feed trafia do logu jako ostrzeżenie i nie zatrzymuje reszty.
2. **Okno czasu:** brane są tylko wpisy z ostatnich `lookback_hours` godzin.
3. **Punktacja:**
   - Wynik to waga źródła plus suma wag słów kluczowych.
   - Trafienie w tytule liczy się ×2, w opisie ×1.
   - Słowa negatywne (finansowanie, plotki) obniżają wynik.
   - Feed z `require_keyword: true` przepuszcza tylko wpisy, które trafiły w co najmniej jedno pozytywne słowo.
4. **Selekcja:** zostają artykuły z wynikiem `>= threshold`, posortowane malejąco, maksymalnie `max_items`.
5. **Deduplikacja:**
   - Po znormalizowanym URL (bez `utm_*`, `www`, końcowego `/`).
   - Po podobieństwie tytułu (`title_similarity`), żeby ten sam news z dwóch źródeł nie przyszedł dwa razy.
   - Stan trzymany jest w `state/seen.json` i commitowany przez workflow; wpisy starsze niż `seen_retention_days` są czyszczone.
6. **Discord:** wiadomość przez webhook. Każdy artykuł to embed z tytułem, linkiem, opisem, źródłem, wynikiem i trafionymi słowami.

## Uruchomienie

1. W repo: **Settings → Secrets and variables → Actions → New repository secret** o nazwie `DISCORD_WEBHOOK_URL`, z adresem webhooka kanału.
2. **Actions → AI Reader digest → Run workflow:**
   - zaznacz „Tylko podgląd”, żeby zobaczyć w logu, co by poszło;
   - potem uruchom bez zaznaczenia, żeby wysłać naprawdę.
3. Dalej workflow działa sam codziennie o 6:47 UTC (7:47 zimą, 8:47 latem). Godzinę zmienisz w `.github/workflows/digest.yml`.

Lokalnie:

```bash
pip install -r requirements.txt
python -m ai_reader.main --dry-run          # podgląd bez wysyłki i bez zapisu stanu
DISCORD_WEBHOOK_URL=... python -m ai_reader.main
python -m pytest -q
```

## Strojenie

Wszystko jest w `config.yaml`:

- **Za dużo szumu:** podnieś `threshold`, obniż wagę źródła albo dodaj mu `require_keyword: true`.
- **Za mało:** obniż `threshold` albo dodaj feedy.
- **Słowa kluczowe:** to regexy bez rozróżniania wielkości liter, każde liczone raz na artykuł.

Po pierwszym przebiegu w Actions sprawdź log: ostrzeżenia `feed pominięty` wskazują adresy RSS do poprawienia.

## Pomysły na później

- Feedback 👍/👎 pod wiadomościami, które korygują wagi źródeł. Wymaga bota Discord, bo webhook nie czyta reakcji.
- Opcjonalna ocena LLM dla top ~20 kandydatów po filtrze słów kluczowych.
