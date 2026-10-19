# Public entity profiles — prerender, structured data, Open Graph and sitemap

Every published entity has its own crawlable page at `/entity/<slug>/`. Pages are
written by `next build` as plain HTML files into `apps/web/out/entity/<slug>/index.html`;
there is no SSR, no Pages Function and no per-request work, so profile traffic costs
nothing beyond static asset delivery on the Cloudflare Pages Free plan.

## Rendering contract

`apps/web/app/entity/[id]/page.tsx` is a static route:

- `export const dynamic = 'force-static'` and `dynamicParams = false` — an unknown slug
  is a build-time 404, not a runtime render.
- `generateStaticParams()` returns `listPublicEntities().map(({ slug }) => ({ id: slug }))`,
  so the route set and the dataset can never disagree.
- Metadata comes from `buildEntityMetadata()`, the visible page from the same normalized
  entity, and both are pure functions of the build-time dataset. No browser API is
  required to read a profile, which is also what makes the pages indexable.

The output directory mirrors the URL: `out/entity/<slug>/index.html` plus the shared
`_next/static` chunks. `tests/export_smoke.mjs` walks `out/entity/*/index.html` after
every build and asserts language, one `<h1>`, canonical URL, the Open Graph and Twitter
set, `robots: index, follow`, an absolute `og:image`, valid JSON-LD, a unique `@id` per
page, and that every root-relative internal link resolves to an exported file.

## What gets published

`apps/web/lib/entity-seo.ts` owns the publication rules; `apps/web/lib/entity-store.ts`
reads the dataset and the page, sitemap and dashboard all call the same module.

| Rule | Value |
| --- | --- |
| Types with a page by default | `Person`, `Organization`, `Location`, `Craft` |
| Types that need an explicit flag | `Document`, `Other` — only with `public: true` |
| Always excluded | `public: false` and `noindex: true` |
| Page cap per build | `MAX_ENTITY_PAGES = 5 000` |
| Related entities per page | `MAX_RELATED_ENTITIES = 12`, filtered to published neighbours |
| Evidence references per page | `MAX_EVIDENCE_REFERENCES = 20` |
| Slug | `slugify(name)` plus a stable 7-character FNV-1a base-36 suffix |

The cap keeps one deployment inside the Pages file budget while leaving room for JS
chunks, OG cards and the legacy console. The slug suffix is derived from the canonical
id, so renaming an entity changes the readable prefix but not the collision space; the
sitemap and the page set are generated from the same list, so a 404 cannot be published.

Analytical values (risk score, `PUPPET_MASTER_OF` rank, centrality) are deliberately
**not** published. A computed suspicion is not a statement about a real person; the
profile only shows evidencable facts, identifiers and edges that exist in the export.

## Dataset: demo fallback vs. published export

`apps/web/public/data/entities.sample.json` is the committed fictional demo dataset and
the fallback. `apps/web/public/data/entities.json` is the *publication* artefact, is
git-ignored, and wins when present.

```sh
# Publish a pipeline or Worker graph export (file or stdin):
npm run emit:entities -- --input graph-export.json
curl -s "$WORKER/graph/overview" | npm run emit:entities -- --input -

# Optional flags: --out, --limit, --allow-empty
```

`scripts/emit-entity-data.ts` normalizes the export through the same
`normalizeEntityDataset()` used at build time, then:

- refuses to write **zero** publishable entities unless `--allow-empty` is passed, because
  an empty graph would silently unpublish every profile and replace the sitemap with a
  two-URL stub;
- rebuilds `edges` from the out-direction `related` list so a later reload of
  `entities.json` yields the same pages instead of dropping every relationship;
- stamps `demo: false` and writes `generatedAt` for the page footer.

Nothing is fetched at build time. A Pages build without `entities.json` therefore ships
the demo dataset, which is why a production build has to run `emit:entities` before
`next build` (or commit a generated file through the pipeline's own publication job).

## JSON-LD

Each page embeds one `<script type="application/ld+json">` with a Schema.org `@graph`:

1. `WebSite` — `@id` `<origin>/#website`, name, URL, `inLanguage: de`.
2. The entity itself — `Person`, `Organization`, `Place`, `Vehicle`, `CreativeWork` or
   `Thing` (`schemaTypeFor()`), with `@id` `<profile-url>#entity`,
   `mainEntityOfPage`, `isPartOf` the WebSite, `dateModified`, `alternateName`
   (aliases), `address` for locations, and `identifier` entries
   (`PropertyValue` with `propertyID`) for registration numbers, LEI, IMO and similar.
3. `BreadcrumbList` — home → dashboard → entity, matching the visible breadcrumb.

`serializeJsonLd()` escapes `<`, `>`, `&` and the two line separators after
`JSON.stringify`, so an entity name containing `</script>` cannot break out of the
element. `tests/seo.test.ts` covers escaping, the graph shape and the identifier mapping.

`next/og` route handlers cannot exist under `output: 'export'`, so there is no
per-entity generated image. Codes use one of six pre-rendered cards instead.

## Open Graph and Twitter cards

`apps/web/public/og/{site,person,organization,location,craft,document}.png` are committed,
1200 × 630 PNGs generated once by `scripts/emit-og-images.mjs` (`npm run emit:og`).
They are static files: a share preview needs no function, no font download and no
request-time rasterization. `ogImageFor(kind)` picks the card, and the page metadata
writes absolute `og:image`, `og:image:width|height|alt`, `og:url`, `og:site_name`,
`og:locale=de_DE`, `og:type` (`profile` for people, `website` otherwise) and the matching
`twitter:*` tags with `summary_large_image`.

Absolute URLs come from `SITE_ORIGIN` (or `NEXT_PUBLIC_SITE_ORIGIN`) at build time,
falling back to `DEFAULT_SITE_ORIGIN`. A social crawler cannot resolve relative paths,
so a missing origin is a real regression; `tests/export_smoke.mjs` fails the build gate
if any card or canonical URL is relative.

## Sitemap and robots

`scripts/emit-seo-files.mjs` runs before `next build` in `npm run ci:pages` with
`--build-gate`. For every target it emits a sitemap containing:

| URL | Priority | Change frequency |
| --- | --- | --- |
| `/` | 1.0 | weekly |
| `/dashboard/` | 0.9 | weekly |
| `/entity/<slug>/` | 0.7 | monthly |

The same run rewrites the `Sitemap:` line in `apps/web/public/robots.txt` and keeps the
committed files origin-free. Without `SITE_ORIGIN` nothing is written and the build
continues; an unusable origin only warns under `--build-gate`, because a missing sitemap
must not take a deployment down. Generated sitemaps and `entities.json` are git-ignored:
a committed sitemap would have to guess a hostname.

`npm run emit:sitemap -- --origin https://… --out /tmp/x` is the standalone renderer used
by `tests/seo.test.ts`. Past `limit` (default 50 000) URLs the writer switches to a
`sitemapindex` plus `sitemap-N.xml` chunks; the public entity cap is far below that, so a
single file is the normal case.

`apps/web/public/robots.txt` documents the three crawlable document kinds and keeps
`Allow: /`. `_headers` sets `Cache-Control: no-cache` on `/entity` and `/entity/*` so a
new publication is visible immediately, and caches `/og/*` for a week with revalidation.

## Accessibility of the profile pages

The profiles are reading pages, so they follow the same WCAG 2.1 AA baseline as the
dashboard:

- a skip link to `<main id="main-content" tabindex="-1">`, one `<h1>`, ordered `<h2>`
  sections and real `<nav>`, `<header>`, `<footer>` landmarks;
- facts use `<dl>` with `<dt>`/`<dd>`; dates use `<time datetime>`; registrations use
  `<code>` without relying on colour;
- the relation list announces direction in text (`ausgehende Beziehung zu`) and hides
  the arrow glyph from assistive technology;
- the decorative evidence index is `aria-hidden`, external links carry
  `rel="noopener noreferrer nofollow"` and a visually hidden „öffnet in einem neuen Tab";
- a visible demo disclaimer marks every fictional dataset, so structured data and page
  copy never present the demo as real intelligence.

## Verification

```sh
npm run test:seo        # publication rules, slugs, JSON-LD, metadata, sitemap
npm run test:a11y       # keyboard model, live region, table semantics, link rules
npm run test:export     # after a build: profiles, JSON-LD, links, OG cards, headers
npm run ci:pages        # complete Pages gate incl. build and export smoke test
```

`npm run test:a11y` renders the table with `react-dom/server`; it does not need a browser.
The visual and keyboard behaviour of the canvas is covered by the optional Playwright
script described in `docs/webgl-dashboard.md`.
