#!/usr/bin/env node
/**
 * emit-og-images.mjs — build the static Open Graph cards.
 *
 *   npm run emit:og
 *
 * Social crawlers do not execute JavaScript and most of them do not render SVG,
 * so `og:image` has to be a real raster file that exists at build time. These
 * cards are generated once per entity *type* (the entity-specific part of a
 * share card is `og:title` / `og:description`), which keeps the export small
 * and the build deterministic: no network, no fonts to download, no per-entity
 * image generation for 5 000 profiles.
 *
 * `next/og` (Satori + resvg, bundled with Next) renders the PNG in Node. The
 * files are committed so a plain `next build` is self-contained; this script
 * regenerates them when the design changes.
 */

import { mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createElement as h } from 'react';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const OUT = path.join(ROOT, 'apps', 'web', 'public', 'og');

const LOGO = 'data:image/png;base64,' + readFileSync(path.join(ROOT, 'apps/web/public/media/logo.png')).toString('base64');

const CARDS = [
  { name: 'site', eyebrow: 'PUPPETNET · OSINT-KONSOLE', title: 'Netzwerke verstehen.\nQuellen prüfen.', accent: '#a2d686' },
  { name: 'person', eyebrow: 'ENTITÄTSPROFIL · PERSON', title: 'Personen.\nRollen mit Belegen.', accent: '#a2d686' },
  { name: 'organization', eyebrow: 'ENTITÄTSPROFIL · ORGANISATION', title: 'Organisationen.\nStrukturen im Kontext.', accent: '#e5c367' },
  { name: 'location', eyebrow: 'ENTITÄTSPROFIL · ORT', title: 'Orte.\nGeographie nachvollziehbar.', accent: '#f3c773' },
  { name: 'craft', eyebrow: 'ENTITÄTSPROFIL · FAHRZEUG', title: 'Fahrzeuge.\nRegister und Referenzen.', accent: '#ff9e91' },
  { name: 'document', eyebrow: 'ENTITÄTSPROFIL · DOKUMENT', title: 'Dokumente.\nQuellen im Original.', accent: '#7daeff' },
];

function card({ eyebrow, title, accent }) {
  return h('div', {
    style: {
      display: 'flex',
      flexDirection: 'column',
      justifyContent: 'space-between',
      width: '100%',
      height: '100%',
      padding: '64px 72px',
      background: 'linear-gradient(145deg, #0b100d 0%, #1a291c 55%, #0b100d 100%)',
      color: '#e9f0f9',
      fontFamily: 'sans-serif',
    },
  },
    h('div', { style: { display: 'flex', alignItems: 'center', gap: 18 } },
      h('img', { src: LOGO, width: 76, height: 76, style: { objectFit: 'contain' } }),
      h('div', { style: { display: 'flex', fontSize: 22, letterSpacing: 5, color: accent } }, 'PUPPETNET'),
    ),
    h('div', { style: { display: 'flex', flexDirection: 'column', gap: 22 } },
      h('div', { style: { display: 'flex', fontSize: 20, letterSpacing: 4, color: '#9fb3c9' } }, eyebrow),
      h('div', { style: { display: 'flex', fontSize: 76, fontWeight: 700, lineHeight: 1.08, whiteSpace: 'pre-wrap', letterSpacing: -1.5 } }, title),
    ),
    h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderTop: `2px solid ${accent}`, paddingTop: 22, fontSize: 20, color: '#b9c9dc' } },
      h('div', { style: { display: 'flex' } }, 'Entitäten · Beziehungen · Quellen'),
      h('div', { style: { display: 'flex', color: accent } }, 'Folge der Verbindung.'),
    ),
  );
}

const { ImageResponse } = await import('next/og.js').catch(() => {
  console.error('emit-og: this Next.js version does not expose next/og.js — upgrade Next or commit the existing cards.');
  process.exit(1);
});

mkdirSync(OUT, { recursive: true });
let total = 0;
for (const definition of CARDS) {
  const response = new ImageResponse(card(definition), { width: 1200, height: 630 });
  const buffer = Buffer.from(await response.arrayBuffer());
  const file = path.join(OUT, `${definition.name}.png`);
  writeFileSync(file, buffer);
  total += buffer.length;
  console.log(`emit-og: ${path.relative(ROOT, file)} — ${(buffer.length / 1024).toFixed(1)} KiB`);
}
console.log(`emit-og: ${CARDS.length} cards, ${(total / 1024).toFixed(1)} KiB total`);
