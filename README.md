# Monitor QGIS Plugin

Gebruik het bijgeleverde bouwscript om een ZIP te genereren die QGIS direct kan installeren.

```bash
./scripts/build_package.py
```

Wil je in één keer alles klaarzetten om naar `main` te uploaden, gebruik dan de publiceer-helper:

```bash
python scripts/publish.py
```

Het script bouwt standaard het pakket opnieuw op en voert een snelle `compileall`-controle uit. Aan het einde verschijnt een
checklist met de exacte `git`-commando's om je commit naar `main` te sturen.

Het script schrijft de distributie naar `dist/qgis_monitor_pro_3_3_13_logsafe.zip`.  Vanuit QGIS installeer je de plugin als volgt:

1. Open QGIS en ga naar **Plugins → Manage and Install Plugins…**
2. Klik op **Install from ZIP**.
3. Blader naar het ZIP-bestand in de `dist/` map en bevestig de installatie.

Als je even wilt checken of je in de projectroot staat, gebruik dan `pwd` nadat je naar de repository bent genavigeerd:

```bash
cd /path/to/Monitor
pwd
```

De uitvoer geeft het absolute pad van de root waar ook dit README-bestand en de `scripts/` map staan.

## Het distributie-zip opnieuw bouwen

Wanneer je wijzigingen aan de plugin hebt doorgevoerd, genereer je met het script hierboven een bijgewerkte distributie. Omdat binaire artefacten niet door GitHub Pull Requests geaccepteerd worden, commit je het ZIP-bestand niet mee; deel in plaats daarvan de output uit `dist/` of maak het pakket opnieuw op de doelomgeving. Het script werkt ook buiten Git (bijvoorbeeld wanneer je alleen een uitgepakte ZIP hebt) en pakt dan alle relevante bronbestanden automatisch mee.

## Uploaden naar main

1. Draai `python scripts/publish.py` om het distributie-zip en de sanity-checks uit te voeren.
2. Controleer de inhoud van `dist/` in QGIS indien gewenst en bekijk de status met `git status`.
3. Commit en push volgens de checklist die het script toont, bijvoorbeeld `git commit -m "Update logging UI"` gevolgd door `git push origin main`.

## Nieuwe functies in de UI

De plugin biedt nu extra tooling direct vanuit het QGIS-menu:

- **Statusoverzicht** toont een samenvatting van de actieve sessie, het logpad en de heartbeat-status.
- **Recente gebeurtenissen** laat de laatste breadcrumbs zien die de engine verzamelt.
- **Opschonen logmap** voert onmiddellijk het prune-script uit zodat oude logbestanden verdwijnen.
- **Instellingen importeren/exporteren** maakt het mogelijk om configuraties te bewaren of te delen via JSON-bestanden.
- **Laatste log openen** navigeert automatisch naar het meest recente logbestand (full of errors) in de logmap.

Elke actie schrijft een logregel in de categorie `QGISMonitorPro.UI`, zodat je in de QGIS-logberichten kunt volgen wat er via de UI gebeurt.
