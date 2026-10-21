# Belpex-prijzen

Bewaart elke dag automatisch de Belgische elektriciteitsprijzen als CSV-bestanden:

| Bestand | Inhoud | Bron |
|---|---|---|
| `data/dayahead_<jaar>.csv` | Day-ahead prijzen (Belpex/EPEX) in hun eigen resolutie: per uur tot oktober 2025, daarna per kwartier | ENTSO-E |
| `data/dayahead_hourly_<jaar>.csv` | Day-ahead uurprijzen. Sinds de kwartiermarkt is dit het gemiddelde van de vier kwartieren | berekend |
| `data/imbalance_<jaar>.csv` | Onbalansprijzen per kwartier, met systeemonbalans en kwaliteitsstatus | Elia Open Data |

Kolommen:

- `datetime_utc` is het begin van de periode in UTC. Gebruik deze kolom als unieke sleutel.
- `datetime_brussels` is hetzelfde moment in Belgische tijd, met de UTC-afwijking erbij (`+01:00` of `+02:00`). Zo blijven de dubbele uren bij de overgang naar wintertijd uit elkaar.
- Prijzen staan in EUR/MWh, met een punt als decimaalteken.

Het script draait elke dag om 13:15 UTC, nadat de day-ahead prijzen voor morgen gepubliceerd zijn. Het werkt telkens de laatste dagen opnieuw bij, zodat een gemiste dag of een latere correctie van Elia (de status gaat van `NotValidated` naar `Validated`) vanzelf goed komt.

---

## Eenmalige installatie (alles via de website van GitHub)

### 1. Repository aanmaken
1. Meld je aan op [github.com](https://github.com).
2. Klik rechtsboven op **+** en dan **New repository**.
3. Naam: `belpex-prijzen`. Kies **Public**: dan kan Power BI de bestanden lezen zonder wachtwoord. Het gaat om publieke marktprijzen, en je token komt er niet in.
4. Vink **Add a README file** aan en klik **Create repository**.

### 2. Bestanden toevoegen
1. Klik in je repository op **Add file** en dan **Upload files**.
2. Sleep `fetch_prices.py`, `requirements.txt`, `.gitignore` en deze `README.md` erin (overschrijf de README) en klik **Commit changes**.
3. De planning voeg je apart toe: klik **Add file** en dan **Create new file**.
   - Typ als naam exact: `.github/workflows/daily.yml` (de schuine strepen maken vanzelf mapjes).
   - Plak de inhoud van het bestand `daily.yml` en klik **Commit changes**.

### 3. Schrijfrechten geven
**Settings**, dan **Actions**, dan **General**. Kies onderaan bij *Workflow permissions* de optie **Read and write permissions** en klik **Save**.

### 4. Eerste test (zonder ENTSO-E-token)
1. Ga naar het tabblad **Actions** en klik links op **Prijzen ophalen**.
2. Klik **Run workflow**, laat alles op de standaardwaarden staan en klik op de groene knop.
3. Na ongeveer een minuut staat er een groen vinkje. De gele waarschuwing "Geen ENTSOE_TOKEN" is normaal zolang je nog geen token hebt.
4. In de map `data` staat nu `imbalance_2026.csv`.

### 5. Historiek van de onbalansprijzen ophalen
Klik **Run workflow** en kies **mode** `backfill` en **source** `imbalance`. Zonder startdatum haalt het script alles op vanaf 1 januari 2024. Wil je verder terug, vul dan bijvoorbeeld `2015-01-01` in: Elia heeft kwartierdata vanaf 2015.

### 6. Zodra je ENTSO-E-token binnen is
1. **Settings**, dan **Secrets and variables**, dan **Actions**, dan **New repository secret**.
2. Naam: `ENTSOE_TOKEN`. Bij *Secret* plak je je token. Klik **Add secret**.
3. Ga naar **Actions** en doe **Run workflow** met **mode** `backfill` en **source** `dayahead`. Dit haalt de day-ahead prijzen op vanaf januari 2015 en duurt ongeveer 5 tot 10 minuten.

Daarna loopt alles elke dag vanzelf.

---

## Gebruik in Power BI en Excel

Elk bestand heeft een vaste link:

```
https://raw.githubusercontent.com/kennethgev/belpex-prijzen/main/data/dayahead_hourly_2026.csv
```

- **Power BI:** **Get Data**, **Web**, plak de link en kies authenticatie **Anonymous**. Voor meerdere jaren voeg je per jaar een query toe en combineer je ze met **Append Queries**. Stel de landinstelling van de query in op *English (United States)*, zodat de punt als decimaalteken gelezen wordt.
- **Excel:** **Gegevens**, **Van web**, plak dezelfde link. Dubbelklik het CSV-bestand niet open, want de Belgische Excel leest de punten dan verkeerd.
- **Google Sheets:** `=IMPORTDATA("<link>")`. Dit werkt goed voor de kleinere bestanden; een volledig jaar kwartierdata is voor Sheets aan de grote kant.

---

## Goed om te weten

- **Elia wijzigt de onbalansprijs.** Vanaf 24/11/2026 (onder voorbehoud van goedkeuring door de CREG) start een nieuwe methodologie. Elia publiceert daarvoor nu al gesimuleerde datasets (ods211/ods212). Controleer na die datum of de dagelijkse run nog data binnenhaalt. Het script past zich dan niet vanzelf aan.
- **Overgang mei 2024:** tot 22/05/2024 komt de onbalansprijs uit Elia-dataset `ods047`, daarna uit `ods134`. De kolom `source_dataset` toont welke. Voor de oude dataset is `imbalance_price_eur_mwh` de *positieve* onbalansprijs. Door de single pricing in België is die bijna altijd gelijk aan de negatieve.
- **Inactiviteit:** GitHub zet geplande runs stil na 60 dagen zonder wijzigingen in de repository. Omdat het script elke dag nieuwe data toevoegt, gebeurt dat hier normaal niet. Krijg je toch een mail daarover, klik dan in **Actions** op **Enable workflow**.
- **Mislukte run:** GitHub stuurt je een e-mail. Meestal is dat een tijdelijke storing bij ENTSO-E of Elia, en de volgende dag wordt het gat automatisch opgevuld.

## Lokaal draaien (optioneel)

```
pip install -r requirements.txt
set ENTSOE_TOKEN=<je token>
python fetch_prices.py daily
python fetch_prices.py backfill --source imbalance --start 2024-01-01
```
