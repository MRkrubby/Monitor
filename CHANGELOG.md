# 3.3.3 — Noise reduction & session-stable logs
- **Qt-noise dempen**: bekende, irrelevante Qt waarschuwingen worden gefilterd.
- **Coalescing**: identieke meldingen binnen een venster (default 3s) worden samengevoegd; periodieke samenvatting in het log.
- **Eén logbestand per sessie** (instelbaar): herstart/stop binnen dezelfde QGIS-sessie blijft in hetzelfde bestand schrijven.
- Instellingen uitgebreid met toggles/venster voor bovenstaande opties.