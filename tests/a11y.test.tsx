import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import EntityTableExport, { type TableRow } from '../apps/web/app/dashboard/EntityTable';

// tsx loads the component through a CJS interop wrapper in this repository
// (root package.json is ESM, the app is not a package); unwrap it once.
const EntityTable = (EntityTableExport as unknown as { default?: typeof EntityTableExport }).default ?? EntityTableExport;
import {
  describeGraphNode,
  graphInstructions,
  graphKeyAction,
  nextIndex,
} from '../apps/web/lib/graph-keyboard';

function row(overrides: Partial<TableRow>): TableRow {
  return {
    id: 'per:anna-berg',
    label: 'Anna Berg',
    type: 'Person',
    confidence: 0.8,
    x: 0,
    y: 0,
    cluster: 'nordlicht',
    evidenceIds: [],
    degree: 4,
    ...overrides,
  };
}

function render(overrides: Partial<Parameters<typeof EntityTable>[0]> = {}) {
  return renderToStaticMarkup(createElement(EntityTable, {
    rows: [row({})],
    total: 1,
    page: 0,
    pageSize: 50,
    query: '',
    onQuery: () => {},
    onPage: () => {},
    onSelect: () => {},
    onFocusGraph: () => {},
    ...overrides,
  }));
}

/* ------------------------------------------------------------------------- */
/* Keyboard model                                                            */
/* ------------------------------------------------------------------------- */

test('every graph action has a keyboard equivalent and nothing else is swallowed', () => {
  assert.deepEqual(graphKeyAction('ArrowRight'), { type: 'move', delta: 1 });
  assert.deepEqual(graphKeyAction('ArrowDown'), { type: 'move', delta: 1 });
  assert.deepEqual(graphKeyAction('ArrowLeft'), { type: 'move', delta: -1 });
  assert.deepEqual(graphKeyAction('ArrowUp'), { type: 'move', delta: -1 });
  assert.deepEqual(graphKeyAction('Home'), { type: 'first' });
  assert.deepEqual(graphKeyAction('End'), { type: 'last' });
  assert.deepEqual(graphKeyAction('Enter'), { type: 'select' });
  assert.deepEqual(graphKeyAction(' '), { type: 'select' });
  assert.deepEqual(graphKeyAction('Escape'), { type: 'clear' });
  assert.deepEqual(graphKeyAction('+'), { type: 'zoom', factor: 0.8 });
  assert.deepEqual(graphKeyAction('-'), { type: 'zoom', factor: 1.25 });
  assert.deepEqual(graphKeyAction('0'), { type: 'reset' });
  for (const key of ['Tab', 'a', 'Shift', 'F5', '?']) {
    assert.deepEqual(graphKeyAction(key), { type: 'none' }, `${key} must not be intercepted`);
  }
});

test('focus movement wraps instead of trapping at the ends', () => {
  assert.equal(nextIndex(0, 1, 3), 1);
  assert.equal(nextIndex(2, 1, 3), 0);
  assert.equal(nextIndex(0, -1, 3), 2);
  assert.equal(nextIndex(0, 1, 0), 0);
  assert.equal(nextIndex(Number.NaN, 1, 4), 1);
});

test('the announcement names the position, type and confidence', () => {
  const text = describeGraphNode({ id: 'a', label: 'Anna Berg', type: 'Organization', confidence: 0.82 }, 1, 9);
  assert.match(text, /Knoten 2 von 9/);
  assert.match(text, /Anna Berg/);
  assert.match(text, /Organisation/);
  assert.match(text, /Confidence 82 Prozent/);
  assert.match(text, /Eingabe/);
});

test('the keyboard instructions mention the table alternative', () => {
  const instructions = graphInstructions();
  assert.match(instructions, /Pfeiltasten/);
  assert.match(instructions, /Escape/);
  assert.match(instructions, /Tabellenansicht/);
});

/* ------------------------------------------------------------------------- */
/* Table alternative                                                         */
/* ------------------------------------------------------------------------- */

test('the table exposes caption, row and column headers', () => {
  const html = render();
  assert.match(html, /<table aria-describedby="table-description">/);
  assert.match(html, /<caption>Knoten der geladenen Ansicht\. Seite 1 von 1, Einträge 1 bis 1 von 1\.<\/caption>/);
  assert.equal((html.match(/scope="col"/g) ?? []).length, 6);
  assert.match(html, /<th scope="row">/);
  assert.match(html, /<span class="table-entity-label">Anna Berg<\/span>/);
  assert.match(html, /<code class="table-entity-key">per:anna-berg<\/code>/);
});

test('search and pagination are labelled and keyboard-scrollable', () => {
  const html = render({ total: 120, page: 1 });
  assert.match(html, /<label class="search-label" for="entity-table-search">Geladene Ansicht durchsuchen<\/label>/);
  assert.match(html, /id="entity-table-search"/);
  assert.match(html, /role="region" aria-label="Tabelle mit Bildlauf" tabindex="0"/);
  assert.match(html, /Einträge 51 bis 100 von 120/);
  assert.match(html, /aria-label="Tabellenseiten"/);
  assert.match(html, /aria-label="Vorherige Seite"/);
  assert.match(html, /aria-label="Nächste Seite"/);
  assert.match(html, /aria-live="polite">Seite 2 von 3</);
});

test('every graph action exists as a button or link with an accessible name', () => {
  const html = render();
  assert.match(html, /<button type="button">Details<span class="visually-hidden"> zu Anna Berg<\/span><\/button>/);
  assert.match(html, /<button type="button">Im Graph<span class="visually-hidden"> auf Anna Berg fokussieren<\/span><\/button>/);
  assert.match(html, /<a href="\/entity\/anna-berg-[a-z0-9]+\/">Profil<span class="visually-hidden"> von Anna Berg<\/span><\/a>/);
});

test('only individual public entities get a profile link', () => {
  const group = render({ rows: [row({ id: 'group:organization:1', label: 'Organisation · 12', group: true, count: 12 })] });
  assert.doesNotMatch(group, /Profil<span/);
  const document = render({ rows: [row({ id: 'doc:1', label: 'Dokument', type: 'Document' })] });
  assert.doesNotMatch(document, /Profil<span/);
});

test('an empty result is announced instead of rendering an empty table silently', () => {
  const html = render({ rows: [], total: 0 });
  assert.match(html, /<p role="status">Keine passenden Einträge\.<\/p>/);
  assert.match(html, /Einträge 0 bis 0 von 0/);
});

test('the table description explains that it is the non-visual alternative', () => {
  const html = render();
  assert.match(html, /Textalternative zum WebGL-Graphen/);
  assert.match(html, /mit Tastatur und\s+Screenreader bedienbar/);
});

/* ------------------------------------------------------------------------- */
/* Standby / onboarding state (no usable publication)                        */
/* ------------------------------------------------------------------------- */

import StandbyPanelExport, { LoadingStage } from '../apps/web/app/dashboard/BootState';
const StandbyPanel = (StandbyPanelExport as unknown as { default?: typeof StandbyPanelExport }).default ?? StandbyPanelExport;

function renderStandby(reason: 'unavailable' | 'empty' = 'unavailable') {
  return renderToStaticMarkup(createElement(StandbyPanel, {
    standby: reason === 'empty'
      ? { reason: 'empty', message: 'Der veröffentlichte Datensatz ist leer — zum Beispiel nach einer vollständigen Datenrücknahme.' }
      : { reason: 'unavailable', message: 'Kein gültiger CDN-Datensatz erreichbar (offline, fehlend oder ungültiges Format).' },
    busy: false,
    onDemo: () => {},
    onResearch: () => {},
    onReload: () => {},
  }));
}

test('standby explains the state instead of showing an error alert', () => {
  const html = renderStandby();
  assert.match(html, /System im Bereitschaftsmodus\./);
  assert.match(html, /Zeige Demonstrationsdaten oder starte eine On-Demand-Recherche\./);
  assert.match(html, /Kein gültiger CDN-Datensatz erreichbar/);
  assert.match(html, /Leere Veröffentlichung|Kein Datensatz gefunden/, 'the concrete reason is labelled');
  assert.doesNotMatch(html, /role="alert"/, 'standby is a state, not an error');
});

test('standby offers a one-click demo load and labels it as fictional', () => {
  const html = renderStandby();
  assert.match(html, /<button type="button" class="primary-button"[^>]*>Demo-Datensatz laden<\/button>/);
  assert.match(html, /On-Demand-Recherche/);
  assert.match(html, /fiktiv[a-z]* Beispieldaten/, 'the demo must stay clearly fictional');
  assert.match(html, /aria-labelledby="standby-title"/, 'the panel heading is named for screen readers');
});

test('an empty publication gets its own standby reason', () => {
  const html = renderStandby('empty');
  assert.match(html, /Leere Veröffentlichung/);
  assert.match(html, /vollständigen Datenrücknahme/);
});

test('the loading stage is polite and names the file under review', () => {
  const html = renderToStaticMarkup(createElement(LoadingStage));
  assert.match(html, /aria-busy="true"/);
  assert.match(html, /role="status"/);
  assert.match(html, /graph_latest\.json/);
  assert.doesNotMatch(html, /role="alert"/);
});
