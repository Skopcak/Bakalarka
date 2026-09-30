# Bachelor's Thesis / Bakalárska práca

## English

This repository contains source code and supporting data for my bachelor's thesis.
The current implementation collects numismatic offers from Aukro and retrieves
their prices, sale types, start and end times, and descriptions. The project is
a work in progress.

Run the commands below in PowerShell from this repository's folder. The listing
step requires Chrome started with remote debugging on port `9222`, with an Aukro
category or search results page open. It collects the current results page.

Collect the list and enrich the first 10 offers:

```powershell
.\.venv\Scripts\python.exe aukro_scraper.py --limit 10
```

Remove `--limit 10` to retrieve details for every collected offer. The list is
saved to `aukro_mince.json`, and the enriched output to `aukro_mince_detail.json`.
The main script automatically calls `aukro_details.py` using the same Python
interpreter. If any detail fails, the previous detail output is retained and the
command reports a failure.

To refresh details from an existing list, without Chrome:

```powershell
.\.venv\Scripts\python.exe aukro_details.py
```

Use `aukro_scraper.py --list-only` to collect only the list.

## Slovensky

Tento repozitár obsahuje zdrojový kód a podklady k mojej bakalárskej práci.
Aktuálna verzia zbiera numizmatické ponuky z Aukra a získava ich ceny, typy predaja,
začiatok a koniec a pôvodný aj vyčistený popis. Projekt bude priebežne aktualizovaný.

Príkazy spúšťaj v PowerShelli z priečinka tohto repozitára. Na získanie zoznamu
musí byť otvorený Chrome s ladením na porte `9222` a stránka kategórie alebo
výsledkov vyhľadávania na Aukre. Spracúva sa aktuálna stránka výsledkov.

Získanie zoznamu a doplnenie detailov prvých 10 ponúk jedným príkazom:

```powershell
.\.venv\Scripts\python.exe aukro_scraper.py --limit 10
```

Pre detaily všetkých získaných ponúk vynechaj `--limit 10`. Zoznam sa uloží do
`aukro_mince.json` a výsledok s detailmi do `aukro_mince_detail.json`. Hlavný skript
automaticky spustí `aukro_details.py`. Ak načítanie niektorého detailu zlyhá,
predchádzajúci súbor s detailmi sa neprepíše a príkaz oznámi chybu.

Aktualizácia detailov z už uloženého zoznamu funguje aj bez Chromu:

```powershell
.\.venv\Scripts\python.exe aukro_details.py
```

Na získanie iba zoznamu použi `aukro_scraper.py --list-only`.
