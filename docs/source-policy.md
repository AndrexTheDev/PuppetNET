# Quellen-Policy & Löschkette / Source Policy & Erasure Chain

> Welle 5 (Vertrauen & Compliance). Die öffentliche Fassung lebt auf `/legal/`
> (`apps/web/app/legal/page.tsx`); dieses Dokument ist die Operator-Vertiefung.
> Kosten: 0 zusätzliche Actions-Minuten — reine Dokumentation plus eine statische Seite.
>
> Wave 5 (trust & compliance). The public version lives at `/legal/`
> (`apps/web/app/legal/page.tsx`); this document is the operator deep-dive.
> Cost: 0 extra Actions minutes — documentation plus one static page.

## 1 · Rechtsrahmen / Legal framework

**DE.** EuGH C-37/20 und C-601/20 (22.11.2022) haben den allgemeinen öffentlichen
Zugang zu Registern wirtschaftlichen Eigentums für ungültig erklärt; das deutsche
Transparenzregister setzt seitdem ein berechtigtes Interesse voraus. Relevant sind
außerdem DSGVO Art. 17 (Löschung), Art. 21 (Widerspruch) und die
EDPB-Leitlinien 5/2019 zum Delisting bei Suchmaschinen.

**EN.** CJEU C-37/20 and C-601/20 (22 Nov 2022) invalidated general public access to
beneficial-ownership registers; Germany's Transparenzregister now requires a legitimate
interest. Also relevant: GDPR Art. 17 (erasure), Art. 21 (objection) and EDPB
Guidelines 5/2019 on search-engine delisting.

## 2 · Was die Pipeline ingestieren darf / What ingestion may use

| Gewicht / Weight | Quellen / Sources | Regel / Rule |
| --- | --- | --- |
| 1.0 | Offizielle offene Register, öffentlich freigegebene Leaks-Datensätze (`public_released`-Kennzeichnung) / official open registers, publicly released leak datasets | Nur frei zugängliche Endpunkte / freely accessible endpoints only |
| 0.9 | Wikidata, OpenCorporates | Offene Daten mit Lizenzhinweis / open data with licence note |
| 0.4 | Nachrichten & RSS / news & RSS | Kein Bezahlschranken-Umgehen / never bypass paywalls |

**DE. Verboten:** zugangsbeschränkte Register scrapen (Transparenzregister u. ä.),
Quellen ohne Rechtsgrundlage ergänzen, gelöschte Entitäten neu anreichern.
Eigene Quellen registriert der Betreiber als `SourceSpec` — das Gewicht folgt der
Quellenfamilie, die rechtliche Prüfung bleibt Betreibersache.

**EN. Forbidden:** scraping access-restricted registers (Transparenzregister and
similar), adding sources without a legal basis, re-enriching erased entities.
Operators register custom sources as a `SourceSpec`; the weight follows the source
family, the legal review stays the operator's duty.

## 3 · Freigabe- & Löschkette / Release & erasure chain

**DE.** `public_released: false` → `approved()`-Veto → der nächste Export pruned die
Entitäts-Slices über das Manifest, entfernt alle Kanten zur Entität aus
`graph_latest.json` und löscht veraltete Profilseiten physisch. Zwei Läufe täglich
(03:00/15:00 UTC) ⇒ Löschung regelmäßig **≤ 24 h** öffentlich wirksam, zuzüglich
kurzer CDN-Nachvalidierung. Sensible Prädikate (z. B. Strafvorwürfe) tragen immer
`requires_review` und erscheinen nie automatisch im öffentlichen Graphen.
Nur nach ausdrücklicher Betreiberfreigabe (`public_released: true`) erscheinen:
- **Personen ohne veröffentlichte Beziehung**;
- Entitäten, die nur das Cloud-Modell erkannt hat (`cloud_only`);
- unbestätigte KI-Kanten (`method: llm`). Bei einer freigegebenen KI-Kante
  müssen auch Cloud-only-Endpunkte freigegeben sein.

Der Grundsatz dahinter: Eine einzelne Namensnennung ohne Zusammenhang ist für eine
Netzwerkanalyse wertlos, als öffentliche Profilseite aber ein Eingriff.

**EN.** `public_released: false` → `approved()` veto → the next export prunes the
entity slices via the manifest, removes every edge to the entity from
`graph_latest.json` and physically deletes stale profile pages. Two runs per day
(03:00/15:00 UTC) ⇒ erasure normally public **within ≤ 24 h**, plus short CDN
revalidation. Sensitive predicates (e.g. accusations) always carry `requires_review`
and never reach the public graph automatically.
Only an explicit operator release (`public_released: true`) publishes:
- **persons without any published relationship**;
- entities only the cloud model recognized (`cloud_only`);
- uncorroborated cloud-model edges (`method: llm`). A released cloud edge also
  needs its cloud-only endpoints released.

The principle: a lone mention without context is worthless for network analysis
but intrusive as a public profile page.

## 4 · Kanten-Provenienz / Edge provenance

**DE.** Jede veröffentlichte Kante trägt `confidence`, `weight` und `evidenceIds`
(Dokumente mit Titel, Quelle, Datum, Original-URL). Im Workspace öffnet ein Klick auf
eine Kante den Inspector („Quellen dieser Kante"); Korrelations-Insights zitieren die
tragenden Kanten-IDs. Berechnete Verbindungen sind Strukturhinweise, keine
Tatsachenbehauptungen.

**EN.** Every published edge carries `confidence`, `weight` and `evidenceIds`
(documents with title, source, date, original URL). Clicking an edge in the workspace
opens the inspector ("sources of this edge"); correlation insights cite the edge ids
they rest on. Computed connections are structural hints, never factual claims.

## 5 · Betreiberpflichten / Operator duties

**DE.**
1. Lösch-/Widerspruchsanfragen über den auf `/legal/` dokumentierten Kanal entgegennehmen
   (öffentliches Repository-Issue oder Betreiberkontakt) und ohne Begründungszwang bearbeiten.
2. `public_released: false` setzen; im nächsten Laufbericht (Manifest/Entitätszahlen)
   gegenprüfen und den Nachweis aufbewahren.
3. Bei Suchmaschinen-Delisting (EDPB 5/2019) die Ablaufdokumentation als Nachweis bereitstellen.
4. Private Installationen: `Disallow: /` in `robots.txt` plus `X-Robots-Tag: noindex, nofollow`
   via `apps/web/public/_headers` — siehe `docs/web-console.md#hardening`.

**EN.**
1. Accept erasure/objection requests via the channel documented on `/legal/`
   (public repository issue or operator contact) — no justification required.
2. Set `public_released: false`; verify in the next run report (manifest/entity counts)
   and keep the proof.
3. For search-engine delisting (EDPB 5/2019), provide this workflow documentation as evidence.
4. Private deployments: `Disallow: /` in `robots.txt` plus `X-Robots-Tag: noindex, nofollow`
   via `apps/web/public/_headers` — see `docs/web-console.md#hardening`.
