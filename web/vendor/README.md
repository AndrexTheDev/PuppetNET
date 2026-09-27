# Vendored browser dependencies

Everything the console needs at runtime lives in this directory. **Nothing is
fetched from a CDN**, for three reasons that matter more than convenience here:

1. **Operator privacy.** An OSINT console that loads a script from a third-party
   origin tells that origin who is investigating what, and when. `cdn.tailwindcss.com`
   would see every analyst's IP address, referrer and session timing.
2. **Availability.** A pinned, committed copy cannot be replaced by a supply-chain
   attack on a package registry mirror, and cannot break when a CDN has an outage.
3. **Performance.** One origin, no DNS/TLS handshake per library, no render-blocking
   cross-origin requests. Cloudflare Pages serves these with brotli and immutable
   caching (see `web/_headers`).

## Inventory

| File | Package | Version | Licence | Uncompressed |
| --- | --- | --- | --- | --- |
| `layout-base.min.js` | [layout-base](https://www.npmjs.com/package/layout-base) | 2.0.1 | MIT | 57.2 KB |
| `cose-base.min.js` | [cose-base](https://www.npmjs.com/package/cose-base) | 2.2.0 | MIT | 43.2 KB |
| `cytoscape.min.js` | [cytoscape](https://www.npmjs.com/package/cytoscape) | 3.30.4 | MIT | 365 KB |
| `cytoscape-fcose.min.js` | [cytoscape-fcose](https://www.npmjs.com/package/cytoscape-fcose) | 2.2.0 | MIT | 19.7 KB |
| `qrcode.min.js` | [qrcode-generator](https://www.npmjs.com/package/qrcode-generator) | 2.0.4 | MIT | 20 KB |
| `tailwind.css` | [tailwindcss](https://www.npmjs.com/package/tailwindcss) | 3.4.17 | MIT | 12.7 KB (purged build) |

`layout-base` → `cose-base` → `cytoscape-fcose` is the UMD chain the fcose
force-directed layout needs; each attaches a browser global (`layoutBase`,
`coseBase`, `cytoscapeFcose`) and must load in that order, before `app.js`.
Load order is fixed in `web/index.html`.

`qrcode-generator` attaches the global `qrcode` and renders the donation addresses
in `web/modals.js` as inline SVG. It is used instead of a QR image service because a
`api.qrserver.com`-style call would hand a third party the address *and* the fact
that this operator is looking at it — the same reason nothing else here is on a CDN.
It must load before `modals.js`.

Each package's MIT licence text is kept alongside it as `LICENSE.<package>`.
`qrcode-generator` publishes no LICENSE file in its npm tarball, so
`LICENSE.qrcode-generator` is the upstream text taken verbatim from
[kazuhikoarase/qrcode-generator](https://github.com/kazuhikoarase/qrcode-generator/blob/master/LICENSE);
`npm run vendor:libs` verifies that file is still present instead of inventing one.

## Reproducing this directory

```bash
npm install                 # devDependencies in the root package.json
npm run vendor:libs         # copies dist files, minifies with terser
npm run build:css           # compiles tailwind.input.css -> vendor/tailwind.css
```

`scripts/vendor-libs.mjs` is the exact procedure: copy the published
`dist/cytoscape.min.js`, then run the three layout libraries through `terser`
(their npm packages ship unminified UMD bundles), then rewrite this table's
sizes. It fails loudly if a version in `package.json` does not match the version
recorded above, so a bump cannot silently drift from the documentation.

## `tailwind.css` is a build artefact — and it is committed on purpose

Cloudflare Pages can run a build command, but a static-only deployment is faster,
cheaper and cannot fail at deploy time because a registry was unreachable. The
committed stylesheet is therefore the source of truth for production, and CI
rebuilds it and fails the job if the committed copy is stale:

```bash
npm run build:css && git diff --exit-code web/vendor/tailwind.css
```

Because Tailwind purges unused classes by scanning `web/index.html`, `web/app.js`
and `web/styles.css`, any class built at runtime (`` `text-${colour}-400` ``) will
not exist in the output. Runtime-only classes must be added to `safelist` in
`web/tailwind.config.cjs`. The console paints per-entity colours from data using
inline styles precisely to avoid that trap.
