# PuppetNET – Release per Git-Push

> 🌐 **Sprachen · Languages:** Deutsch ↓ · [English](#english)

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
- [ ] `/`, `/dashboard/`, `/legacy` (Permanent-Redirect auf `/dashboard/`),
  veröffentlichte `/entity/<slug>/`,
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
- [ ] GH_PAT mit passenden Rechten hinterlegen; Branch-Regeln prüfen.
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

---

<a id="english"></a>

# PuppetNET – release via git push (English)

## 1. Prepare
- [ ] Stay on the release session branch; check `git status` and the **entire**
  diff (it also contains the previous pipeline/Zero-KV/UI work).
- [ ] `npm ci`, Node.js 22, then `npm run ci:pages`, `npm run ci:worker`,
  `node --test tests/release.test.mjs`, `git diff --check`.
- [ ] Set `SITE_ORIGIN` to the final HTTPS domain without a path.
  Default: `https://puppetnet-web.pages.dev`. Remove a differing old
  `NEXT_PUBLIC_SITE_ORIGIN`; `SITE_ORIGIN` takes precedence.
- [ ] Do not stage secrets, `.state`, build outputs or unreviewed personal data.
  Generated sitemaps/SEO manifests/token copies stay ignored. Robots sitemap
  lines are an intentional, idempotent build change.
- [ ] Check mobile 320/375/768 px and desktop 1440/1920 px: header without
  overlap, banners reserved at 320×50 and 160×600; test close + reload.
  Blocked localStorage must not cause an exception. The mobile safe distance
  remains after closing (deliberately no reflow).

## 2. Commit, push and preview
```sh
git branch --show-current
git diff --check
git add <reviewed-files>
git commit -m "Prepare static Pages release and consolidate design tokens"
git push origin <session-branch>
```
- [ ] Cloudflare Pages: repository root, build `npm run ci:pages`, output
  `apps/web/out`, Node 22; no Pages Functions/SSR and no KV required.
- [ ] Check the branch preview. A preview may deliberately use the production
  domain as canonical; `SITE_ORIGIN` is the canonical domain, not automatically
  the random preview domain.
- [ ] Test `/`, `/dashboard/`, `/legacy` (permanent redirect to `/dashboard/`),
  published `/entity/<slug>/`, `/data/graph_latest.json` (with a real
  publication), `/sitemap.xml`, `/robots.txt`, `/styles/globals.css` and
  `/og/site.png`.
- [ ] Sitemap URLs match the actually exported profiles; check the HTML source
  for canonical, `og:url`, `og:image`, `application/ld+json` and the demo hint.

## 3. Production release
- [ ] Open a PR from the session branch to the configured production branch,
  wait for checks, review and approve explicitly. No push to foreign branches
  from this session. Only the merge triggers the production build when Pages
  watches that branch.
- [ ] Check the production domain/HTTPS, cache headers and the new data
  revision; migrate the search worker separately per the Zero-KV runbook,
  secrets server-side only.
- [ ] Store a GH_PAT with suitable rights; check branch rules.
- [ ] The GitHub dispatch workflow must be registered on the default branch;
  pipeline activation and data release are separate steps.
- [ ] Control quotas, Actions executions and provider billing. The Cache API is
  not a global daily limit. Do not guarantee free permanent availability.

## Rollback
Restore the last successful production deployment in the Pages dashboard.
Then fix the fault via a reviewed revert PR; no forced branch resets.
After a rollback, re-check canonicals, data revision and profile/sitemap
consistency.

## Local browser verification

The 320/375/768/1440/1920 px checks are done; details and measurement limits:
[release browser test](testing/release-browser.md). This does not replace
checking the actual production deployment.

