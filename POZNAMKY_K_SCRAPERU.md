# Poznámky na stretnutie s učiteľom: ako funguje scraper

## 1. Krátke vysvetlenie vlastnými slovami

„Program má dve časti. Prvá sa pripojí k otvorenému Chromu a z aktuálnej stránky
Aukra získa zoznam ponúk s ich ID, názvom, cenou a odkazom. Druhá načíta detail
každej ponuky a doplní začiatok, koniec, cenu, typ predaja a popis. Z popisu
odstráni emoji a rušivé formátovacie znaky. Výsledok ukladám do JSON. Pri overovaní
kontrolujem ID, vyplnenosť údajov, rôzne typy predaja a správanie filtra. Pred
prezentáciou ešte porovnám vybrané záznamy priamo s ich stránkami na Aukre.“

## 2. Ako funguje prvá časť: aukro_scraper.py

1. `attach_to_chrome()` cez Selenium pripojí program k Chromu s ladením na porte
   9222. Tento port umožňuje programu komunikovať s prehliadačom.
2. Počká na načítanie zoznamu ponúk. Číta DOM, teda prvky už načítanej webovej
   stránky, ako sú karty ponúk, názvy, ceny a odkazy.
3. Vyberá karty hlavného zoznamu a vynecháva reklamné bannery a odporúčané ponuky
   v samostatných posuvných paneloch.
4. `extract_listings()` postupne posúva stránku, pretože ďalšie karty sa môžu
   načítať až pri scrollovaní. Počet posunov má ochranný limit.
5. `parse_card()` spracuje každú kartu: získa ID, skontroluje odkaz na Aukro,
   upraví medzery a prevedie text ceny na číslo. Napríklad `1 234,50 Kč` sa zmení
   na `1234.5`.
6. Ponuky ukladá podľa ID. Opakovane nájdená ponuka sa v zozname nezduplikuje.
7. Výsledok uloží do `aukro_mince.json` a automaticky spustí druhý skript.

**Prečo je ID kľúčové:** názov sa môže opakovať, ale ID určuje konkrétnu ponuku.
Používa sa na prepojenie zoznamu s detailom a na rozlíšenie ponúk tej istej mince.
ID ponuky nie je ID druhu mince.

## 3. Ako funguje druhá časť: aukro_details.py

1. Načíta uložený zoznam a pre každú ponuku prevezme jej ID a odkaz.
2. `fetch_detail()` cez webovú požiadavku stiahne HTML verejnej stránky detailu.
   Táto časť už nepotrebuje otvorený Chrome.
3. `NgStateParser` nájde JSON vložený do stránky v prvku `ng-state`.
   `parse_detail_page()` v jeho časti `aukCache` vyberie údaje konkrétnej ponuky.
4. Skontroluje zhodu ID, typ predaja a číselnú cenu v Kč. Pri prihadzovaní číta
   `price`, pri pevnej cene `buyNowPrice`.
5. Získa časy `startingTime` a `endingTime` a text `descriptionStripped`, ktorý
   už neobsahuje HTML značky popisu.
6. `clean_description()` odstráni pokryté emoji, neviditeľné formátovacie znaky
   a nadbytočné medzery. Zachováva českú a slovenskú diakritiku, čísla a bežnú
   interpunkciu. Príklad: `Minca 5 Kč ❤️🍎🪙 UNC` → `Minca 5 Kč UNC`.
7. Výsledok uloží do `aukro_mince_detail.json`.

## 4. Čo znamenajú výsledné polia

| Pole | Význam |
|---|---|
| `id` | ID konkrétnej ponuky na Aukre. |
| `title` | Názov zo zoznamu ponúk. |
| `url` | Odkaz na detail, podľa ktorého môžem výsledok overiť. |
| `price_czk` | Cena z detailu v čase zberu. Pri prihadzovaní aktuálna cena, pri pevnom predaji cena Kup teď. |
| `sale_type` | `BIDDING` = prihadzovanie; `BUYNOW` = pevná cena. |
| `buy_now_price_czk` | Dostupná cena Kup teď, prípadne `null`, ak nie je dostupná. |
| `bidders_count` | Počet prihadzujúcich ľudí; nie počet všetkých príhozov. |
| `start_at`, `end_at` | Začiatok a koniec podľa zdrojovej stránky, vrátane časového posunu, napríklad `+02:00`. |
| `description_raw` | Pôvodný text popisu, ktorý môže obsahovať emoji. |
| `description_clean` | Vyčistený text určený na ďalšie vyhľadávanie a spracovanie. |

Pôvodný popis uchovávam, aby som mohol skontrolovať, čo filter odstránil, a neskôr
čistenie zopakovať. Filter pracuje po stiahnutí textu; upravuje jeho čistú verziu.

## 5. Prečo sú dva skripty a jedno spustenie

Každý skript má jasnú úlohu. Keď upravujem filter alebo spracovanie detailov,
môžem pracovať s už uloženým zoznamom. Jednotlivé časti sa ľahšie kontrolujú
a opravujú. Rozdelenie samo osebe sťahovanie nezrýchľuje.

`run_detail_scraper()` spustí druhý skript pomocou rovnakého Pythonu ako prvý.
Ak druhá časť oznámi chybu, aj hlavný príkaz skončí s chybovým stavom.

## 6. Ako overujem správnosť

### Čo už bolo overené

- Pri skoršom behu sa doplnili detaily 60 ponúk bez chyby. Mali jedinečné ID
  a vyplnené dátumy, cenu a popis.
- Pri aktuálnej kontrole je v základnom zozname 60 ponúk a v súbore s detailmi
  10 záznamov. Všetkých 10 má jedinečné ID a vyplnené požadované polia.
- Na živých príkladoch boli vyskúšané prihadzovanie aj pevná cena.
- Filter bol overený na emoji a zachovaní diakritiky.
- Prepojenie skriptov bolo testované s pripraveným zoznamom a živým detailom:
  fungoval limit aj vlastné názvy výstupných súborov s medzerami.
- Pri chybnom ID sa chyba preniesla do hlavného príkazu a hotový výstup s
  detailmi sa neprepísal.

**Kontrola vyplnenosti nie je úplná kontrola správnosti.** Existujúca cena alebo
dátum môžu byť vyplnené, ale ešte ich treba porovnať so správnou ponukou.

### Čo ručne urobiť pred stretnutím

1. Vybrať 3–5 záznamov a otvoriť ich odkazy `url`.
2. Porovnať ID, cenu, typ predaja, dátumy a obsah popisu so stránkou.
3. Skontrolovať aspoň jednu ponuku s prihadzovaním a jednu s pevnou cenou.
4. Pri popise s emoji porovnať pôvodný a čistý text: odstránili sa dekorácie,
   ale zostala hodnota mince, ročník a ďalšie dôležité údaje?
5. Pri cenách porovnávať krátko po zbere, pretože živá aukcia sa môže zmeniť.

Tieto ručné kontroly sú plán na ukážku učiteľovi, nie tvrdenie, že už boli
systematicky vykonané na všetkých ponukách.

## 7. Čo vedieť povedať o obmedzeniach

- Spracúva sa aktuálna stránka výsledkov. Automatické prechádzanie ďalších
  stránok zatiaľ nie je implementované.
- Číslo 60 bolo počtom ponúk na stránke, nie pevným limitom kódu.
  `--limit 10` obmedzí iba počet načítaných detailov na prvých desať.
- Ukladá sa aktuálny stav cien. História vývoja cien zatiaľ nevzniká.
- Dáta sú zatiaľ v JSON; databáza, pravidlá rozpoznávania mincí a AI sú ďalšie
  možné kroky.
- Kód kontroluje viaceré vstupy, ale zatiaľ automaticky nekontroluje napríklad
  platný formát každého dátumu a to, či začiatok predchádza koncu.
- Ak niektorý detail zlyhá, súbor s detailmi sa neprepíše. Súbor so základným
  zoznamom už v tej chvíli môže byť aktualizovaný.
- Zmena štruktúry webu môže vyžadovať úpravu výberu kariet alebo čítania údajov.

## 8. Príkazy, ktoré potrebujem poznať

Spúšťajú sa v PowerShelli z priečinka projektu. Pre zber zoznamu musí byť
otvorený Chrome s ladením na porte 9222 a požadovaná stránka Aukra.

```powershell
# Celý proces pre všetky ponuky na aktuálnej stránke
.\.venv\Scripts\python.exe aukro_scraper.py

# Zoznam a detaily prvých 10 ponúk na skúšanie
.\.venv\Scripts\python.exe aukro_scraper.py --limit 10

# Iba aktualizácia detailov z už uloženého zoznamu; Chrome nie je potrebný
.\.venv\Scripts\python.exe aukro_details.py
```
