#!/usr/bin/env node
/**
 * emit-entity-data.ts — publish a graph export as the profile dataset.
 *
 *   npm run emit:entities -- --input graph-export.json
 *   curl …/graph/overview | npm run emit:entities -- --input -
 *
 * The Next build reads `apps/web/public/data/entities.json` and pre-renders one
 * page per public entity. This script is the only writer of that file: it
 * applies the publication rules (public node types, no private/noindex records,
 * safe slugs, identifiers only) through the same TypeScript module the page
 * uses, so a URL in the sitemap is always a URL in the build.
 *
 * Without `entities.json` the build falls back to the committed demo dataset.
 * An empty publication is therefore an error, not a silent unpublishing.
 */

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { MAX_ENTITY_PAGES, normalizeEntityDataset } from '../apps/web/lib/entity-seo';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function argument(name: string): string {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] ?? '' : '';
}

const input = argument('--input');
const output = path.resolve(ROOT, argument('--out') || 'apps/web/public/data/entities.json');
const limit = Math.max(1, Math.min(Number(argument('--limit')) || MAX_ENTITY_PAGES, MAX_ENTITY_PAGES));
const allowEmpty = process.argv.includes('--allow-empty');

if (!input) {
  console.error('emit-entities: pass --input <graph.json|-> (a pipeline or Worker graph export).');
  process.exit(1);
}

let raw: unknown;
try {
  const text = input === '-' ? readFileSync(0, 'utf8') : readFileSync(path.resolve(ROOT, input), 'utf8');
  raw = JSON.parse(text);
} catch (error) {
  console.error(`emit-entities: could not read ${input} (${error instanceof Error ? error.message : error}).`);
  process.exit(1);
}

const source = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
const nodes = Array.isArray(source.nodes) ? source.nodes : Array.isArray(source.entities) ? source.entities : [];
const dataset = normalizeEntityDataset(raw, { limit });

if (dataset.entities.length === 0 && !allowEmpty) {
  console.error(
    'emit-entities: the export contains no publishable entity. Refusing to unpublish every profile; '
    + 'use --allow-empty if this is intentional.',
  );
  process.exit(1);
}

// `related` is recomputed from edges on every read. Store the edges that
// produced it so a reload of entities.json yields identical pages instead of
// dropping every relationship.
const edges = new Map<string, { source: string; target: string; predicate: string }>();
for (const entity of dataset.entities) {
  for (const related of entity.related) {
    if (related.direction !== 'out') continue;
    edges.set(`${entity.id}|${related.id}|${related.predicate}`, {
      source: entity.id,
      target: related.id,
      predicate: related.predicate,
    });
  }
}

const relativeOutput = path.relative(process.cwd(), output);
const shownOutput = relativeOutput.startsWith('..') ? output : relativeOutput;

const publication = {
  generatedAt: dataset.generatedAt,
  demo: false,
  entities: dataset.entities,
  edges: [...edges.values()].sort((left, right) =>
    `${left.source}|${left.target}|${left.predicate}`.localeCompare(`${right.source}|${right.target}|${right.predicate}`),
  ),
};

mkdirSync(path.dirname(output), { recursive: true });
writeFileSync(output, `${JSON.stringify(publication, null, 2)}\n`, 'utf8');
console.log(
  `emit-entities: ${nodes.length} input node(s) → ${dataset.entities.length} public profile(s) `
  + `(${Math.max(0, nodes.length - dataset.entities.length)} not published) → ${shownOutput}`,
);
