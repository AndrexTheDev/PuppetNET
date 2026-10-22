/**
 * Public entity pages: one contract shared by the build, the sitemap and the UI.
 *
 * Everything in this module is pure. The filesystem lives in
 * `entity-store.ts`, so the same code decides a profile URL in a Node build,
 * a Playwright-free unit test and the dashboard's "Profil" link.
 *
 * The dataset is deliberately a *publication* format, not a graph dump:
 * `entity` fields are reduced to facts the pipeline can evidence, `Document`
 * nodes stay out unless an operator marks them public, and accusation-style
 * analytical values (risk score, puppet-master rank) are never published.
 * The deterministic *structural* metrics from Wave 1 (degree, PageRank,
 * community label, mention dates) are published as position hints — the same
 * values the dashboard shows, framed as structure, not as verdicts.
 */

import type { Metadata } from 'next';

/** T types that get a crawlable profile by default. Documents and unknown
 *  nodes need an explicit `public: true`: their labels are often raw text. */
export const PUBLIC_ENTITY_TYPES = ['Person', 'Organization', 'Location', 'Craft'] as const;
export type PublicEntityType = (typeof PUBLIC_ENTITY_TYPES)[number];
export type EntityKind = PublicEntityType | 'Document' | 'Other';

export const ENTITY_TYPE_LABELS: Record<EntityKind, string> = {
  Person: 'Person',
  Organization: 'Organisation',
  Location: 'Ort',
  Craft: 'Fahrzeug oder Luftfahrzeug',
  Document: 'Dokument',
  Other: 'Sonstiges',
};

/** Cloudflare Pages builds are time-boxed; 5 000 profiles is ~0.4 s of export. */
export const MAX_ENTITY_PAGES = 5_000;
export const MAX_RELATED_ENTITIES = 12;
export const MAX_EVIDENCE_REFERENCES = 20;
export const DEFAULT_SITE_ORIGIN = 'https://puppetnet-web.pages.dev';

export interface EntityEvidence {
  id: string;
  title: string;
  url?: string;
  source?: string;
  date?: string;
}

export interface EntityIdentifier {
  /** Schema.org `propertyID`, e.g. `LEI` or `Wikidata`. */
  property: string;
  value: string;
}

export interface RelatedEntity {
  id: string;
  slug: string;
  name: string;
  type: EntityKind;
  predicate: string;
  direction: 'in' | 'out';
  /** Wave 3: structural metrics of the neighbour, when the revision has them. */
  degree?: number;
  rank?: number;
  community?: string;
}

/** Deterministic structural metrics from the export analytics (Wave 1):
 *  degree, PageRank, community label and first/last mention dates. They are
 *  position hints for link analysis — never verdicts — and are only present
 *  when the publication carried valid values. */
export interface EntityAnalytics {
  degree: number;
  rank: number;
  community?: string;
  firstSeen?: string;
  lastSeen?: string;
}

export interface PublicEntity {
  id: string;
  slug: string;
  name: string;
  type: EntityKind;
  description: string;
  /** Optional curated English text (`description_en`); see englishDescription(). */
  descriptionEn?: string;
  aliases: string[];
  identifiers: EntityIdentifier[];
  jurisdiction: string;
  address: string;
  confidence: number;
  updatedAt: string;
  evidence: EntityEvidence[];
  related: RelatedEntity[];
  analytics?: EntityAnalytics;
  sameAs: string[];
  /** False only when the source explicitly opts the page out of indexing. */
  public: boolean;
}

export interface EntityDataset {
  generatedAt: string;
  demo: boolean;
  entities: PublicEntity[];
}

/* ------------------------------------------------------------------------- */
/* Small, dependency-free helpers                                            */
/* ------------------------------------------------------------------------- */

const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};

const text = (value: unknown, max = 500): string =>
  typeof value === 'string' || typeof value === 'number'
    ? String(value).replace(/[\u0000-\u001f\u007f]+/g, ' ').replace(/\s+/g, ' ').trim().slice(0, max)
    : '';

const list = (value: unknown, max = 50): unknown[] => (Array.isArray(value) ? value.slice(0, max) : []);

const number = (value: unknown, fallback = 0): number => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(0, Math.min(1, parsed)) : fallback;
};

export function safeHttpUrl(value: unknown): string | undefined {
  try {
    const url = new URL(String(value));
    if (!['https:', 'http:'].includes(url.protocol)) return undefined;
    if (url.username || url.password) return undefined;
    return url.href;
  } catch {
    return undefined;
  }
}

/** FNV-1a, 32 bit — the same cheap hash the dashboard uses for node colours. */
export function stableHash(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash = Math.imul(hash ^ value.charCodeAt(index), 16777619);
  }
  return hash >>> 0;
}

/** ASCII, lowercase, dash-separated. Non-Latin names collapse to an empty base
 *  and keep their uniqueness in the hash suffix, never in invented Latin text. */
export function slugifyEntityName(name: string): string {
  const base = String(name)
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/ß/g, 'ss')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60)
    .replace(/-+$/g, '');
  return base || 'entity';
}

/** `/entity/<name>-<hash>/`. The hash is over the canonical key, so renaming an
 *  entity does not create a second URL and two identical names never collide. */
export function entitySlug(id: string, name: string): string {
  return `${slugifyEntityName(name)}-${stableHash(id).toString(36).padStart(7, '0')}`;
}

export function entityPath(slug: string): string {
  return `/entity/${slug}/`;
}

export function isPublicEntityType(kind: EntityKind): boolean {
  return (PUBLIC_ENTITY_TYPES as readonly string[]).includes(kind);
}

export function canonicalEntityPath(entity: Pick<PublicEntity, 'slug'>): string {
  return entityPath(entity.slug);
}

/** Absolute origin for JSON-LD, canonicals and the sitemap. The same variable
 *  drives `metadataBase` in the root layout and `emit-seo-files.mjs`. */
export function siteOrigin(): string {
  const raw = process.env.SITE_ORIGIN || process.env.NEXT_PUBLIC_SITE_ORIGIN || DEFAULT_SITE_ORIGIN;
  try {
    const url = new URL(raw);
    if (!['https:', 'http:'].includes(url.protocol) || url.pathname !== '/' || url.search || url.hash) {
      return DEFAULT_SITE_ORIGIN;
    }
    return url.origin;
  } catch {
    return DEFAULT_SITE_ORIGIN;
  }
}

export function schemaTypeFor(kind: EntityKind): string {
  switch (kind) {
    case 'Person':
      return 'Person';
    case 'Organization':
      return 'Organization';
    case 'Location':
      return 'Place';
    case 'Craft':
      return 'Vehicle';
    case 'Document':
      return 'CreativeWork';
    default:
      return 'Thing';
  }
}

export function ogImageFor(kind: EntityKind): string {
  const name = kind.toLowerCase();
  return ['person', 'organization', 'location', 'craft', 'document'].includes(name)
    ? `/og/${name}.png`
    : '/og/site.png';
}

/* ------------------------------------------------------------------------- */
/* Normalisation                                                             */
/* ------------------------------------------------------------------------- */

const IDENTIFIER_FIELDS: ReadonlyArray<readonly [string, string]> = [
  ['wikidata_id', 'Wikidata'],
  ['lei', 'LEI'],
  ['imo', 'IMO'],
  ['mmsi', 'MMSI'],
  ['tail_number', 'Kennzeichen'],
  ['reg_number', 'Handelsregister'],
  ['company_number', 'Firmennummer'],
  ['icao24', 'ICAO 24-bit'],
];

function entityKind(value: unknown): EntityKind {
  const label = text(value, 40).toLowerCase();
  if (['per', 'person', 'individual'].includes(label)) return 'Person';
  if (['org', 'organization', 'organisation', 'company', 'foundation', 'shellcompany'].includes(label)) return 'Organization';
  if (['loc', 'gpe', 'location', 'place'].includes(label)) return 'Location';
  if (['craft', 'aircraft', 'vessel', 'vehicle', 'ship', 'plane'].includes(label)) return 'Craft';
  if (label === 'document') return 'Document';
  return 'Other';
}

function isPublicRecord(raw: Record<string, unknown>, kind: EntityKind): boolean {
  const explicit = raw.public ?? raw.is_public ?? raw.visibility;
  if (explicit === false || explicit === 'private' || explicit === 'unlisted' || explicit === 'internal') return false;
  if (raw.noindex === true || raw.indexable === false) return false;
  if (raw.robots === 'noindex' || raw.robots === 'noindex, nofollow') return false;
  if (explicit === true || explicit === 'public' || explicit === 'published') return true;
  return (PUBLIC_ENTITY_TYPES as readonly string[]).includes(kind);
}

function normalizeAliases(raw: Record<string, unknown>): string[] {
  const values = [...list(raw.aliases, 20), ...list(raw.alternate_names, 20)];
  const seen = new Set<string>();
  const aliases: string[] = [];
  for (const value of values) {
    const alias = text(value, 120);
    const key = alias.toLowerCase();
    if (!alias || seen.has(key)) continue;
    seen.add(key);
    aliases.push(alias);
  }
  return aliases.slice(0, 12);
}

function normalizeIdentifiers(raw: Record<string, unknown>): EntityIdentifier[] {
  const nested = record(raw.identifiers ?? raw.ids ?? raw.props ?? raw.properties);
  const identifiers: EntityIdentifier[] = [];
  for (const [field, property] of IDENTIFIER_FIELDS) {
    const value = text(raw[field] ?? nested[field], 60);
    // Identifier values are printed, not evaluated: restrict them to the
    // characters real registers use so a hostile field cannot smuggle markup.
    if (/^[A-Za-z0-9][A-Za-z0-9 ._:/–-]{0,59}$/.test(value)) identifiers.push({ property, value });
  }
  return identifiers.slice(0, 8);
}

function normalizeEvidence(value: unknown, fallbackTitle = 'Dokument'): EntityEvidence[] {
  const seen = new Set<string>();
  const evidence: EntityEvidence[] = [];
  for (const item of list(value, MAX_EVIDENCE_REFERENCES)) {
    const raw = record(item);
    const id = text(raw.id ?? raw.doc_id ?? raw.key, 160);
    if (!id || seen.has(id)) continue;
    seen.add(id);
    evidence.push({
      id,
      title: text(raw.title ?? raw.name ?? raw.label, 200) || fallbackTitle,
      ...(safeHttpUrl(raw.url) ? { url: safeHttpUrl(raw.url) } : {}),
      ...(text(raw.source ?? raw.source_name ?? raw.source_id, 120)
        ? { source: text(raw.source ?? raw.source_name ?? raw.source_id, 120) }
        : {}),
      ...(text(raw.date ?? raw.published_at ?? raw.fetched_at, 40)
        ? { date: text(raw.date ?? raw.published_at ?? raw.fetched_at, 40) }
        : {}),
    });
  }
  return evidence;
}

function normalizeDate(value: unknown, fallback: string): string {
  const candidate = text(value, 40);
  if (/^\d{4}-\d{2}-\d{2}/.test(candidate)) return candidate.slice(0, 10);
  const parsed = Date.parse(candidate);
  return Number.isFinite(parsed) ? new Date(parsed).toISOString().slice(0, 10) : fallback;
}

const isoDay = (value: unknown): string | undefined => {
  const candidate = text(value, 10);
  return /^\d{4}-\d{2}-\d{2}$/.test(candidate) ? candidate : undefined;
};

/** Wave 3: accept only export analytics that are already valid — bounded
 *  integer degree, rank in [0,1], machine community labels, ISO days.
 *  Anything else is dropped entirely; invalid metrics are never clamped
 *  into looking real. */
function entityAnalytics(raw: Record<string, unknown>): EntityAnalytics | undefined {
  if (typeof raw.degree === 'boolean' || typeof raw.rank === 'boolean') return undefined;
  const degree = Number(raw.degree);
  const rank = Number(raw.rank);
  if (!Number.isInteger(degree) || degree < 0 || degree > 1_000_000) return undefined;
  if (!Number.isFinite(rank) || rank < 0 || rank > 1) return undefined;
  const analytics: EntityAnalytics = { degree, rank: Math.round(rank * 1e6) / 1e6 };
  const community = text(raw.community, 24);
  if (/^[a-z]\d{3,8}$/.test(community)) analytics.community = community;
  const firstSeen = isoDay(raw.firstSeen);
  const lastSeen = isoDay(raw.lastSeen);
  if (firstSeen) analytics.firstSeen = firstSeen;
  if (lastSeen) analytics.lastSeen = lastSeen;
  return analytics;
}

function defaultDescription(
  entity: Pick<PublicEntity, 'name' | 'type' | 'jurisdiction' | 'confidence' | 'evidence'>,
): string {
  const parts = [`${entity.name} ist im PuppetNET-Graphen als ${ENTITY_TYPE_LABELS[entity.type]} erfasst.`];
  if (entity.jurisdiction) parts.push(`Zuständigkeit: ${entity.jurisdiction}.`);
  if (entity.evidence.length) {
    parts.push(`${entity.evidence.length} Quellenreferenz${entity.evidence.length === 1 ? '' : 'en'} im geladenen Datensatz.`);
  }
  parts.push(`Confidence ${Math.round(entity.confidence * 100)} %.`);
  parts.push('Berechnete Beziehungen sind keine Tatsachenbehauptungen.');
  return parts.join(' ');
}

const ENTITY_TYPE_LABELS_EN: Record<EntityKind, string> = {
  Person: 'a person',
  Organization: 'an organization',
  Location: 'a place',
  Craft: 'a vehicle or aircraft',
  Document: 'a document',
  Other: 'an entity of another type',
};

function defaultDescriptionEn(
  entity: Pick<PublicEntity, 'name' | 'type' | 'jurisdiction' | 'confidence' | 'evidence'>,
): string {
  const parts = [`${entity.name} is recorded in the PuppetNET graph as ${ENTITY_TYPE_LABELS_EN[entity.type]}.`];
  if (entity.jurisdiction) parts.push(`Jurisdiction: ${entity.jurisdiction}.`);
  if (entity.evidence.length) {
    parts.push(`${entity.evidence.length} source reference${entity.evidence.length === 1 ? '' : 's'} in the loaded dataset.`);
  }
  parts.push(`Confidence ${Math.round(entity.confidence * 100)} %.`);
  parts.push('Calculated relationships are not statements of fact.');
  return parts.join(' ');
}

/**
 * Text for English surfaces (/en/). A curated `description_en` wins; otherwise
 * the factual summary is generated in English. The German `description` is
 * never shown there: real publications rarely carry one, so the German
 * generated default would otherwise leak onto the English page.
 */
export function englishDescription(
  entity: Pick<PublicEntity, 'name' | 'type' | 'jurisdiction' | 'confidence' | 'evidence' | 'descriptionEn'>,
): string {
  return entity.descriptionEn || defaultDescriptionEn(entity);
}

interface EdgeRecord {
  source: string;
  target: string;
  predicate: string;
}

function normalizeEdges(value: unknown): EdgeRecord[] {
  const edges: EdgeRecord[] = [];
  for (const item of list(value, 200_000)) {
    const raw = record(item);
    const source = text(raw.source ?? raw.from ?? raw.key, 200);
    const target = text(raw.target ?? raw.to, 200);
    if (!source || !target) continue;
    const predicate = text(raw.predicate ?? raw.type ?? raw.relation, 60).toUpperCase();
    edges.push({ source, target, predicate: /^[A-Z][A-Z0-9_]{0,59}$/.test(predicate) ? predicate : 'RELATED_TO' });
  }
  return edges;
}

/**
 * Accepts either the publication format (`{ entities: [...] }`) or the graph
 * export the pipeline and the dashboard already speak (`{ nodes, edges }`).
 * Unknown or unpublishable records are dropped, never guessed at.
 */
export function normalizeEntityDataset(
  input: unknown,
  options: { limit?: number } = {},
): EntityDataset {
  const raw = record(input);
  const limit = Math.max(1, Math.min(Number(options.limit) || MAX_ENTITY_PAGES, MAX_ENTITY_PAGES));
  const generatedAt = normalizeDate(raw.generatedAt ?? raw.generated_at, new Date().toISOString().slice(0, 10));
  const demo = raw.demo === true;
  const rawEntities = list(raw.entities, 100_000);
  const nodes = rawEntities.length ? rawEntities : list(raw.nodes, 100_000);
  const edges = normalizeEdges(raw.edges);
  const globalEvidence = normalizeEvidence(raw.evidence ?? raw.citations, 'Dokument');
  const evidenceById = new Map(globalEvidence.map((item) => [item.id, item]));

  const byId = new Map<string, PublicEntity>();
  for (const item of nodes) {
    const source = record(item);
    const attribute = record(source.attributes);
    const nested = record(source.properties ?? source.props);
    const merged = { ...nested, ...attribute, ...source };
    const id = text(merged.key ?? merged.id ?? merged.canonical_key ?? merged.canonicalKey, 200);
    const name = text(merged.name ?? merged.label ?? merged.title, 200);
    const kind = entityKind(merged.entity_type ?? merged.type ?? merged.kind);
    if (!id || !name || !isPublicRecord(merged, kind)) continue;
    if (byId.has(id)) continue;

    const localEvidence = normalizeEvidence(
      merged.evidence ?? merged.citations ?? merged.documents,
      text(merged.title ?? merged.label, 200) || 'Dokument',
    );
    const docIds = list(merged.evidenceIds ?? merged.evidence_ids ?? merged.doc_ids, 50)
      .map((value) => text(value, 200))
      .filter(Boolean);
    const evidence = [...localEvidence];
    for (const docId of docIds) {
      const known = evidenceById.get(docId);
      if (known && !evidence.some((item) => item.id === docId)) evidence.push(known);
    }

    const identifiers = normalizeIdentifiers(merged);
    const jurisdiction = text(merged.jurisdiction ?? merged.country ?? merged.address_country, 120);
    const address = text(merged.address ?? merged.street_address, 240);
    const aliases = normalizeAliases(merged);
    const analytics = entityAnalytics(merged);
    const sameAs = [...new Set(list(merged.sameAs ?? merged.same_as, 10).map(safeHttpUrl).filter((url): url is string => Boolean(url)))];
    const entity: PublicEntity = {
      id,
      slug: entitySlug(id, name),
      name,
      type: kind,
      description: '',
      aliases,
      identifiers,
      jurisdiction,
      address,
      confidence: number(merged.confidence),
      updatedAt: normalizeDate(merged.updated_at ?? merged.updatedAt ?? merged.last_seen ?? merged.first_seen, generatedAt),
      evidence: evidence.slice(0, MAX_EVIDENCE_REFERENCES),
      related: [],
      ...(analytics ? { analytics } : {}),
      sameAs,
      public: true,
    };
    entity.description =
      text(merged.description ?? merged.summary, 320) || defaultDescription(entity);
    const descriptionEn = text(merged.description_en ?? merged.descriptionEn, 320);
    if (descriptionEn) entity.descriptionEn = descriptionEn;
    byId.set(id, entity);
  }

  const entities = [...byId.values()];
  const publicIds = new Set(entities.map((entity) => entity.id));
  const bySlug = new Map<string, PublicEntity>();
  for (const entity of entities) {
    // Slug collisions are possible after NFKD (two non-Latin names share the
    // "entity-" base). The hash makes them rare; the suffix makes them unique.
    let slug = entity.slug;
    for (let suffix = 2; bySlug.has(slug); suffix += 1) slug = `${entity.slug}-${suffix}`;
    entity.slug = slug;
    bySlug.set(slug, entity);
  }

  const related = new Map<string, RelatedEntity[]>();
  for (const edge of edges) {
    if (edge.source === edge.target) continue;
    if (!publicIds.has(edge.source) || !publicIds.has(edge.target)) continue;
    const source = byId.get(edge.source);
    const target = byId.get(edge.target);
    if (!source || !target) continue;
    for (const [entity, other, direction] of [
      [source, target, 'out'] as const,
      [target, source, 'in'] as const,
    ]) {
      const bucket = related.get(entity.id) ?? [];
      bucket.push({
        id: other.id,
        slug: other.slug,
        name: other.name,
        type: other.type,
        predicate: edge.predicate,
        direction,
        ...(other.analytics
          ? { degree: other.analytics.degree, rank: other.analytics.rank,
              ...(other.analytics.community ? { community: other.analytics.community } : {}) }
          : {}),
      });
      related.set(entity.id, bucket);
    }
  }
  for (const entity of entities) {
    const seen = new Set<string>();
    entity.related = (related.get(entity.id) ?? [])
      .filter((item) => {
        const key = `${item.id}:${item.predicate}:${item.direction}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .sort((left, right) => left.predicate.localeCompare(right.predicate) || left.name.localeCompare(right.name))
      .slice(0, MAX_RELATED_ENTITIES);
  }

  // Keep the strongest profiles when the dataset exceeds the page budget:
  // evidence first, then confidence, then the stable key.
  entities.sort(
    (left, right) =>
      right.evidence.length - left.evidence.length ||
      right.confidence - left.confidence ||
      left.id.localeCompare(right.id),
  );
  const selected = entities.slice(0, limit).sort((left, right) => left.id.localeCompare(right.id));
  // A relation to a profile that was dropped by the page budget would be a
  // dead link in static HTML. Only published neighbours survive the cut.
  const published = new Set(selected.map((entity) => entity.id));
  for (const entity of selected) entity.related = entity.related.filter((item) => published.has(item.id));
  return { generatedAt, demo, entities: selected };
}

/* ------------------------------------------------------------------------- */
/* JSON-LD                                                                   */
/* ------------------------------------------------------------------------- */

/**
 * JSON-LD is injected with `dangerouslySetInnerHTML`, so every `<`, `>` and `&`
 * is escaped. A document title containing `</script>` then stays text instead
 * of closing the tag.
 */
export function serializeJsonLd(value: unknown): string {
  return JSON.stringify(value)
    .replace(/</g, '\\u003c')
    .replace(/>/g, '\\u003e')
    .replace(/&/g, '\\u0026')
    .replace(/\u2028/g, '\\u2028')
    .replace(/\u2029/g, '\\u2029');
}

export function buildWebsiteJsonLd(origin: string): Record<string, unknown> {
  const base = origin.replace(/\/+$/, '');
  return {
    '@type': 'WebSite',
    '@id': `${base}/#website`,
    name: 'PuppetNET',
    url: `${base}/`,
    inLanguage: 'de',
    description: 'Open-Source-OSINT-Netzwerkanalyse mit nachvollziehbaren Quellen.',
  };
}

export function buildEntityJsonLd(entity: PublicEntity, origin: string): Record<string, unknown> {
  const base = origin.replace(/\/+$/, '');
  const url = `${base}${canonicalEntityPath(entity)}`;
  const website = buildWebsiteJsonLd(base);
  const identifier = entity.identifiers.map((item) => ({
    '@type': 'PropertyValue',
    propertyID: item.property,
    value: item.value,
  }));
  const address = entity.address || entity.jurisdiction
    ? {
        '@type': 'PostalAddress',
        ...(entity.address ? { streetAddress: entity.address } : {}),
        ...(entity.jurisdiction ? { addressCountry: entity.jurisdiction } : {}),
      }
    : undefined;
  // Wave 3: structural metrics as PropertyValue pairs, sources as subjectOf
  // CreativeWorks, person-to-person neighbours as `knows` references. All stay
  // inside the subject node — the @graph keeps its pinned website+subject+
  // breadcrumb shape.
  const metrics = entity.analytics
    ? [
        { '@type': 'PropertyValue', name: 'degree', value: entity.analytics.degree },
        { '@type': 'PropertyValue', name: 'PageRank', value: entity.analytics.rank },
        ...(entity.analytics.community ? [{ '@type': 'PropertyValue', name: 'community', value: entity.analytics.community }] : []),
        ...(entity.analytics.firstSeen ? [{ '@type': 'PropertyValue', name: 'firstMention', value: entity.analytics.firstSeen }] : []),
        ...(entity.analytics.lastSeen ? [{ '@type': 'PropertyValue', name: 'lastMention', value: entity.analytics.lastSeen }] : []),
      ]
    : [];
  const knownPersons = entity.type === 'Person'
    ? [...new Map(
        entity.related
          .filter((item) => item.type === 'Person')
          .map((item) => [item.id, { '@type': 'Person' as const, '@id': `${base}${entityPath(item.slug)}#entity` }]),
      ).values()]
    : [];
  const subjectOf = entity.evidence.slice(0, 10).map((item) => ({
    '@type': 'CreativeWork',
    name: item.title,
    ...(item.url ? { url: item.url } : {}),
    ...(item.date && /^\d{4}-\d{2}-\d{2}/.test(item.date) ? { datePublished: item.date.slice(0, 10) } : {}),
  }));
  const subject: Record<string, unknown> = {
    '@type': schemaTypeFor(entity.type),
    '@id': `${url}#entity`,
    name: entity.name,
    url,
    description: entity.description,
    inLanguage: 'de',
    isPartOf: { '@id': `${base}/#website` },
    mainEntityOfPage: url,
    dateModified: entity.updatedAt,
    ...(entity.aliases.length ? { alternateName: entity.aliases } : {}),
    ...(identifier.length ? { identifier } : {}),
    ...(address ? { address } : {}),
    ...(entity.sameAs.length ? { sameAs: entity.sameAs } : {}),
    ...(metrics.length ? { additionalProperty: metrics } : {}),
    ...(subjectOf.length ? { subjectOf } : {}),
    ...(knownPersons.length ? { knows: knownPersons } : {}),
  };
  const breadcrumb = {
    '@type': 'BreadcrumbList',
    itemListElement: [
      { '@type': 'ListItem', position: 1, name: 'Startseite', item: `${base}/` },
      { '@type': 'ListItem', position: 2, name: 'Netzwerkanalyse', item: `${base}/dashboard/` },
      { '@type': 'ListItem', position: 3, name: entity.name, item: url },
    ],
  };
  return { '@context': 'https://schema.org', '@graph': [website, subject, breadcrumb] };
}

/* ------------------------------------------------------------------------- */
/* Next.js metadata                                                          */
/* ------------------------------------------------------------------------- */

export function buildEntityMetadata(entity: PublicEntity): Metadata {
  const path = canonicalEntityPath(entity);
  const typeLabel = ENTITY_TYPE_LABELS[entity.type];
  const title = `${entity.name} — ${typeLabel}`;
  const image = {
    url: ogImageFor(entity.type),
    width: 1200,
    height: 630,
    alt: `${entity.name}, ${typeLabel} im PuppetNET-Graphen`,
  };
  return {
    title,
    description: entity.description,
    keywords: [entity.name, ...entity.aliases, typeLabel, 'PuppetNET', 'OSINT'],
    alternates: { canonical: path },
    robots: entity.public
      ? { index: true, follow: true, 'max-image-preview': 'large', 'max-snippet': -1 }
      : { index: false, follow: false },
    openGraph: {
      type: entity.type === 'Person' ? 'profile' : 'website',
      locale: 'de_DE',
      siteName: 'PuppetNET',
      url: path,
      title,
      description: entity.description,
      images: [image],
    },
    twitter: {
      card: 'summary_large_image',
      title,
      description: entity.description,
      images: [image.url],
    },
  };
}
