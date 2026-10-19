/**
 * Build-time access to the public entity dataset.
 *
 * Cloudflare Pages serves a static export, so a profile page exists only if it
 * was rendered during `next build`. There is no request-time database: the
 * pipeline publishes `graph_latest.json`, the build turns it into pages, and the
 * sitemap lists exactly those pages. A missing export falls back to the
 * committed demo dataset so a fresh clone still builds a coherent site.
 * Historical entities.json imports remain a secondary compatibility fallback.
 */

import { existsSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import {
  MAX_ENTITY_PAGES,
  normalizeEntityDataset,
  type EntityDataset,
  type PublicEntity,
} from './entity-seo';

export interface EntityDatasetSource {
  dataset: EntityDataset;
  /** `export` for a real pipeline publication, `sample` for the demo file. */
  source: 'export' | 'sample' | 'empty';
  file: string;
}

const EMPTY: EntityDataset = { generatedAt: new Date().toISOString().slice(0, 10), demo: true, entities: [] };

function dataDirectory(directory?: string): string {
  // `next build` runs with cwd = apps/web. Tests pass an explicit directory.
  return directory ?? path.join(process.cwd(), 'public', 'data');
}

export function datasetCandidates(directory?: string): string[] {
  const base = dataDirectory(directory);
  return [path.join(base, 'graph_latest.json'), path.join(base, 'entities.json'), path.join(base, 'entities.sample.json')];
}

let cache: { key: string; value: EntityDatasetSource } | null = null;

export function loadEntityDataset(directory?: string): EntityDatasetSource {
  for (const file of datasetCandidates(directory)) {
    if (!existsSync(file)) continue;
    const key = `${file}:${statSync(file).mtimeMs}`;
    if (cache?.key === key) return cache.value;
    const raw: unknown = JSON.parse(readFileSync(file, 'utf8'));
    const source = file.endsWith('entities.sample.json') ? 'sample' : 'export';
    const value: EntityDatasetSource = {
      dataset: normalizeEntityDataset(raw, { limit: MAX_ENTITY_PAGES }),
      source,
      file,
    };
    cache = { key, value };
    return value;
  }
  return { dataset: EMPTY, source: 'empty', file: '' };
}

export function listPublicEntities(directory?: string): PublicEntity[] {
  return loadEntityDataset(directory).dataset.entities.filter((entity) => entity.public);
}

export function findEntityBySlug(slug: string, directory?: string): PublicEntity | undefined {
  if (!/^[a-z0-9][a-z0-9-]{0,80}$/.test(slug)) return undefined;
  return listPublicEntities(directory).find((entity) => entity.slug === slug);
}

/** Test helper: bypass the shared cache when a fixture changes between tests. */
export function resetEntityCache(): void {
  cache = null;
}
