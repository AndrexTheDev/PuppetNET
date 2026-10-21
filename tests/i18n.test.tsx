/**
 * Wave 6 i18n contract.
 *
 * 1. Dictionary parity: English covers every German key, no empty values and
 *    identical {param} placeholders — a half-translated UI is a release bug.
 * 2. Pinned German strings survive byte-identical (the a11y suite additionally
 *    asserts the rendered markup).
 * 3. English render smoke: the same components produce a fully English,
 *    accessible tree under the fixed-language provider.
 * 4. The keyboard model announces in both languages.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import { createRequire } from 'node:module';

import { DICTIONARIES, NUMBER_LOCALE, STRING_KEYS, translate } from '../apps/web/lib/i18n-strings';
import { describeGraphNode, graphInstructions } from '../apps/web/lib/graph-keyboard';

// The provider and the components must share ONE instance of the context
// module. The components are compiled as CommonJS and `require('./i18n')`.
// Node 22 before 22.18 (e.g. 22.16.0, the Cloudflare Pages v3 default image)
// evaluates a CommonJS file imported from ESM separately from the copy a
// CommonJS sibling requires: two React contexts, so the English provider was
// invisible and every Pages build since wave 6 failed while local runs on a
// newer Node passed. Loading everything through `require` keeps one instance
// on every supported Node version.
const requireCjs = createRequire(import.meta.url);
const { FixedLanguageProvider } = requireCjs('../apps/web/app/dashboard/i18n') as typeof import('../apps/web/app/dashboard/i18n');
const BootStateModule = requireCjs('../apps/web/app/dashboard/BootState') as typeof import('../apps/web/app/dashboard/BootState');
const EntityTableModule = requireCjs('../apps/web/app/dashboard/EntityTable') as typeof import('../apps/web/app/dashboard/EntityTable');
const { LoadingStage } = BootStateModule;

// tsx CJS interop: default exports can arrive wrapped (same shim as a11y.test.tsx).
const StandbyPanel = (BootStateModule.default as unknown as { default?: typeof BootStateModule.default }).default ?? BootStateModule.default;
const EntityTable = (EntityTableModule.default as unknown as { default?: typeof EntityTableModule.default }).default ?? EntityTableModule.default;

const PARAM_RE = /\{(\w+)\}/g;

function params(value: string): string[] {
  return [...value.matchAll(PARAM_RE)].map(match => match[1]).sort();
}

test('every German key exists in English with identical placeholders', () => {
  for (const key of STRING_KEYS) {
    const de = DICTIONARIES.de[key];
    const en = DICTIONARIES.en[key];
    assert.ok(typeof en === 'string' && en.length > 0, `English value missing for "${key}"`);
    assert.ok(de.length > 0, `German value missing for "${key}"`);
    assert.deepEqual(params(en), params(de), `placeholder mismatch for "${key}"`);
  }
  assert.equal(Object.keys(DICTIONARIES.en).length, Object.keys(DICTIONARIES.de).length);
});

test('translate substitutes params and falls back honestly', () => {
  assert.equal(translate('de', 'table.pageOf', { page: 2, pageCount: 3 }), 'Seite 2 von 3');
  assert.equal(translate('en', 'table.pageOf', { page: 2, pageCount: 3 }), 'Page 2 of 3');
  assert.equal(translate('en', 'type.Vehicle'), 'Vehicle or aircraft');
  // Unknown keys render the key itself — never an empty string in the a11y tree.
  assert.equal(translate('en', 'does.not.exist'), 'does.not.exist');
  assert.deepEqual(NUMBER_LOCALE, { de: 'de-DE', en: 'en-GB' });
});

test('pinned German chrome strings are byte-identical in the dictionary', () => {
  assert.equal(DICTIONARIES.de['boot.standbyTitle'], 'System im Bereitschaftsmodus.');
  assert.equal(DICTIONARIES.de['boot.demoButton'], 'Demo-Datensatz laden');
  assert.equal(DICTIONARIES.de['boot.reasonEmpty'], 'Leere Veröffentlichung');
  assert.match(DICTIONARIES.de['boot.demoNote'], /fiktiv[a-z]* Beispieldaten/);
  assert.equal(DICTIONARIES.de['inspector.edgeSources'], 'Quellen dieser Kante');
  assert.equal(DICTIONARIES.de['table.empty'], 'Keine passenden Einträge.');
  assert.equal(DICTIONARIES.de['table.searchLabel'], 'Geladene Ansicht durchsuchen');
});

function standbyProps(reason: 'empty' | 'unavailable' = 'unavailable') {
  return {
    standby: reason === 'empty'
      ? { reason: 'empty' as const, message: 'Der veröffentlichte Datensatz ist leer — zum Beispiel nach einer vollständigen Datenrücknahme.' }
      : { reason: 'unavailable' as const, message: 'Kein gültiger CDN-Datensatz erreichbar (offline, fehlend oder ungültiges Format).' },
    busy: false,
    onDemo: () => {},
    onResearch: () => {},
    onReload: () => {},
  };
}

function row(overrides: Record<string, unknown> = {}) {
  return {
    id: '1', label: 'Anna Berg', type: 'Person', confidence: 0.82, degree: 3,
    rank: 0.12, group: false, ...overrides,
  } as never;
}

function renderEn(node: React.ReactElement): string {
  return renderToStaticMarkup(createElement(FixedLanguageProvider, { lang: 'en' }, node));
}

test('the standby panel renders fully English under the language provider', () => {
  const html = renderEn(createElement(StandbyPanel, standbyProps()));
  assert.match(html, /System in standby mode\./);
  assert.match(html, /STANDBY MODE · No dataset found/);
  assert.match(html, /<button type="button" class="primary-button"[^>]*>Load demo dataset<\/button>/);
  assert.match(html, /fictional sample data/);
  assert.doesNotMatch(html, /role="alert"/, 'standby stays a state, not an error');
  const empty = renderEn(createElement(StandbyPanel, standbyProps('empty')));
  assert.match(empty, /Empty publication/);
});

test('the loading stage and table render English without losing structure', () => {
  const loading = renderEn(createElement(LoadingStage));
  assert.match(loading, /aria-busy="true"/);
  assert.match(loading, /graph_latest\.json/);
  assert.match(loading, /Published dataset is being checked\./);
  const table = renderEn(createElement(EntityTable, {
    rows: [row()], total: 1, page: 0, pageSize: 50, query: '',
    onQuery: () => {}, onPage: () => {}, onSelect: () => {}, onFocusGraph: () => {},
  }));
  assert.match(table, /<caption>Nodes of the loaded view\. Page 1 of 1, entries 1 to 1 of 1\.<\/caption>/);
  assert.match(table, /Search the loaded view/);
  assert.match(table, /aria-label="Scrollable table"/);
  assert.match(table, /Details<span class="visually-hidden"> for Anna Berg<\/span>/);
  const emptyTable = renderEn(createElement(EntityTable, {
    rows: [], total: 0, page: 0, pageSize: 50, query: 'zzz',
    onQuery: () => {}, onPage: () => {}, onSelect: () => {}, onFocusGraph: () => {},
  }));
  assert.match(emptyTable, /No matching entries\./);
});

test('without a provider everything stays German (static rendering contract)', () => {
  const html = renderToStaticMarkup(createElement(StandbyPanel, standbyProps()));
  assert.match(html, /System im Bereitschaftsmodus\./);
  assert.match(html, /Demo-Datensatz laden/);
});

test('the graph keyboard announces in both languages', () => {
  const node = { id: '2', label: 'Acme GmbH', type: 'Organization', confidence: 0.82 };
  assert.equal(
    describeGraphNode(node, 1, 9),
    'Knoten 2 von 9: Acme GmbH. Typ Organisation. Confidence 82 Prozent. Eingabe öffnet die Details.',
  );
  assert.equal(
    describeGraphNode(node, 1, 9, 'en'),
    'Node 2 of 9: Acme GmbH. Type Organization. Confidence 82 percent. Enter opens the details.',
  );
  assert.match(graphInstructions(), /Pfeiltasten/);
  assert.match(graphInstructions(), /Tabellenansicht/);
  assert.match(graphInstructions('en'), /Arrow keys/);
  assert.match(graphInstructions('en'), /table view/);
});
