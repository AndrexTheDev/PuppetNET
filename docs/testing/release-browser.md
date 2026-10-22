# Lokaler Release-Browsertest

> 🌐 **Sprachen · Languages:** Deutsch ↓ · [English](#english)

Geprüft am 19.10.2026 mit Headless Chromium gegen den statischen Export.
Keine Cloudflare-Produktion und keine echten Recherche-Dispatches wurden aufgerufen.

## Ergebnisse

| Breite | Seite | Horizontaler Overflow | Layout-Shift-Summe |
| --- | --- | --- | --- |
| 320 px | `/` | nein | 0.00000 |
| 320 px | `/dashboard/` | nein | 0.03791 |
| 320 px | `/legacy/` | nein | 0.00046 |
| 375 px | `/` | nein | 0.00000 |
| 375 px | `/dashboard/` | nein | 0.03250 |
| 375 px | `/legacy/` | nein | 0.00040 |
| 768 px | `/` | nein | 0.00000 |
| 768 px | `/dashboard/` | nein | 0.03598 |
| 768 px | `/legacy/` | nein | 0.00019 |
| 1440 px | `/` | nein | 0.00000 |
| 1440 px | `/dashboard/` | nein | 0.01799 |
| 1440 px | `/legacy/` | nein | 0.00000 |
| 1920 px | `/` | nein | 0.00000 |
| 1920 px | `/dashboard/` | nein | 0.01096 |
| 1920 px | `/legacy/` | nein | 0.00000 |

Die Summe umfasst nicht benutzerverursachte Layout-Shifts während eines kurzen lokalen Seitenladens (ca. 1,6 Sekunden). Sie ist **keine Feldmessung** und keine Garantie für Core Web Vitals unter realen Netzwerk-/Gerätebedingungen. Intro wurde für diese Messung durch Reduced Motion übersprungen.

## Zusätzlich bestanden

- Banner-Schließen bleibt nach Reload bei 320, 375 und 768 px erhalten.
- Reservierter unterer Abstand ändert sich beim Schließen/Reload nicht.
- Blockierter localStorage: Schließen funktioniert ohne unbehandelte Browserausnahme.
- Desktop-Header-Banner 90 px hoch; Skyscraper bei 1920 px exakt 160×600 px.
- Keine unbehandelten JavaScript-Ausnahmen in den geprüften Ansichten.
- Bestehender Layout-/Drawer-Test bei 360, 820, 1280 und 1920 px bestanden.
- 320-px-Filterüberlauf gefunden und durch schrumpfbare Grid-Spalten / zwei Spalten auf kleinen Smartphones korrigiert.
- Vollständiger `npm run ci:pages` nach Korrektur erneut erfolgreich.

## Reproduktion

Optional Playwright und Chromium außerhalb der Runtime-Abhängigkeiten bereitstellen, Export auf Port 8080 servieren und ausführen:

```sh
SITE_URL=http://127.0.0.1:8080 node tests/release_browser.mjs
SITE_URL=http://127.0.0.1:8080 node tests/site_browser.mjs
SITE_URL=http://127.0.0.1:8080 node tests/entity_browser.mjs
```

Für vorhandenes Chromium können `CHROMIUM_EXECUTABLE` und `CHROMIUM_ARGS` gesetzt werden. Screenshots und JSON-Ergebnisse liegen ignoriert unter `.screenshots/release/`.

## Vor Produktionsfreigabe weiterhin offen

- Tatsächliche Pages-Build-/Domain-/Cache-Konfiguration prüfen.
- Echte Veröffentlichung, Datenrevision und Search-Worker separat validieren.
- Live-Feldmetriken und Tests unter gedrosseltem Mobilnetz.
- Commit/Push/PR-Freigabe: bislang nicht ausgeführt.

---

<a id="english"></a>

# Local release browser test (English)

Checked on 2026-10-19 with headless Chromium against the static export.
No Cloudflare production and no real research dispatches were called.

## Results

| Width | Page | Horizontal overflow | Layout-shift sum |
| --- | --- | --- | --- |
| 320 px | `/` | no | 0.00000 |
| 320 px | `/dashboard/` | no | 0.03791 |
| 320 px | `/legacy/` | no | 0.00046 |
| 375 px | `/` | no | 0.00000 |
| 375 px | `/dashboard/` | no | 0.03250 |
| 375 px | `/legacy/` | no | 0.00040 |
| 768 px | `/` | no | 0.00000 |
| 768 px | `/dashboard/` | no | 0.03598 |
| 768 px | `/legacy/` | no | 0.00019 |
| 1440 px | `/` | no | 0.00000 |
| 1440 px | `/dashboard/` | no | 0.01799 |
| 1440 px | `/legacy/` | no | 0.00000 |
| 1920 px | `/` | no | 0.00000 |
| 1920 px | `/dashboard/` | no | 0.01096 |
| 1920 px | `/legacy/` | no | 0.00000 |

The sum does not include user-caused layout shifts during a short local page load
(~1.6 seconds). It is **not a field measurement** and no guarantee for Core Web
Vitals under real network/device conditions. The intro was skipped for this
measurement via reduced motion.

## Additionally passed

- Banner close persists after reload at 320, 375 and 768 px.
- The reserved bottom distance does not change on close/reload.
- Blocked localStorage: close works without an unhandled browser exception.
- Desktop header banner 90 px high; skyscraper exactly 160×600 px at 1920 px.
- No unhandled JavaScript exceptions in the checked views.
- Existing layout/drawer test passed at 360, 820, 1280 and 1920 px.
- 320-px filter overflow found and fixed via shrinkable grid columns / two
  columns on small smartphones.
- Full `npm run ci:pages` successful again after the fix.

## Reproduction

Optionally provide Playwright and Chromium outside the runtime dependencies,
serve the export on port 8080 and run:

```sh
SITE_URL=http://127.0.0.1:8080 node tests/release_browser.mjs
SITE_URL=http://127.0.0.1:8080 node tests/site_browser.mjs
SITE_URL=http://127.0.0.1:8080 node tests/entity_browser.mjs
```

For an existing Chromium, `CHROMIUM_EXECUTABLE` and `CHROMIUM_ARGS` can be set.
Screenshots and JSON results live ignored under `.screenshots/release/`.

## Still open before production release

- Check the actual Pages build/domain/cache configuration.
- Validate the real publication, data revision and search worker separately.
- Live field metrics and tests under a throttled mobile network.
- Commit/push/PR approval: not executed so far.
