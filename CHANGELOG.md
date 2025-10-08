# 4.0.0 — Engine rewrite & Live viewer refresh
- Volledige herbouw van de engine rond `MonitorEngine` met centraal beheer van handlers, watchdog en heartbeat.
- Actieregistratie in de plugin zodat toolbar/menu-consistentie behouden blijft en logica testbaar is.
- Live Log Viewer gebruikt nu filesystem-notificaties, verbeterde filters en een zichtbare idle-indicator.
- Nieuwe `utils.SettingSpec` zorgt voor veilige typeconversie en eenvoudige export/import van instellingen.
# 3.3.3 — Noise reduction & session-stable logs
- **Qt-noise dempen**: bekende, irrelevante Qt waarschuwingen worden gefilterd.
- **Coalescing**: identieke meldingen binnen een venster (default 3s) worden samengevoegd; periodieke samenvatting in het log.
- **Eén logbestand per sessie** (instelbaar): herstart/stop binnen dezelfde QGIS-sessie blijft in hetzelfde bestand schrijven.
- Instellingen uitgebreid met toggles/venster voor bovenstaande opties.