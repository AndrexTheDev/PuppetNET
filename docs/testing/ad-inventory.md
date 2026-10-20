# Werbeplatzhalter

Die globale Next-Oberfläche enthält zwei responsive Container in
`apps/web/app/components/AdContainers.tsx`. Es werden keine externen Skripte,
Cookies oder Anzeigenanbieter geladen. Banner später an den `data-ad-slot`-Zielen
integrieren, nicht anhand der sichtbaren Beschriftung.

| Slot | Sichtbarkeit | Format |
|---|---|---|
| `mobile-footer` | unter 1100 px, sticky am unteren Fensterrand, schließbar | 320 × 50 |
| `desktop-header` | ab 1100 px, vor dem Seiteninhalt | bis 970 × 90 |
| `desktop-footer` | ab 1100 px, nach dem Seiteninhalt | bis 970 × 90 |
| `desktop-left`, `desktop-right` | ab 1600 px, seitlich fixiert | je 160 × 600 |

Seitliche Slots werden bei schmaleren Desktopfenstern bewusst ausgeblendet, damit
Analysewerkzeuge nicht eingeengt werden. Mobil ist Platz unter dem Dokument für den
Sticky-Banner inklusive Safe-Area reserviert. Alle Werbeflächen verschwinden beim
Drucken. Die eigenständige Legacy-Konsole hat keine zusätzlichen Anzeigen, damit
Drawer, Dialoge und Canvas unbehindert bleiben. Die vorherige Startseiten-Einbettung
wurde durch direkte Links ersetzt.

Vor dem Einbinden eines Werbenetzwerks dessen Consent-/Datenschutzanforderungen,
Cloudflare-CSP, Tastaturbedienung und responsive Bannergrößen separat prüfen.
Kein fremdes Script ohne diese Prüfung einfügen.
