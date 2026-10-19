# PuppetNET – Release per Git-Push

## 1. Vorbereiten
- [ ] Auf `arena/5fb0fe0b-puppetnet` bleiben; `git status` und den **gesamten**
  Diff prüfen (enthält auch die vorherigen Pipeline-/Zero-KV-/UI-Arbeiten).
- [ ] `npm ci`, Node.js 22, dann `npm run ci:pages`, `npm run ci:worker`,
  `node --test tests/release.test.mjs`, `git diff --check`.
- [ ] `SITE_ORIGIN` auf die endgültige HTTPS-Domain ohne Pfad setzen.
  Default: `https://puppetnet-web.pages.dev`. Ein abweichendes altes
  `NEXT_PUBLIC_SITE_ORIGIN` entfernen; `SITE_ORIGIN` hat Vorrang.
- [ ] Keine Secrets, `.state`, Buildausgaben oder ungeprüften personenbezogenen
  Daten stagen. Generierte Sitemaps/SEO-Manifeste/Token-Kopien bleiben ignoriert.
  Robots-Sitemap-Zeilen sind eine absichtliche, idempotente Buildänderung.
- [ ] Mobil 320/375/768 px und Desktop 1440/1920 px prüfen: Header ohne
  Überlappung, Banner 320×50 bzw. 160×600 reserviert; Schließen + Reload testen.
  Blockierter localStorage darf keine Ausnahme verursachen. Der mobile
  Sicherheitsabstand bleibt auch nach Schließen bestehen (bewusst kein Reflow).

## 2. Commit, Push und Preview
```sh
git branch --show-current
git diff --check
git add <geprüfte-Dateien>
git commit -m "Prepare static Pages release and consolidate design tokens"
git push origin arena/5fb0fe0b-puppetnet
```
- [ ] Cloudflare Pages: Repository-Root, Build `npm run ci:pages`, Output
  `apps/web/out`, Node 22; keine Pages Functions/SSR und kein KV erforderlich.
- [ ] Branch-Preview prüfen. Preview kann absichtlich die Produktionsdomain als
  Canonical verwenden; `SITE_ORIGIN` ist die kanonische Domain, nicht automatisch
  die zufällige Preview-Domain.
- [ ] `/`, `/dashboard/`, `/legacy/`, veröffentlichte `/entity/<slug>/`,
  `/data/graph_latest.json` (bei echter Publikation), `/sitemap.xml`, `/robots.txt`,
  `/styles/globals.css` und `/og/site.png` testen.
- [ ] Sitemap-URLs stimmen mit wirklich exportierten Profilen überein; HTML-Quelltext
  auf Canonical, `og:url`, `og:image`, `application/ld+json` und Demohinweis prüfen.

## 3. Produktionsfreigabe
- [ ] PR aus dem Sessionbranch auf den konfigurierten Produktionsbranch öffnen,
  Checks abwarten, reviewen und explizit freigeben. Kein Push auf fremde Branches
  aus dieser Sitzung. Erst der Merge löst den Produktionsbuild aus, wenn Pages
  diesen Branch beobachtet.
- [ ] Produktionsdomain/HTTPS, Cache-Header und neue Datenrevision prüfen;
  Search-Worker separat nach Zero-KV-Runbook migrieren, Secrets nur serverseitig.
- [ ] GitHub-Dispatch-Workflow muss auf dem Defaultbranch registriert sein;
  Pipeline-Aktivierung und Freigabe der Daten sind gesonderte Schritte.
- [ ] Kontingente, Actions-Ausführungen und Provider-Abrechnung kontrollieren.
  Cache API ist kein globales Tageslimit. Keine kostenlose Dauerverfügbarkeit garantieren.

## Rollback
Im Pages-Dashboard die letzte erfolgreiche Produktionsbereitstellung wiederherstellen.
Danach den Fehler per geprüftem Revert-PR beheben; keine erzwungenen Branch-Resets.
Nach Rollback Canonicals, Datenrevision und Profil-/Sitemap-Konsistenz erneut prüfen.

## Lokaler Browser-Prüfnachweis

Die Prüfungen für 320/375/768/1440/1920 px sind durchgeführt; Details und
Messgrenzen: [Release-Browsertest](testing/release-browser.md). Das ersetzt
nicht die Prüfung der tatsächlichen Produktionsbereitstellung.
