# Werbeplatzhalter

> 🌐 **Sprachen · Languages:** Deutsch ↓ · [English](#english)

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

---

<a id="english"></a>

# Ad placeholders (English)

The global Next interface contains two responsive containers in
`apps/web/app/components/AdContainers.tsx`. No external scripts, cookies or ad
providers are loaded. Integrate banners later via the `data-ad-slot` targets,
not by the visible label.

| Slot | Visibility | Format |
|---|---|---|
| `mobile-footer` | below 1100 px, sticky at the bottom window edge, closable | 320 × 50 |
| `desktop-header` | from 1100 px, before the page content | up to 970 × 90 |
| `desktop-footer` | from 1100 px, after the page content | up to 970 × 90 |
| `desktop-left`, `desktop-right` | from 1600 px, fixed at the sides | 160 × 600 each |

Side slots are deliberately hidden on narrower desktop windows so analysis
tools are not squeezed. On mobile, space below the document is reserved for the
sticky banner including the safe area. All ad surfaces disappear when printing.
The standalone legacy console has no additional ads so drawers, dialogs and the
canvas stay unobstructed. The previous homepage embed was replaced by direct
links.

Before integrating an ad network, separately check its consent/privacy
requirements, the Cloudflare CSP, keyboard operability and responsive banner
sizes. Do not embed a foreign script without this review.

