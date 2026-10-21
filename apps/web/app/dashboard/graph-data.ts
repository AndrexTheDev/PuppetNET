import palette from '../../src/styles/palette.json';
/** Shared, deterministic data contract. Executed by the data/layout worker, also unit-tested offline. */
export const TYPES = ['Person', 'Organization', 'Location', 'Craft', 'Document', 'Other'] as const;
export type EntityType = typeof TYPES[number];
export const COLORS: Record<EntityType, string> = { Person: palette.green, Organization: palette.gold, Location: palette['green-muted'], Craft: palette.danger, Document: palette['gold-muted'], Other: palette.muted };
export interface Evidence { id: string; title: string; url?: string; source?: string; date?: string }
/** Wave-1 export analytics (deterministic, precomputed at publication time). */
export interface GraphAnalytics {
  version: number; nodes: number; edges: number; communities: number; largestCommunity: number;
  density: number; hubs: { id: string; name: string; rank: number; degree: number }[];
  temporalRange?: { first: string; last: string };
}
/** Wave-4 correlation insights: bounded deterministic rule hits from the export
 *  (nominee chains, star structures, community bridges, shared officers).
 *  Structural hints with edge-id evidence, never accusations. */
export interface GraphInsight {
  rule: string; version: number; title: string; description: string; support: number;
  entities: { id: string; name: string; type?: string }[];
  evidence: string[];
}
export interface GraphNode {
  id: string; label: string; type: EntityType; confidence: number; x: number; y: number;
  cluster: string; lat?: number; lon?: number; evidenceIds: string[];
  count?: number; examples?: string[]; internalEdges?: number; group?: boolean;
  degree?: number; rank?: number; community?: string; firstSeen?: string; lastSeen?: string;
}
export interface GraphEdge { id: string; source: string; target: string; type: string; confidence: number; weight: number; evidenceIds: string[]; count?: number; types?: string[] }
export interface Dataset { nodes: GraphNode[]; edges: GraphEdge[]; evidence: Evidence[]; truncated?: boolean; analytics?: GraphAnalytics; insights?: GraphInsight[] }
export interface Filters { confidence: number; weight: number; types: EntityType[]; lod: boolean; ratio: number; community?: string; from?: string; to?: string; bundle?: boolean; sources?: SourceFilter; outlet?: string; fresh?: FreshWindow; dispute?: boolean; weak?: boolean }
export interface Vulnerability {
  cutVertices: { id: string; label: string; degree: number; impact: { components: number; largest: number } }[];
  cutEdges: number;
  maxCore: number;
  maxCoreSize: number;
}
/** Wave 17: provenance coverage of the visible slice — the methodology rule
 * "high-confidence links need two independent sources" made checkable. */
export interface ProvenanceGaps {
  nodes: number; nodesBacked: number;
  edges: number; edgesBacked: number;
  /** High-confidence relationships documented by a single source. */
  singleSource: { id: string; source: string; target: string; type: string; confidence: number; evidence: number }[];
  /** Entities without any backing document. */
  unbacked: { id: string; label: string; type: EntityType }[];
  singleSourceCount: number;
  unbackedNodeCount: number;
}
/** Wave 10: one hop of a link chain with the documented window of the
 * relationship(s) connecting its two nodes. `ordered` is false when the hop is
 * documented entirely before the previous hop began — an anachronism that needs
 * an explanation instead of a hunch. */
export interface PathTimelineHop {
  from: string; to: string; type: string;
  first?: string; last?: string;
  evidence: number;
  ordered: boolean;
}
export interface PathTimeline { hops: PathTimelineHop[]; first?: string; last?: string; anachronisms: number }
export interface MapPoint { id: string; lat: number; lon: number; count: number; label: string; exampleId: string }
export interface View {
  version: number; nodes: GraphNode[]; edges: GraphEdge[]; mapPoints: MapPoint[];
  stats: { totalNodes: number; totalEdges: number; filteredNodes: number; filteredEdges: number; locatedNodes: number; grouped: boolean; level: string; focused: boolean; truncated: boolean };
  analytics?: GraphAnalytics;
  insights?: GraphInsight[];
  /** Wave 7: first mentions per month in the filtered slice (deterministic buckets). */
  timeline?: { month: string; count: number }[];
  /** Wave 8: entity-resolution hints — same normalized label, different ids. */
  duplicates?: { label: string; ids: string[]; sameType: boolean }[];
  /** Wave 9: articulation points, bridges and the k-core nucleus. */
  vulnerability?: Vulnerability;
  /** Wave 17: provenance coverage and the gaps behind the visible claims. */
  gaps?: ProvenanceGaps;
  /** Wave 18: documents that back several visible entities at once. */
  sharedSources?: SharedSourceGroup[];
  /** Wave 20: which outlets carry the visible slice and how much of it. */
  sourceRegistry?: SourceRegistryEntry[];
  /** Wave 21: same pair, different relationship types — conflict or change over time. */
  conflicts?: PairConflict[];
  /** Wave 21: how current the documented evidence behind the slice is. */
  freshness?: Freshness;
  /** Wave 23: which claims are thin and/or old — the follow-up list. */
  risk?: RiskReport;
}
export const LIMITS = { nodes: 200_000, edges: 500_000, bytes: 64 * 1024 * 1024 };
const record = (v: unknown): Record<string, unknown> => v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {};
const text = (v: unknown, max = 500) => typeof v === 'string' || typeof v === 'number' ? String(v).slice(0, max) : '';
const score = (v: unknown) => v === null || v === '' || !Number.isFinite(Number(v)) ? 0 : Math.max(0, Math.min(1, Number(v)));
const ids = (v: unknown) => Array.isArray(v) ? v.slice(0, 200).map(x => text(x)).filter(Boolean) : [];
const isoDate = (v: unknown): string | undefined => { const s = text(v, 40); return /^\d{4}-\d{2}-\d{2}/.test(s) ? s.slice(0, 10) : undefined; };
const optInt = (v: unknown): number | undefined => v === null || v === '' || v === undefined ? undefined : Number.isFinite(Number(v)) && Number(v) >= 0 ? Math.trunc(Number(v)) : undefined;
export function hash(value: string): number { let n = 2166136261; for (let i = 0; i < value.length; i++) n = Math.imul(n ^ value.charCodeAt(i), 16777619); return n >>> 0; }
/** Deterministic community colour: stable hue per community id, no palette file needed. */
export function communityColor(community: string): string { return `hsl(${hash(community) % 360} 58% 58%)`; }
/** Validates the optional Wave-1 `analytics` block; anything malformed is dropped, never trusted. */
export function normalizeAnalytics(value: unknown): GraphAnalytics | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const a = record(value);
  const num = (v: unknown) => Number.isFinite(Number(v)) && Number(v) >= 0 ? Math.trunc(Number(v)) : 0;
  const hubs = (Array.isArray(a.hubs) ? a.hubs : []).slice(0, 10).map(item => {
    const h = record(item);
    return { id: text(h.id, 200), name: text(h.name, 200), rank: score(h.rank), degree: num(h.degree) };
  }).filter(h => h.id);
  const out: GraphAnalytics = { version: num(a.version) || 1, nodes: num(a.nodes), edges: num(a.edges),
    communities: num(a.communities), largestCommunity: num(a.largestCommunity), density: score(a.density), hubs };
  const range = record(a.temporalRange), first = isoDate(range.first), last = isoDate(range.last);
  if (first && last) out.temporalRange = { first, last };
  return out;
}
/** Validates the optional Wave-4 `insights` block; malformed items are dropped. */
export function normalizeInsights(value: unknown): GraphInsight[] {
  if (!Array.isArray(value)) return [];
  const out: GraphInsight[] = [];
  for (const item of value.slice(0, 24)) {
    const i = record(item);
    const rule = text(i.rule, 40), title = text(i.title, 120), description = text(i.description, 400);
    if (!rule || !title) continue;
    const entities = (Array.isArray(i.entities) ? i.entities : []).slice(0, 5).map(raw => {
      const e = record(raw);
      return { id: text(e.id, 200), name: text(e.name, 200), ...(text(e.type, 40) ? { type: text(e.type, 40) } : {}) };
    }).filter(e => e.id && e.name);
    out.push({ rule, version: Number.isFinite(Number(i.version)) ? Math.trunc(Number(i.version)) : 1,
      title, description, support: score(i.support), entities,
      evidence: (Array.isArray(i.evidence) ? i.evidence : []).slice(0, 4).map(x => text(x, 64)).filter(Boolean) });
  }
  return out;
}
export function safeUrl(value: unknown): string | undefined {
  try { const url = new URL(String(value)); return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined; } catch { return undefined; }
}
function entityType(value: unknown): EntityType {
  const label = text(value).toLowerCase();
  if (['per', 'person'].includes(label)) return 'Person';
  if (['org', 'organization', 'organisation', 'company', 'foundation', 'shellcompany'].includes(label)) return 'Organization';
  if (['loc', 'gpe', 'location'].includes(label)) return 'Location';
  if (['craft', 'aircraft', 'vessel', 'vehicle'].includes(label)) return 'Craft';
  if (label === 'document') return 'Document';
  return 'Other';
}
export function normalizeEvidence(value: unknown): Evidence[] {
  return (Array.isArray(value) ? value : []).slice(0, 20_000).map((item, i) => {
    const d = record(item);
    return { id: text(d.id ?? d.doc_id ?? `doc:${i}`), title: text(d.title ?? d.name ?? 'Dokument'), url: safeUrl(d.url), source: text(d.source ?? d.source_name ?? d.source_id), date: text(d.date ?? d.published_at) };
  });
}
export function normalizeDataset(input: unknown): Dataset {
  const raw = record(input);
  if (!Array.isArray(raw.nodes) || !Array.isArray(raw.edges)) throw new Error('JSON benötigt nodes[] und edges[].');
  if (raw.nodes.length > LIMITS.nodes || raw.edges.length > LIMITS.edges) throw new Error('Importlimit: 200.000 Knoten / 500.000 Kanten. Bitte einen Teilgraphen exportieren.');
  const seen = new Set<string>();
  const evidence = normalizeEvidence(raw.evidence ?? raw.citations);
  const nodes = raw.nodes.map((item, index): GraphNode => {
    const n = record(item), attr = { ...record(n.attributes), ...n }, props = record(attr.props ?? attr.properties);
    const id = text(attr.key ?? attr.id);
    if (!id || seen.has(id)) throw new Error(`Fehlende oder doppelte Knoten-ID bei Index ${index}.`);
    seen.add(id);
    const seed = hash(id), angle = (seed % 6283) / 1000, radius = Math.sqrt(index + 1) * 3;
    const lat = attr.lat ?? attr.latitude ?? props.lat ?? props.latitude;
    const lon = attr.lon ?? attr.lng ?? attr.longitude ?? props.lon ?? props.lng ?? props.longitude;
    const latitude = lat !== null && lat !== '' && lat !== undefined ? Number(lat) : NaN;
    const longitude = lon !== null && lon !== '' && lon !== undefined ? Number(lon) : NaN;
    const hasGeo = Number.isFinite(latitude) && Number.isFinite(longitude) && Math.abs(latitude) <= 90 && Math.abs(longitude) <= 180;
    const type = entityType(attr.entity_type ?? attr.type ?? (Array.isArray(attr.labels) ? attr.labels.find(label => entityType(label) !== 'Other') : 'Other'));
    const embedded = normalizeEvidence(attr.citations);
    evidence.push(...embedded);
    if (type === 'Document') {
      evidence.push({ id, title: text(attr.title ?? attr.label ?? attr.name ?? id), url: safeUrl(attr.url ?? props.url), source: text(attr.source_name ?? attr.source_id), date: text(attr.published_at) });
    }
    return { id, label: text(attr.label ?? attr.name ?? id), type,
      confidence: score(attr.confidence), x: Number.isFinite(attr.x) ? Number(attr.x) : Math.cos(angle) * radius,
      y: Number.isFinite(attr.y) ? Number(attr.y) : Math.sin(angle) * radius, cluster: text(attr.cluster_id ?? attr.cluster ?? props.cluster_id ?? 'unassigned'),
      ...(hasGeo ? { lat: latitude, lon: longitude } : {}), evidenceIds: [...ids(attr.evidenceIds ?? attr.doc_ids), ...embedded.map(d => d.id)],
      ...(optInt(attr.degree) !== undefined ? { degree: optInt(attr.degree) } : {}),
      ...(Number.isFinite(Number(attr.rank)) && attr.rank !== null && attr.rank !== '' ? { rank: score(attr.rank) } : {}),
      ...(text(attr.community, 24) ? { community: text(attr.community, 24) } : {}),
      ...(isoDate(attr.firstSeen) ? { firstSeen: isoDate(attr.firstSeen) } : {}),
      ...(isoDate(attr.lastSeen) ? { lastSeen: isoDate(attr.lastSeen) } : {}) };
  });
  let dangling = 0;
  const edges = raw.edges.map((item, i): GraphEdge | null => {
    const e = record(item), attr = { ...record(e.attributes), ...e }, source = text(attr.source), target = text(attr.target);
    if (!seen.has(source) || !seen.has(target)) { dangling++; return null; }
    return { id: `${text(attr.id ?? attr.key ?? 'edge')}:${i}`, source, target, type: text(attr.type ?? 'RELATED_TO', 80), confidence: score(attr.confidence), weight: score(attr.weight), evidenceIds: [...ids(attr.evidenceIds ?? attr.doc_ids), ...(attr.doc_id ? [text(attr.doc_id)] : [])] };
  }).filter((e): e is GraphEdge => e !== null);
  if (dangling) throw new Error(`${dangling} Kanten verweisen auf fehlende Knoten. Bitte einen geschlossenen Teilgraphen importieren.`);
  const analytics = normalizeAnalytics(raw.analytics);
  const insights = normalizeInsights(raw.insights);
  return { nodes, edges, evidence, truncated: raw.truncated === true, ...(analytics ? { analytics } : {}),
    ...(insights.length ? { insights } : {}) };
}
/** Wave 20: one outlet in the source registry. `name` is the published source
 * name; unnamed documents collect under '' and are reported as such instead of
 * being dropped — an untraceable document is a fact worth seeing. */
export interface SourceRegistryEntry {
  name: string;
  documents: number;
  entities: number;
  edges: number;
  first?: string;
  last?: string;
}

/**
 * Wave 20: which outlets carry the visible slice. Aggregates the documents
 * behind the visible entities and relationships per source name (case-folded,
 * trimmed): how many documents, entities and relationships each outlet supports
 * and the documented window it spans. Deterministic (documents desc, then
 * name), bounded. This is the "who is saying this" view that per-node evidence
 * lists cannot give.
 */
export function sourceRegistry(data: Dataset, nodes: GraphNode[], edges: GraphEdge[], limit = 12): SourceRegistryEntry[] {
  const documentById = new Map(data.evidence.map(doc => [doc.id, doc]));
  const uses = new Map<string, { entities: Set<string>; edges: Set<string> }>();
  const use = (docId: string) => {
    let entry = uses.get(docId);
    if (!entry) { entry = { entities: new Set(), edges: new Set() }; uses.set(docId, entry); }
    return entry;
  };
  const visible = new Set(nodes.filter(node => !node.group).map(node => node.id));
  for (const node of nodes) {
    if (node.group) continue;
    for (const docId of new Set(node.evidenceIds)) if (docId) use(docId).entities.add(node.id);
  }
  for (const edge of edges) {
    if (!visible.has(edge.source) || !visible.has(edge.target)) continue;
    for (const docId of new Set(edge.evidenceIds)) if (docId) use(docId).edges.add(edge.id);
  }
  // Documents are visited in id order, so the displayed spelling of a source is
  // the first published name in a stable order — never a side effect of data order.
  const entries = new Map<string, { name: string; docs: Set<string>; entities: Set<string>; edges: Set<string>; first?: string; last?: string }>();
  for (const docId of [...uses.keys()].sort()) {
    const doc = documentById.get(docId), used = uses.get(docId)!;
    const published = (doc?.source ?? '').trim();
    const key = published.toLowerCase();
    let entry = entries.get(key);
    if (!entry) { entry = { name: published, docs: new Set(), entities: new Set(), edges: new Set() }; entries.set(key, entry); }
    entry.docs.add(docId);
    for (const id of used.entities) entry.entities.add(id);
    for (const id of used.edges) entry.edges.add(id);
    const day = isoDate(doc?.date);
    if (day) {
      if (!entry.first || day < entry.first) entry.first = day;
      if (!entry.last || day > entry.last) entry.last = day;
    }
  }
  return [...entries.values()]
    .map(entry => ({
      name: entry.name,
      documents: entry.docs.size,
      entities: entry.entities.size,
      edges: entry.edges.size,
      ...(entry.first ? { first: entry.first } : {}),
      ...(entry.last ? { last: entry.last } : {}),
    }))
    .sort((a, b) => b.documents - a.documents || b.entities - a.entities
      || (a.name.toLowerCase() < b.name.toLowerCase() ? -1 : a.name.toLowerCase() > b.name.toLowerCase() ? 1 : 0))
    .slice(0, Math.max(1, limit));
}

/** Wave 19: work on claims that stand up. `corroborated` keeps relationships
 * with at least two distinct named sources, `thin` keeps the rest — the edges
 * that still need a second source. Entities are never dropped by this filter:
 * it narrows *claims*, not people. */
export type SourceFilter = 'any' | 'corroborated' | 'thin';
const SOURCE_FILTERS: SourceFilter[] = ['corroborated', 'thin'];
export function edgeSourceStatus(edge: GraphEdge, evidence: Evidence[]): SourceIndependence {
  const documents = new Set(edge.evidenceIds);
  return edgeSources(evidence.filter(doc => documents.has(doc.id))).independence;
}

export function filterDataset(data: Dataset, filters: Filters): Dataset {
  const types = new Set(filters.types);
  let nodes = data.nodes.filter(n => types.has(n.type) && n.confidence >= filters.confidence);
  if (filters.community) nodes = nodes.filter(n => n.community === filters.community);
  const from = isoDate(filters.from), to = isoDate(filters.to);
  if (from || to) {
    // Time window over published evidence spans; undated nodes drop out (honest
    // absence, no guessing). ISO dates compare lexicographically.
    nodes = nodes.filter(n => {
      const start = n.firstSeen, end = n.lastSeen ?? n.firstSeen;
      if (!start) return false;
      if (from && (end ?? start) < from) return false;
      if (to && start > to) return false;
      return true;
    });
  }
  const keep = new Set(nodes.map(n => n.id));
  let edges = data.edges.filter(e => e.confidence >= filters.confidence && e.weight >= filters.weight && keep.has(e.source) && keep.has(e.target));
  if (filters.sources === 'corroborated') edges = edges.filter(e => edgeSourceStatus(e, data.evidence) === 'independent');
  else if (filters.sources === 'thin') edges = edges.filter(e => edgeSourceStatus(e, data.evidence) !== 'independent');
  // Wave 20: evidence-scoped view — "what does this outlet report?". Unlike the
  // claim filter this one is meant to narrow entities too, because the question
  // is about one source's coverage; it is opt-in, named in the UI and cleared
  // with one click.
  const outlet = (filters.outlet ?? '').trim().toLowerCase().slice(0, 80);
  if (outlet) {
    const documents = new Set(data.evidence.filter(doc => (doc.source ?? '').trim().toLowerCase() === outlet).map(doc => doc.id));
    edges = edges.filter(e => e.evidenceIds.some(docId => documents.has(docId)));
    const touched = new Set<string>();
    for (const node of nodes) if (node.evidenceIds.some(docId => documents.has(docId))) touched.add(node.id);
    for (const edge of edges) { touched.add(edge.source); touched.add(edge.target); }
    nodes = nodes.filter(node => touched.has(node.id));
    const allowed = new Set(nodes.map(node => node.id));
    edges = edges.filter(edge => allowed.has(edge.source) && allowed.has(edge.target));
  }
  // Wave 22: dispute view — only the pairs whose claims genuinely clash. The
  // conflict set is derived from the edges that survived every other filter, so
  // switching the view on and off can never feed back into itself.
  if (filters.dispute) {
    const disputed = new Set(pairConflicts(nodes, edges, data.evidence, Number.POSITIVE_INFINITY)
      .filter(entry => entry.kind === 'conflict')
      .map(entry => `${entry.pair[0]}|${entry.pair[1]}`));
    edges = edges.filter(edge => disputed.has(edge.source <= edge.target ? `${edge.source}|${edge.target}` : `${edge.target}|${edge.source}`));
    const touched = new Set<string>();
    for (const edge of edges) { touched.add(edge.source); touched.add(edge.target); }
    nodes = nodes.filter(node => touched.has(node.id));
  }
  // Wave 23: thin-file view — only entities that rest on at most one independent
  // source. Deliberately date-free: the filter must be reproducible without a
  // clock, so it narrows the dimension that is fixable (add a second source), and
  // the panel keeps the two-dimensional judgment (thin *and* old) for reading.
  if (filters.weak) {
    const backing = nodeBacking(nodes, edges, data.evidence);
    nodes = nodes.filter(node => (backing.get(node.id)?.sources ?? 0) <= 1);
    const allowed = new Set(nodes.map(node => node.id));
    edges = edges.filter(edge => allowed.has(edge.source) && allowed.has(edge.target));
  }
  return { nodes, edges, evidence: data.evidence, ...(data.analytics ? { analytics: data.analytics } : {}),
    ...(data.insights ? { insights: data.insights } : {}) };
}
export function levelFor(count: number, filters: Filters): string {
  if (!filters.lod || count <= 5000) return 'Einzelknoten';
  return filters.ratio >= 0.55 ? 'Übersicht' : filters.ratio >= 0.18 ? 'Gruppen' : 'Feingruppen';
}
export function groupDataset(data: Dataset, filters: Filters): { nodes: GraphNode[]; edges: GraphEdge[]; members: Map<string, string> } {
  const level = levelFor(data.nodes.length, filters), members = new Map<string, string>();
  if (level === 'Einzelknoten') return { nodes: data.nodes, edges: data.edges, members };
  const groups = new Map<string, GraphNode>(), groupEdges = new Map<string, GraphEdge>();
  const bins = level === 'Übersicht' ? 16 : level === 'Gruppen' ? 32 : 64;
  const subdivisions = level === 'Übersicht' ? 1 : level === 'Gruppen' ? 4 : 8;
  for (const n of data.nodes) {
    const key = `group:${n.type}:${hash(n.cluster) % bins}:${hash(n.id) % subdivisions}`;
    members.set(n.id, key);
    let group = groups.get(key);
    if (!group) { group = { ...n, id: key, label: '', x: 0, y: 0, lat: undefined, lon: undefined, count: 0, confidence: 0, evidenceIds: [], examples: [], internalEdges: 0, group: true }; groups.set(key, group); }
    group.count!++; group.x += n.x; group.y += n.y; group.confidence += n.confidence;
    if (group.examples!.length < 5) group.examples!.push(n.id);
  }
  for (const g of groups.values()) { g.x /= g.count!; g.y /= g.count!; g.confidence /= g.count!; g.label = `${g.type} · ${g.count!.toLocaleString('de-DE')}`; }
  for (const e of data.edges) {
    const source = members.get(e.source)!, target = members.get(e.target)!;
    if (source === target) { groups.get(source)!.internalEdges!++; continue; }
    const id = JSON.stringify([source, target]);
    const previous = groupEdges.get(id);
    if (previous) { previous.count!++; previous.weight += e.weight; previous.confidence += e.confidence; }
    else groupEdges.set(id, { ...e, id, source, target, type: 'AGGREGATED', count: 1, evidenceIds: [] });
  }
  for (const e of groupEdges.values()) { e.weight /= e.count!; e.confidence /= e.count!; }
  return { nodes: [...groups.values()], edges: [...groupEdges.values()], members };
}
export function geographicPoints(nodes: GraphNode[]): { points: MapPoint[]; count: number } {
  const located = nodes.filter(n => Number.isFinite(n.lat) && Number.isFinite(n.lon));
  let step = 0.25;
  for (;;) {
    const bins = new Map<string, MapPoint>();
    for (const n of located) {
      const key = `${Math.floor(n.lat! / step)}:${Math.floor(n.lon! / step)}`;
      let bin = bins.get(key);
      if (!bin) { bin = { id: key, lat: 0, lon: 0, count: 0, label: n.label, exampleId: n.id }; bins.set(key, bin); }
      bin.lat += n.lat!; bin.lon += n.lon!; bin.count++;
    }
    if (bins.size <= 1000) {
      for (const b of bins.values()) { b.lat /= b.count; b.lon /= b.count; if (b.count > 1) b.label = `${b.count} geographisch gruppierte Knoten`; }
      return { points: [...bins.values()], count: located.length };
    }
    step *= 2;
  }
}
export function evidenceFor(data: Dataset, id: string): Evidence[] {
  const node = data.nodes.find(n => n.id === id);
  const docs = new Set(node?.evidenceIds ?? []);
  for (const edge of data.edges) {
    if (edge.source !== id && edge.target !== id) continue;
    edge.evidenceIds.forEach(doc => docs.add(doc));
    if (edge.type === 'EVIDENCED_BY' && edge.source === id) docs.add(edge.target);
    if (edge.type === 'MENTIONS' && edge.target === id) docs.add(edge.source);
  }
  return [...docs].map(doc => data.evidence.find(e => e.id === doc) ?? { id: doc, title: data.nodes.find(n => n.id === doc)?.label ?? doc });
}
/** Wave 5 edge provenance: the documents behind ONE edge, in publication order.
 *  Unknown references degrade to honest placeholders, never invented titles. */
export function evidenceForEdge(data: Dataset, edgeId: string): Evidence[] {
  const edge = data.edges.find(e => e.id === edgeId);
  if (!edge) return [];
  return edge.evidenceIds.map(doc => data.evidence.find(e => e.id === doc) ?? { id: doc, title: data.nodes.find(n => n.id === doc)?.label ?? doc });
}
export const CYPHER = 'MATCH (n:Entity {canonical_key: $key})\nOPTIONAL MATCH (n)-[:EVIDENCED_BY]->(d:Document)\nOPTIONAL MATCH (m:Document)-[:MENTIONS]->(n)\nRETURN n, collect(DISTINCT d) + collect(DISTINCT m) AS evidence';
export function cypherExport(key: string) { return { query: CYPHER, parameters: { key } }; }
export function demoDataset(count = 900): Dataset {
  count = Math.max(1, Math.min(100_000, count));
  const cities = [[40.4168, -3.7038], [51.5072, -0.1276], [52.52, 13.405], [48.8566, 2.3522], [46.2044, 6.1432], [38.7223, -9.1393]];
  const nodes: GraphNode[] = [], edges: GraphEdge[] = [];
  for (let i = 0; i < count; i++) {
    const type = TYPES[i % 4], cluster = i % 24, angle = (i * 2.39996), r = Math.sqrt(i / 24 + 1) * 0.7;
    const c = cities[Math.floor(i / 3) % cities.length];
    nodes.push({ id: `demo:${i}`, label: `${['Akteur', 'Organisation', 'Standort', 'Flugzeug'][i % 4]} ${String(i + 1).padStart(3, '0')}`, type,
      confidence: 0.3 + (hash(`confidence:${i}`) % 701) / 1000, x: Math.cos(cluster / 24 * Math.PI * 2) * 50 + Math.cos(angle) * r,
      y: Math.sin(cluster / 24 * Math.PI * 2) * 50 + Math.sin(angle) * r, cluster: `demo-community:${cluster}`, evidenceIds: ['demo-document'],
      ...(i % 3 === 0 ? { lat: c[0] + ((i % 37) - 18) * .01, lon: c[1] + ((i % 41) - 20) * .01 } : {}) });
    for (let k = 1; k <= 3; k++) edges.push({ id: `demo-edge:${i}:${k}`, source: `demo:${i}`, target: `demo:${(i + (k === 3 ? 1 : k * 24)) % count}`, type: k === 1 ? 'OWNS' : k === 2 ? 'ASSOCIATED_WITH' : 'MENTIONED_WITH', confidence: .3 + (hash(`${i}:${k}`) % 701) / 1000, weight: k === 1 ? .9 : k === 2 ? .6 : .3, evidenceIds: ['demo-document'] });
  }
  return { nodes, edges, evidence: [{ id: 'demo-document', title: 'Fiktiver Beispieldatensatz – keine realen Rechercheergebnisse', source: 'PuppetNET Demo', date: '2026-10-19' }] };
}

/* ---------------------------------------------------------------------------
 * Wave 2 analysis primitives — pure, deterministic, unit-tested. They run in
 * the browser/worker (free compute); none of them touches the CI budget.
 * ------------------------------------------------------------------------- */

/** Undirected BFS shortest path over a (filtered) dataset, hop-bounded.
 * Deterministic: adjacency lists are sorted, so the same input always yields
 * the same path. Returns null when no path exists within maxHops. */
export function shortestPath(data: Dataset, source: string, target: string, maxHops = 6):
    { nodes: string[]; edges: GraphEdge[] } | null {
  const present = new Set(data.nodes.map(n => n.id));
  if (!present.has(source) || !present.has(target)) return null;
  if (source === target) return { nodes: [source], edges: [] };
  const adjacency = new Map<string, { peer: string; edge: GraphEdge }[]>();
  for (const edge of data.edges) {
    if (!present.has(edge.source) || !present.has(edge.target)) continue;
    for (const [from, to] of [[edge.source, edge.target], [edge.target, edge.source]] as const) {
      const list = adjacency.get(from);
      if (list) list.push({ peer: to, edge }); else adjacency.set(from, [{ peer: to, edge }]);
    }
  }
  for (const list of adjacency.values()) {
    list.sort((a, b) => a.peer < b.peer ? -1 : a.peer > b.peer ? 1 : a.edge.id < b.edge.id ? -1 : 1);
  }
  const previous = new Map<string, { peer: string; edge: GraphEdge }>();
  const seen = new Set<string>([source]);
  let frontier = [source];
  for (let hop = 0; hop < maxHops && frontier.length; hop++) {
    const next: string[] = [];
    for (const id of frontier) {
      for (const step of adjacency.get(id) ?? []) {
        if (seen.has(step.peer)) continue;
        seen.add(step.peer);
        previous.set(step.peer, { peer: id, edge: step.edge });
        if (step.peer === target) {
          const nodes = [target], edges: GraphEdge[] = [];
          let cursor = target;
          while (cursor !== source) {
            const step2 = previous.get(cursor)!;
            edges.unshift(step2.edge);
            nodes.unshift(step2.peer);
            cursor = step2.peer;
          }
          return { nodes, edges };
        }
        next.push(step.peer);
      }
    }
    frontier = next;
  }
  return null;
}

/** Fuzzy relevance: exact substring beats subsequence; earlier and denser wins. */
/** Wave 7 ego expansion: deterministic breadth-first neighborhood around one
 * entity inside the given (already filtered) slice. Depth is clamped to 1–3 —
 * Maltego-style multi-hop expansion, bounded so a hub node cannot swallow the
 * whole graph by accident. Direction is ignored (a connection is a connection);
 * adjacency lists are sorted, so the same slice always yields the same order.
 * Returns the member ids (center first) or null when the center is not in the
 * slice — an honest "not found", never a fabricated neighborhood. */
export const EGO_DEPTH_LIMIT = 3;
export function egoNetwork(data: Dataset, id: string, depth = 2): string[] | null {
  const present = new Set(data.nodes.map(n => n.id));
  if (!present.has(id)) return null;
  const hops = Math.max(1, Math.min(EGO_DEPTH_LIMIT, Math.trunc(Number(depth)) || 1));
  const adjacency = new Map<string, string[]>();
  for (const edge of data.edges) {
    if (!present.has(edge.source) || !present.has(edge.target)) continue;
    for (const [from, to] of [[edge.source, edge.target], [edge.target, edge.source]] as const) {
      const list = adjacency.get(from);
      if (list) list.push(to); else adjacency.set(from, [to]);
    }
  }
  for (const list of adjacency.values()) list.sort();
  const seen = new Set<string>([id]);
  let frontier = [id];
  for (let hop = 0; hop < hops && frontier.length; hop++) {
    const next: string[] = [];
    for (const node of frontier) {
      for (const peer of adjacency.get(node) ?? []) {
        if (seen.has(peer)) continue;
        seen.add(peer);
        next.push(peer);
      }
    }
    frontier = next;
  }
  return [...seen];
}

/** Wave 7 temporal analytics: first mentions per calendar month (UTC bucket of
 * `firstSeen`), ascending, bounded to the most recent `limit` months. Undated
 * entities are honestly absent — no guessing, no smoothing. */
export function mentionTimeline(nodes: GraphNode[], limit = 24): { month: string; count: number }[] {
  const buckets = new Map<string, number>();
  for (const node of nodes) {
    const first = isoDate(node.firstSeen);
    if (!first) continue;
    const month = first.slice(0, 7);
    buckets.set(month, (buckets.get(month) ?? 0) + 1);
  }
  return [...buckets.entries()]
    .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
    .slice(-Math.max(1, limit))
    .map(([month, count]) => ({ month, count }));
}

/** Wave 8 entity-resolution hints: nodes whose normalized labels collide while
 * their ids differ — the classic cross-source duplicate. Conservative by
 * design (case/whitespace folding only, no phonetic guessing): a structural
 * hint for the investigator, never a merge decision. LoD groups are excluded
 * (they are display artifacts, not entities). Deterministic order: larger
 * groups first, then the normalized label; bounded to `limit` groups. */
export function duplicateCandidates(nodes: GraphNode[], limit = 12): { label: string; ids: string[]; sameType: boolean }[] {
  const groups = new Map<string, GraphNode[]>();
  for (const node of nodes) {
    if (node.group) continue;
    const key = node.label.toLowerCase().replace(/\s+/g, ' ').trim();
    if (!key) continue;
    const list = groups.get(key);
    if (list) list.push(node); else groups.set(key, [node]);
  }
  const out: { label: string; ids: string[]; sameType: boolean }[] = [];
  for (const members of groups.values()) {
    if (members.length < 2) continue;
    out.push({
      label: members[0].label,
      ids: members.map(m => m.id),
      sameType: members.every(m => m.type === members[0].type),
    });
  }
  out.sort((a, b) => b.ids.length - a.ids.length || (a.label.toLowerCase() < b.label.toLowerCase() ? -1 : a.label.toLowerCase() > b.label.toLowerCase() ? 1 : 0));
  return out.slice(0, Math.max(1, limit));
}

/** Wave 8: exact UTC calendar window for a `YYYY-MM` histogram bucket — the
 * click-to-filter contract of the timeline. Invalid months are null, never
 * guessed; leap years come from the real calendar. */
export function monthWindow(month: string): { from: string; to: string } | null {
  if (!/^\d{4}-\d{2}$/.test(month)) return null;
  const year = Number(month.slice(0, 4)), index = Number(month.slice(5, 7));
  if (index < 1 || index > 12) return null;
  const lastDay = new Date(Date.UTC(year, index, 0)).getUTCDate();
  return { from: `${month}-01`, to: `${month}-${String(lastDay).padStart(2, '0')}` };
}

/** Wave 13: one side of a duplicate comparison — the facts an investigator
 * weighs before deciding that two node ids are the same entity. */
export interface DuplicateProfile {
  id: string; label: string; type: EntityType;
  degree: number; community?: string; firstSeen?: string; lastSeen?: string;
  evidence: number;
  neighbors: { id: string; label: string }[];
}
export interface DuplicateComparison {
  profiles: DuplicateProfile[];
  /** Neighbours every compared entity shares — the strongest merge signal. */
  shared: { id: string; label: string }[];
  /** Neighbours only this entity has (per id), sorted by label. */
  unique: Record<string, string[]>;
  /** Field names that differ across the compared entities (type, community, …). */
  conflicts: string[];
}

/**
 * Wave 13: side-by-side facts for up to four duplicate candidates. Pure and
 * deterministic; neighbours are read undirected from the same filtered slice the
 * hints came from, and the compared ids are never counted as neighbours of each
 * other (a duplicate is not its own evidence). Fewer than two known ids -> null
 * instead of a one-sided "comparison".
 */
export function compareDuplicates(data: Dataset, ids: string[], neighborLimit = 8): DuplicateComparison | null {
  const known = new Map(data.nodes.map(node => [node.id, node]));
  const wanted = [...new Set(ids)].filter(id => known.has(id)).slice(0, 4);
  if (wanted.length < 2) return null;
  const wantedSet = new Set(wanted);
  const adjacency = new Map<string, Set<string>>();
  for (const id of wanted) adjacency.set(id, new Set());
  for (const edge of data.edges) {
    for (const [from, to] of [[edge.source, edge.target], [edge.target, edge.source]] as const) {
      if (!adjacency.has(from) || from === to || wantedSet.has(to)) continue;
      adjacency.get(from)!.add(to);
    }
  }
  const labelOf = (id: string) => known.get(id)?.label ?? id;
  const profiles: DuplicateProfile[] = wanted.map(id => {
    const node = known.get(id)!;
    const neighbors = [...adjacency.get(id)!]
      .map(peer => ({ id: peer, label: labelOf(peer) }))
      .sort((a, b) => (a.label.toLowerCase() < b.label.toLowerCase() ? -1 : a.label.toLowerCase() > b.label.toLowerCase() ? 1 : a.id < b.id ? -1 : 1));
    return {
      id, label: node.label, type: node.type, degree: neighbors.length,
      community: node.community, firstSeen: node.firstSeen, lastSeen: node.lastSeen,
      evidence: new Set(node.evidenceIds).size,
      neighbors: neighbors.slice(0, Math.max(1, neighborLimit)),
    };
  });
  const sharedIds = profiles.map(profile => new Set(adjacency.get(profile.id)!)).reduce((left, right) => new Set([...left].filter(id => right.has(id))));
  const shared = [...sharedIds].map(id => ({ id, label: labelOf(id) }))
    .sort((a, b) => (a.label.toLowerCase() < b.label.toLowerCase() ? -1 : a.label.toLowerCase() > b.label.toLowerCase() ? 1 : a.id < b.id ? -1 : 1));
  const unique: Record<string, string[]> = {};
  for (const profile of profiles) {
    unique[profile.id] = [...adjacency.get(profile.id)!]
      .filter(peer => !sharedIds.has(peer) && !profiles.some(other => other.id !== profile.id && adjacency.get(other.id)!.has(peer)))
      .map(labelOf).sort();
  }
  const conflicts: string[] = [];
  const differs = (field: (profile: DuplicateProfile) => string | undefined) => {
    const values = new Set(profiles.map(field));
    return values.size > 1;
  };
  if (differs(profile => profile.type)) conflicts.push('type');
  if (differs(profile => profile.community)) conflicts.push('community');
  if (differs(profile => profile.firstSeen)) conflicts.push('firstSeen');
  if (differs(profile => profile.lastSeen)) conflicts.push('lastSeen');
  return { profiles, shared, unique, conflicts };
}

/** Wave 13: failure simulation for several entities at once — how many parts
 * remain, how big the largest survivor is and how many nodes end up isolated.
 * Only real entities count (LoD groups are display artifacts), the selection is
 * bounded and unknown ids are ignored instead of silently shrinking the test. */
export function multiFailureImpact(nodes: GraphNode[], edges: GraphEdge[], ids: string[], maxNodes = 4): { removed: number; components: number; largest: number; isolated: number } | null {
  const singles = nodes.filter(node => !node.group).map(node => node.id).sort();
  const known = new Set(singles);
  const removed = new Set([...new Set(ids)].filter(id => known.has(id)).slice(0, Math.max(1, maxNodes)));
  if (removed.size === 0) return null;
  const index = new Map<string, number>();
  singles.forEach((id, i) => index.set(id, i));
  const adjacency: number[][] = singles.map(() => []);
  const seen = new Set<string>();
  for (const edge of edges) {
    const a = index.get(edge.source), b = index.get(edge.target);
    if (a === undefined || b === undefined || a === b) continue;
    const key = a < b ? `${a}|${b}` : `${b}|${a}`;
    if (seen.has(key)) continue;
    seen.add(key);
    adjacency[a].push(b); adjacency[b].push(a);
  }
  const reached = new Uint8Array(singles.length);
  for (const id of removed) reached[index.get(id)!] = 1;
  let components = 0, largest = 0, isolated = 0;
  for (let start = 0; start < singles.length; start++) {
    if (reached[start]) continue;
    components++;
    let size = 0;
    const stack = [start];
    reached[start] = 1;
    while (stack.length) {
      const v = stack.pop()!;
      size++;
      for (const u of adjacency[v]) if (!reached[u]) { reached[u] = 1; stack.push(u); }
    }
    if (size > largest) largest = size;
    if (size === 1) isolated++;
  }
  return { removed: removed.size, components, largest, isolated };
}

/** Wave 9: bundle parallel relationships of the same unordered pair into one
 * edge — Maltego-style link charts drown in multi-edges otherwise. The bundle
 * keeps the strongest documented confidence and the union of provenance; the
 * count and the sorted type list make the rest visible instead of hiding it.
 * Self-loops stay untouched, the input is never mutated, the order is stable. */
export function bundleEdges(edges: GraphEdge[]): GraphEdge[] {
  const bundles = new Map<string, GraphEdge>();
  for (const edge of edges) {
    const [a, b] = edge.source <= edge.target ? [edge.source, edge.target] : [edge.target, edge.source];
    const key = edge.source === edge.target ? `self:${edge.id}` : `pair:${a}|${b}`;
    const previous = bundles.get(key);
    if (!previous) { bundles.set(key, { ...edge, count: edge.count ?? 1, types: [edge.type] }); continue; }
    const types = [...new Set([...(previous.types ?? [previous.type]), edge.type])].sort();
    const evidenceIds = [...new Set([...previous.evidenceIds, ...edge.evidenceIds])].sort();
    const better = edge.confidence > previous.confidence || (edge.confidence === previous.confidence && edge.id < previous.id);
    const base = better ? edge : previous;
    bundles.set(key, {
      ...base,
      id: base.id, source: a, target: b, type: base.type,
      confidence: Math.max(previous.confidence, edge.confidence),
      weight: Number((previous.weight + edge.weight).toFixed(4)),
      evidenceIds,
      count: (previous.count ?? 1) + (edge.count ?? 1),
      types,
    });
  }
  return [...bundles.values()].sort((x, y) =>
    x.source < y.source ? -1 : x.source > y.source ? 1 :
    x.target < y.target ? -1 : x.target > y.target ? 1 :
    x.id < y.id ? -1 : x.id > y.id ? 1 : 0);
}

export const VULNERABILITY_NODE_LIMIT = 50_000;

/** Wave 9: structural vulnerability of the visible slice — articulation points
 * (removing them splits the network), bridges (edges whose loss disconnects it)
 * and the k-core decomposition (the densest well-connected nucleus). Iterative
 * Tarjan plus bucket peeling keeps 20k-node slices fast and stack-safe; parallel
 * edges count as one relationship (simple-graph semantics) and LoD display
 * groups are ignored. Above the node limit this returns null instead of
 * pretending to know. */
export function structuralVulnerability(nodes: GraphNode[], edges: GraphEdge[], limit = 12, maxNodes = VULNERABILITY_NODE_LIMIT): Vulnerability | null {
  const singles = nodes.filter(n => !n.group).sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const n = singles.length;
  if (n > maxNodes) return null;
  const index = new Map<string, number>();
  singles.forEach((node, i) => index.set(node.id, i));
  const adjacency: number[][] = singles.map(() => []);
  const seen = new Set<string>();
  for (const edge of edges) {
    const a = index.get(edge.source), b = index.get(edge.target);
    if (a === undefined || b === undefined || a === b) continue;
    const key = a < b ? `${a}|${b}` : `${b}|${a}`;
    if (seen.has(key)) continue;
    seen.add(key);
    adjacency[a].push(b); adjacency[b].push(a);
  }
  for (const list of adjacency) list.sort((x, y) => x - y);
  const discovery = new Int32Array(n).fill(-1), low = new Int32Array(n), parent = new Int32Array(n).fill(-1), cut = new Uint8Array(n);
  let cutEdges = 0, time = 0;
  for (let root = 0; root < n; root++) {
    if (discovery[root] !== -1) continue;
    discovery[root] = low[root] = time++;
    let rootChildren = 0;
    const stack: { v: number; next: number }[] = [{ v: root, next: 0 }];
    while (stack.length) {
      const frame = stack[stack.length - 1], v = frame.v;
      if (frame.next < adjacency[v].length) {
        const w = adjacency[v][frame.next++];
        if (w === parent[v]) continue;
        if (discovery[w] === -1) {
          parent[w] = v; discovery[w] = low[w] = time++;
          if (v === root) rootChildren++;
          stack.push({ v: w, next: 0 });
        } else if (discovery[w] < low[v]) low[v] = discovery[w];
      } else {
        stack.pop();
        const p = parent[v];
        if (p === -1) continue;
        if (low[v] < low[p]) low[p] = low[v];
        if (parent[p] !== -1 && low[v] >= discovery[p]) cut[p] = 1;
        if (low[v] > discovery[p]) cutEdges++;
      }
    }
    if (rootChildren > 1) cut[root] = 1;
  }
  // k-core by binary search over k (monotone: a non-empty k-core implies a
  // non-empty (k-1)-core) — O((n+m) log n), far easier to trust than an
  // incremental peeling index, and the slice sizes here are bounded.
  const strength = adjacency.map(list => list.length);
  const maxDegree = strength.reduce((max, d) => Math.max(max, d), 0);
  const survivors = (k: number) => {
    const degree = strength.slice(), removed = new Uint8Array(n), queue: number[] = [];
    for (let v = 0; v < n; v++) if (degree[v] < k) { removed[v] = 1; queue.push(v); }
    let remaining = n;
    for (let qi = 0; qi < queue.length; qi++) {
      const v = queue[qi];
      remaining--;
      for (const u of adjacency[v]) if (!removed[u] && --degree[u] < k) { removed[u] = 1; queue.push(u); }
    }
    return remaining;
  };
  let maxCore = 0, coreLow = 1, coreHigh = maxDegree;
  while (coreLow <= coreHigh) {
    const mid = (coreLow + coreHigh) >> 1;
    if (survivors(mid) > 0) { maxCore = mid; coreLow = mid + 1; } else coreHigh = mid - 1;
  }
  const maxCoreSize = n === 0 ? 0 : survivors(maxCore);
  const cutVertices: { id: string; label: string; degree: number }[] = [];
  for (let v = 0; v < n; v++) if (cut[v]) cutVertices.push({ id: singles[v].id, label: singles[v].label, degree: strength[v] });
  cutVertices.sort((a, b) => b.degree - a.degree || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  // Wave 10: failure simulation — how many parts does the slice fall into when
  // this articulation point disappears, and how big is the largest survivor?
  const componentsWithout = (skip: number) => {
    const reached = new Uint8Array(n);
    reached[skip] = 1;
    let components = 0, largest = 0;
    for (let start = 0; start < n; start++) {
      if (reached[start]) continue;
      components++;
      let size = 0;
      const stack = [start];
      reached[start] = 1;
      while (stack.length) {
        const v = stack.pop()!;
        size++;
        for (const u of adjacency[v]) if (u !== skip && !reached[u]) { reached[u] = 1; stack.push(u); }
      }
      if (size > largest) largest = size;
    }
    return { components, largest };
  };
  return {
    cutVertices: cutVertices.slice(0, Math.max(1, limit)).map(entry => ({ ...entry, impact: componentsWithout(index.get(entry.id)!) })),
    cutEdges, maxCore, maxCoreSize,
  };
}

/**
 * Wave 10: temporal plausibility of a link chain. Each hop carries the
 * documented window of the relationship(s) connecting its two nodes (earliest
 * to latest evidence date, normalized to YYYY-MM-DD). A hop whose latest
 * documentation predates the previous hop's earliest one is an anachronism —
 * the chain cannot have happened in that order without an explanation.
 * Undated hops stay honestly empty and never turn into a verdict; a hop without
 * any connecting edge returns null instead of inventing a relation.
 */
export function pathTimeline(data: Dataset, ids: string[]): PathTimeline | null {
  if (ids.length < 2) return null;
  const dateOf = new Map(data.evidence.map(e => [e.id, isoDate(e.date)]));
  const hops: PathTimelineHop[] = [];
  for (let i = 1; i < ids.length; i++) {
    const from = ids[i - 1], to = ids[i];
    const edges = data.edges.filter(e => (e.source === from && e.target === to) || (e.source === to && e.target === from));
    if (edges.length === 0) return null;
    const days = edges.flatMap(e => e.evidenceIds.map(id => dateOf.get(id))).filter((d): d is string => !!d).sort();
    const first = days[0], last = days[days.length - 1];
    const previous = hops[hops.length - 1];
    const breaks = !!(previous?.first && last && last < previous.first);
    hops.push({
      from, to, type: edges.map(e => e.type).sort()[0],
      first, last, evidence: new Set(edges.flatMap(e => e.evidenceIds)).size,
      ordered: !breaks,
    });
  }
  const starts = hops.map(h => h.first).filter((d): d is string => !!d).sort();
  const ends = hops.map(h => h.last).filter((d): d is string => !!d).sort();
  return { hops, first: starts[0], last: ends[ends.length - 1], anachronisms: hops.filter(h => !h.ordered).length };
}

/** Wave 12: the next month of a cumulative playback. The month list is
 * captured when playback starts (the filtered timeline shrinks while it runs),
 * so this helper only decides the step — out of range returns null and the
 * caller stops instead of looping. */
export function nextPlaybackMonth(months: string[], current: string | undefined, direction: 1 | -1): string | null {
  if (months.length === 0) return null;
  const index = current ? months.indexOf(current) : -1;
  if (index < 0) return direction > 0 ? months[0] : null;
  const next = index + direction;
  return next >= 0 && next < months.length ? months[next] : null;
}

/** Wave 12: documented window of a single relationship — earliest to latest
 * evidence date, normalized to the day. Undated documents are counted as
 * absent, never guessed. */
export function evidenceWindow(evidence: Evidence[]): { first?: string; last?: string; dated: number } {
  const days = evidence.map(doc => isoDate(doc.date)).filter((day): day is string => !!day).sort();
  return { first: days[0], last: days[days.length - 1], dated: days.length };
}

export function fuzzyScore(query: string, value: string): number {
  const q = query.trim().toLowerCase(), t = value.toLowerCase();
  if (!q || !t) return 0;
  const at = t.indexOf(q);
  if (at >= 0) return 2000 - Math.min(at, 999) + (at === 0 ? 500 : 0);
  let total = 0, cursor = 0, streak = 0;
  for (const char of q) {
    const found = t.indexOf(char, cursor);
    if (found < 0) return 0;
    streak = found === cursor ? streak + 1 : 0;
    total += 10 + streak * 5;
    cursor = found + 1;
  }
  return Math.min(total, 999);
}

/** Ranked search over nodes (label and id), stable order, bounded result. */
export function rankSearch(nodes: GraphNode[], query: string, limit = 50): GraphNode[] {
  const q = query.trim();
  if (!q) return nodes.slice(0, limit);
  const scored = nodes
    .map(n => ({ n, s: Math.max(fuzzyScore(q, n.label), fuzzyScore(q, n.id)) }))
    .filter(entry => entry.s > 0);
  scored.sort((a, b) => b.s - a.s || a.n.label.localeCompare(b.n.label) || (a.n.id < b.n.id ? -1 : a.n.id > b.n.id ? 1 : 0));
  return scored.slice(0, limit).map(entry => entry.n);
}

/** Resolves a pathfinder input (exact id/label first, then best fuzzy match). */
export function resolveNodeRef(nodes: GraphNode[], ref: string): GraphNode | null {
  const value = ref.trim();
  if (!value) return null;
  return nodes.find(n => n.id === value || n.label === value) ?? rankSearch(nodes, value, 1)[0] ?? null;
}

/** Wave 14: saved views are case sessions — a name plus the encoded view
 * state (mode, query, focus, community, time window, colour). They live in this
 * browser only, are capped and sanitized on read, and stay ordered by name so
 * the list never jumps around. */
export const SAVED_VIEW_LIMIT = 50;
export const SAVED_VIEW_STORAGE_KEY = 'puppetnet.views.v1';
export interface SavedView { name: string; query: string; saved: string }
export function normalizeSavedName(value: unknown): string {
  if (typeof value !== 'string') return '';
  return value.replace(/[\u0000-\u001f\u007f]/g, '').replace(/\s+/g, ' ').trim().slice(0, 60);
}
export function readSavedViews(raw: unknown): SavedView[] {
  let parsed: unknown = raw;
  if (typeof raw === 'string') {
    try { parsed = JSON.parse(raw); } catch { return []; }
  }
  const list = Array.isArray(parsed) ? parsed : [];
  const out: SavedView[] = [];
  const seen = new Set<string>();
  for (const item of list) {
    if (out.length >= SAVED_VIEW_LIMIT) break;
    const entry = record(item);
    const name = normalizeSavedName(entry.name);
    const query = typeof entry.query === 'string' ? entry.query.slice(0, 600) : '';
    const saved = isoDate(entry.saved);
    if (!name || !query || !saved || seen.has(name.toLowerCase())) continue;
    seen.add(name.toLowerCase());
    out.push({ name, query, saved });
  }
  return out.sort(compareSavedViews);
}
const compareSavedViews = (a: SavedView, b: SavedView) =>
  a.name.toLowerCase() < b.name.toLowerCase() ? -1 : a.name.toLowerCase() > b.name.toLowerCase() ? 1 : a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
/** Saves (or overwrites) a view under its name; the list stays bounded and
 * sorted, and a blank name is a no-op instead of an unnamed entry. */
export function upsertSavedView(list: SavedView[], name: string, query: string, saved: string): SavedView[] {
  const cleanName = normalizeSavedName(name), cleanQuery = query.slice(0, 600), day = isoDate(saved);
  if (!cleanName || !cleanQuery || !day) return list;
  const next = list.filter(entry => entry.name.toLowerCase() !== cleanName.toLowerCase());
  next.push({ name: cleanName, query: cleanQuery, saved: day });
  return next.sort(compareSavedViews).slice(0, SAVED_VIEW_LIMIT);
}
export function removeSavedView(list: SavedView[], name: string): SavedView[] {
  const cleanName = normalizeSavedName(name).toLowerCase();
  return list.filter(entry => entry.name.toLowerCase() !== cleanName);
}
/** Wave 14: provenance export — every document that supports the visible
 * nodes or edges, with the entities it backs. Deterministic, bounded, and
 * honest about documents that no longer resolve (they keep their raw id). */
export function evidenceToCSV(data: Dataset, nodes: GraphNode[], edges: GraphEdge[], limit = 5_000): string {
  const documents = new Map(data.evidence.map(doc => [doc.id, doc]));
  const uses = new Map<string, Set<string>>();
  const labels = new Map(data.nodes.map(node => [node.id, node.label]));
  const add = (docId: string, use: string) => {
    const set = uses.get(docId) ?? new Set<string>();
    set.add(use);
    uses.set(docId, set);
  };
  for (const node of nodes) node.evidenceIds.forEach(docId => add(docId, labels.get(node.id) ?? node.id));
  for (const edge of edges) {
    const from = labels.get(edge.source) ?? edge.source, to = labels.get(edge.target) ?? edge.target;
    edge.evidenceIds.forEach(docId => add(docId, `${from} → ${to}`));
  }
  const header = 'id,title,source,date,url,used_by';
  const rows = [...uses.entries()]
    .map(([docId, usedBy]) => ({ docId, usedBy }))
    .sort((a, b) => (a.docId < b.docId ? -1 : a.docId > b.docId ? 1 : 0))
    .slice(0, Math.max(1, limit))
    .map(({ docId, usedBy }) => {
      const doc = documents.get(docId);
      return [docId, doc?.title, doc?.source, doc?.date, doc?.url, [...usedBy].sort().join(' | ')].map(csvCell).join(',');
    });
  return [header, ...rows].join('\n') + '\n';
}

const csvCell = (value: string | number | undefined): string => {
  const s = value === undefined || value === null ? '' : String(value);
  return /[",\n\r]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
};

/** RFC-4180 CSV of the visible view — client-side reporting without a server. */
/** Wave 11 research notes are case work, not data: they live only in this
 * browser (localStorage), are capped per note and never leave the device unless
 * the investigator exports them on purpose. */
export const NOTE_LIMIT = 2_000;
export const NOTE_STORAGE_KEY = 'puppetnet.notes.v1';
export function normalizeNote(value: unknown): string {
  if (typeof value !== 'string') return '';
  return value.replace(/\r\n?/g, '\n').trim().slice(0, NOTE_LIMIT);
}
/** Sanitizes whatever localStorage handed back: junk, empty values and
 * non-string notes are dropped instead of trusted; the store stays bounded. */
export function readNotes(raw: unknown): Record<string, string> {
  let parsed: unknown = raw;
  if (typeof raw === 'string') {
    try { parsed = JSON.parse(raw); } catch { return {}; }
  }
  const out: Record<string, string> = {};
  let count = 0;
  for (const [key, value] of Object.entries(record(parsed))) {
    if (count >= 500) break;
    const id = text(key, 200), note = normalizeNote(value);
    if (!id || !note) continue;
    out[id] = note; count++;
  }
  return out;
}
/**
 * Wave 15: reads the research notes back out of an exported case report (DE or
 * EN, Wave-11 format). The report is the user's own exchange medium — nothing
 * is uploaded, the file stays on the device. Parsing is deliberately strict:
 * only the notes section counts, entries need the `- **Label** (`id`)` line,
 * the note is the following indented block, and everything is sanitized with
 * the same rules as live notes. A foreign or truncated file yields what it
 * honestly contains instead of throwing.
 */
export function parseCaseNotes(markdown: string): Record<string, string> {
  if (typeof markdown !== 'string' || !markdown) return {};
  const lines = markdown.replace(/\r\n?/g, '\n').split('\n');
  const start = lines.findIndex(line => /^##\s+(Ermittlungsnotizen|Research notes)\s*(?:\(\d+\))?\s*$/.test(line));
  if (start < 0) return {};
  const notes: Record<string, string> = {};
  let count = 0;
  for (let i = start + 1; i < lines.length && count < 500; i++) {
    const line = lines[i];
    if (/^##\s/.test(line)) break;
    const entry = /^-\s+\*\*(.+?)\*\*\s+\(`([^`]+)`\)\s*$/.exec(line);
    if (!entry) continue;
    const id = text(entry[2], 200);
    if (!id) continue;
    const body: string[] = [];
    for (let j = i + 1; j < lines.length; j++) {
      if (/^\s{2,}\S/.test(lines[j])) { body.push(lines[j].replace(/^\s+/, '')); continue; }
      if (lines[j].trim() === '') { if (body.length) body.push(''); continue; }
      break;
    }
    while (body.length && body[body.length - 1] === '') body.pop();
    const note = normalizeNote(body.join('\n'));
    if (!note) continue;
    notes[id] = note;
    count++;
  }
  return notes;
}

/** Wave 15: merges imported notes into the local store without silently
 * overwriting the investigator's own work. Imported notes fill empty slots and
 * are reported as "added"; a differing existing note is kept and reported as
 * "skipped"; identical text counts as "unchanged". Deterministic and pure. */
export function mergeNotes(current: Record<string, string>, imported: Record<string, string>): { notes: Record<string, string>; added: number; unchanged: number; skipped: number } {
  const notes = { ...current };
  let added = 0, unchanged = 0, skipped = 0;
  for (const [rawId, rawNote] of Object.entries(imported)) {
    const id = text(rawId, 200), note = normalizeNote(rawNote);
    if (!id || !note) continue;
    const existing = normalizeNote(notes[id]);
    if (!existing) { notes[id] = note; added++; continue; }
    if (existing === note) { unchanged++; continue; }
    skipped++;
  }
  return { notes, added, unchanged, skipped };
}

/** Wave 16: one note hit in the case-wide note index. */
export interface NoteHit { id: string; label: string; snippet: string; where: 'label' | 'note' | 'both' }

/**
 * Wave 16: searches the research notes, not just the graph. An investigator who
 * recorded twenty observations needs to find "where did I write about X" without
 * clicking every entity: label matches, note-text matches and the lines around
 * them. Empty query lists every annotated entity (sorted by label) so the index
 * doubles as a case overview. Deterministic ranking, bounded output, and notes
 * whose entity vanished keep their raw id instead of disappearing.
 */
export function searchNoteIndex(nodes: GraphNode[], notes: Record<string, string>, query: string, limit = 20): NoteHit[] {
  const labels = new Map(nodes.map(node => [node.id, node.label]));
  const q = query.trim().toLowerCase().slice(0, 120);
  const hits: { hit: NoteHit; rank: number }[] = [];
  for (const [rawId, rawNote] of Object.entries(notes)) {
    const id = text(rawId, 200), note = normalizeNote(rawNote);
    if (!id || !note) continue;
    const label = labels.get(id) ?? id;
    const inLabel = !!q && label.toLowerCase().includes(q);
    const inNote = !!q && note.toLowerCase().includes(q);
    if (q && !inLabel && !inNote) continue;
    const lines = note.split('\n').map(line => line.trim()).filter(Boolean);
    const matchLine = inNote ? lines.find(line => line.toLowerCase().includes(q)) ?? lines[0] : lines[0];
    const snippet = (matchLine ?? '').length > 140 ? `${(matchLine ?? '').slice(0, 139).trimEnd()}…` : matchLine ?? '';
    hits.push({
      hit: { id, label, snippet, where: inLabel && inNote ? 'both' : inLabel ? 'label' : 'note' },
      rank: inLabel && inNote ? 3 : inLabel ? 2 : 1,
    });
  }
  hits.sort((a, b) => b.rank - a.rank
    || (a.hit.label.toLowerCase() < b.hit.label.toLowerCase() ? -1 : a.hit.label.toLowerCase() > b.hit.label.toLowerCase() ? 1
      : a.hit.id < b.hit.id ? -1 : a.hit.id > b.hit.id ? 1 : 0));
  return hits.slice(0, Math.max(1, limit)).map(entry => entry.hit);
}

export interface CaseBundleInput {
  notes: Record<string, string>;
  views: { name: string; query: string; saved: string }[];
  report: string;
  generated: string;
}
/**
 * Wave 16: the whole case in one portable file — notes, saved sessions and the
 * Markdown report, wrapped in a versioned envelope. Deterministic (the caller
 * passes the timestamp, notes are sorted), so the same case produces the same
 * bytes. The bundle is plain JSON: readable in any editor, no server involved.
 */
export function caseBundle(input: CaseBundleInput): string {
  const notes = Object.keys(input.notes).sort().reduce<Record<string, string>>((out, id) => {
    const note = normalizeNote(input.notes[id]);
    if (note) out[id] = note;
    return out;
  }, {});
  const views = [...input.views]
    .map(view => ({ name: normalizeSavedName(view.name), query: view.query.slice(0, 600), saved: isoDate(view.saved) ?? '' }))
    .filter(view => view.name && view.query && view.saved)
    .sort((a, b) => (a.name.toLowerCase() < b.name.toLowerCase() ? -1 : a.name.toLowerCase() > b.name.toLowerCase() ? 1 : 0));
  return JSON.stringify({
    format: 'puppetnet-case',
    version: 1,
    generated: input.generated,
    notes,
    views,
    report: input.report,
  }, null, 2) + '\n';
}

/** Wave 18: how well a relationship is sourced. Two documents from the same
 * outlet are not two independent sources — the methodological difference that a
 * plain document count hides. */
export type SourceIndependence = 'none' | 'single' | 'shared' | 'unknown' | 'independent';
export interface EdgeSources { documents: number; sources: number; independence: SourceIndependence }

/**
 * Wave 18: source verdict for one relationship. Documents are the distinct
 * evidence ids; `sources` counts distinct non-empty source names (case-folded).
 * A document without a source name can never prove independence, so two such
 * documents are 'unknown' instead of 'independent', and a named-but-identical
 * origin is reported as 'shared' instead of pretending to be corroborated.
 */
export function edgeSources(evidence: Evidence[]): EdgeSources {
  const documents = new Set(evidence.map(doc => doc.id).filter(Boolean)).size;
  const sources = new Set(evidence.map(doc => (doc.source ?? '').trim().toLowerCase()).filter(Boolean)).size;
  const independence: SourceIndependence =
    documents === 0 ? 'none'
      : documents === 1 ? 'single'
        : sources >= 2 ? 'independent'
          : sources === 1 ? 'shared' : 'unknown';
  return { documents, sources, independence };
}

export interface SharedSourceGroup {
  document: string;
  title?: string;
  source?: string;
  entities: { id: string; label: string }[];
}

/**
 * Wave 18: documents that back more than one visible entity. A single register
 * entry naming three companies, or one article naming two people, is evidence
 * *about all of them* — a link-analysis signal that edge-based views cannot
 * show. Entity-level only (node evidence), LoD display groups excluded, sorted
 * by group size and then document id, bounded. It is a hint about shared
 * provenance, never a claim of identity.
 */
export function sharedSourceGroups(nodes: GraphNode[], evidence: Evidence[], limit = 8): SharedSourceGroup[] {
  const documents = new Map(evidence.map(doc => [doc.id, doc]));
  const groups = new Map<string, { id: string; label: string }[]>();
  for (const node of nodes) {
    if (node.group) continue;
    for (const document of new Set(node.evidenceIds)) {
      if (!document) continue;
      const list = groups.get(document);
      const entry = { id: node.id, label: node.label };
      if (list) list.push(entry); else groups.set(document, [entry]);
    }
  }
  const out: SharedSourceGroup[] = [];
  for (const [document, entities] of groups) {
    if (entities.length < 2) continue;
    const doc = documents.get(document);
    out.push({
      document,
      ...(doc?.title ? { title: doc.title } : {}),
      ...(doc?.source ? { source: doc.source } : {}),
      entities: entities.sort((a, b) => (a.label.toLowerCase() < b.label.toLowerCase() ? -1 : a.label.toLowerCase() > b.label.toLowerCase() ? 1 : a.id < b.id ? -1 : a.id > b.id ? 1 : 0)),
    });
  }
  out.sort((a, b) => b.entities.length - a.entities.length || (a.document < b.document ? -1 : a.document > b.document ? 1 : 0));
  return out.slice(0, Math.max(1, limit));
}

/** Wave 21: one pair of entities claimed in different ways. `sequence` means the
 * documented windows do not overlap — most likely a change over time, not a
 * contradiction. `conflict` means both claims are documented inside the same
 * period, which needs a decision instead of a guess. `undated` means at least
 * one side has no dates, so nothing can be concluded. */
export type ConflictKind = 'conflict' | 'sequence' | 'undated';
export interface PairConflict {
  pair: [string, string];
  labels: [string, string];
  kind: ConflictKind;
  claims: { type: string; documents: number; sources: number; first?: string; last?: string }[];
}

/**
 * Wave 21: conflicting claims on the same pair. A pair carrying two different
 * relationship types is not automatically a contradiction — an ownership that
 * ended before a new one began is a history, not a dispute. So every type group
 * gets its documented window, and only overlapping windows count as a conflict;
 * undated groups stay 'undated' instead of being guessed either way. Deterministic
 * (conflicts first, then pair key), bounded, and direction-agnostic.
 */
export function pairConflicts(nodes: GraphNode[], edges: GraphEdge[], evidence: Evidence[], limit = 8): PairConflict[] {
  const labels = new Map(nodes.map(node => [node.id, node.label]));
  const dates = new Map(evidence.map(doc => [doc.id, isoDate(doc.date)]));
  const sources = new Map(evidence.map(doc => [doc.id, (doc.source ?? '').trim().toLowerCase()]));
  const groups = new Map<string, Map<string, { documents: Set<string>; sources: Set<string>; first?: string; last?: string }>>();
  for (const edge of edges) {
    if (edge.source === edge.target || edge.type === 'AGGREGATED') continue;
    const [a, b] = edge.source <= edge.target ? [edge.source, edge.target] : [edge.target, edge.source];
    const key = `${a}|${b}`;
    const byType = groups.get(key) ?? new Map();
    groups.set(key, byType);
    const claim = byType.get(edge.type) ?? { documents: new Set<string>(), sources: new Set<string>() };
    byType.set(edge.type, claim);
    for (const docId of edge.evidenceIds) {
      claim.documents.add(docId);
      const source = sources.get(docId);
      if (source) claim.sources.add(source);
      const day = dates.get(docId);
      if (day) {
        if (!claim.first || day < claim.first) claim.first = day;
        if (!claim.last || day > claim.last) claim.last = day;
      }
    }
  }
  const out: PairConflict[] = [];
  for (const [key, byType] of groups) {
    if (byType.size < 2) continue;
    const claims = [...byType.entries()]
      .map(([type, claim]) => ({
        type,
        documents: claim.documents.size,
        sources: claim.sources.size,
        ...(claim.first ? { first: claim.first } : {}),
        ...(claim.last ? { last: claim.last } : {}),
      }))
      .sort((a, b) => (a.type < b.type ? -1 : a.type > b.type ? 1 : 0));
    let kind: ConflictKind = 'undated';
    if (claims.every(claim => claim.first && claim.last)) {
      const overlapping = claims.some((left, i) => claims.slice(i + 1).some(right => left.first! <= right.last! && right.first! <= left.last!));
      kind = overlapping ? 'conflict' : 'sequence';
    }
    const [a, b] = key.split('|');
    out.push({ pair: [a, b], labels: [labels.get(a) ?? a, labels.get(b) ?? b], kind, claims });
  }
  const rank: Record<ConflictKind, number> = { conflict: 0, sequence: 1, undated: 2 };
  out.sort((x, y) => rank[x.kind] - rank[y.kind] || (x.pair[0] < y.pair[0] ? -1 : x.pair[0] > y.pair[0] ? 1 : x.pair[1] < y.pair[1] ? -1 : x.pair[1] > y.pair[1] ? 1 : 0));
  return out.slice(0, Math.max(1, limit));
}

export interface Freshness {
  reference: string;
  months: number;
  /** Entities whose newest document is older than the window. */
  stale: { id: string; label: string; last?: string }[];
  staleCount: number;
  /** Entities with documents that carry no usable date at all. */
  undatedCount: number;
  entities: number;
}

/** Wave 21: subtracts whole months from an ISO day, clamping the day of month
 * (31 Jan minus one month stays valid instead of rolling into March). */
export function monthsBefore(reference: string, months: number): string | null {
  const day = isoDate(reference);
  if (!day) return null;
  const year = Number(day.slice(0, 4)), month = Number(day.slice(5, 7)) - 1, date = Number(day.slice(8, 10));
  const target = new Date(Date.UTC(year, month - Math.max(0, Math.trunc(months)), 1));
  const lastDay = new Date(Date.UTC(target.getUTCFullYear(), target.getUTCMonth() + 1, 0)).getUTCDate();
  const safeDay = Math.min(date, lastDay);
  return `${target.getUTCFullYear()}-${String(target.getUTCMonth() + 1).padStart(2, '0')}-${String(safeDay).padStart(2, '0')}`;
}

/**
 * Wave 21: is the case still current? Counts, per visible entity, the newest
 * document date and compares it with a window counted back from `reference`.
 * Entities without any documented date are reported separately — "unknown" is
 * not "old". The reference date is passed in, so the result is reproducible.
 */
export function evidenceFreshness(nodes: GraphNode[], evidence: Evidence[], reference: string, months = 12, limit = 8): Freshness | null {
  const cutoff = monthsBefore(reference, months), day = isoDate(reference);
  if (!cutoff || !day) return null;
  const dates = new Map(evidence.map(doc => [doc.id, isoDate(doc.date)]));
  const stale: { id: string; label: string; last?: string }[] = [];
  let undatedCount = 0, entities = 0;
  for (const node of nodes) {
    if (node.group) continue;
    entities++;
    let newest: string | undefined;
    for (const docId of node.evidenceIds) {
      const documentDay = dates.get(docId);
      if (documentDay && (!newest || documentDay > newest)) newest = documentDay;
    }
    if (!newest) { undatedCount++; continue; }
    if (newest < cutoff) stale.push({ id: node.id, label: node.label, last: newest });
  }
  stale.sort((a, b) => (a.last! < b.last! ? -1 : a.last! > b.last! ? 1 : a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  return { reference: day, months: Math.max(1, Math.trunc(months)), stale: stale.slice(0, Math.max(1, limit)), staleCount: stale.length, undatedCount, entities };
}

/** Wave 22: the only windows the currency check may use. A closed vocabulary
 * keeps permalinks comparable and makes "stale" mean one defined thing. */
export const FRESH_WINDOWS = [6, 12, 24, 60] as const;
export type FreshWindow = (typeof FRESH_WINDOWS)[number];
export const DEFAULT_FRESH_MONTHS: FreshWindow = 12;
export function isFreshWindow(value: unknown): value is FreshWindow {
  return FRESH_WINDOWS.includes(value as FreshWindow);
}

/** Wave 23: how far a claim can be trusted — two honest dimensions, no invented
 * score. `sources` counts *independent* sources: unnamed documents can back a
 * claim but can never corroborate it (Wave 18's rule), so two unnamed documents
 * still leave an entity on a single source. */
export type RiskState = 'unbacked' | 'weak' | 'thin' | 'stale' | 'undated' | 'solid';
export interface RiskEntry {
  id: string; label: string; type: EntityType;
  documents: number; sources: number; relations: number; last?: string;
  state: RiskState;
}
export interface RiskReport {
  reference: string; months: number; entities: number;
  states: Record<RiskState, number>;
  /** Every entity that is not solid, worst first. */
  entries: RiskEntry[];
  weakCount: number;
}

/** Wave 23: per entity, the documents that mention it — attached directly or via
 * a visible relationship — the distinct *independent* source names behind them
 * (unnamed documents can back a claim but never corroborate it, Wave 18's rule)
 * and the newest usable date. Shared by the panel and the thin-file filter so
 * both always mean the same thing. */
export function nodeBacking(nodes: GraphNode[], edges: GraphEdge[], evidence: Evidence[]): Map<string, { documents: number; sources: number; newest?: string }> {
  const dates = new Map(evidence.map(doc => [doc.id, isoDate(doc.date)]));
  const names = new Map(evidence.map(doc => [doc.id, (doc.source ?? '').trim().toLowerCase()]));
  const documents = new Map<string, Set<string>>();
  const addDoc = (id: string, docId: string) => {
    const set = documents.get(id) ?? new Set<string>();
    set.add(docId);
    documents.set(id, set);
  };
  for (const node of nodes) for (const docId of node.evidenceIds) addDoc(node.id, docId);
  for (const edge of edges) for (const docId of edge.evidenceIds) { addDoc(edge.source, docId); addDoc(edge.target, docId); }
  const backing = new Map<string, { documents: number; sources: number; newest?: string }>();
  for (const node of nodes) {
    const sources = new Set<string>();
    let count = 0, newest: string | undefined;
    for (const docId of documents.get(node.id) ?? []) {
      if (!dates.has(docId) && !names.has(docId)) continue;
      count++;
      const name = names.get(docId);
      if (name) sources.add(name);
      const day = dates.get(docId);
      if (day && (!newest || day > newest)) newest = day;
    }
    backing.set(node.id, { documents: count, sources: sources.size, ...(newest ? { newest } : {}) });
  }
  return backing;
}

/**
 * Wave 23: ranks the file by what it can actually carry. Each visible entity
 * gets its document count, its *independent* source count and its newest date;
 * the combination decides the state — `weak` is thin *and* old (the claim needs
 * a second source and a fresher one), `thin` is thin but current, `stale` is
 * corroborated but old, `undated` is corroborated but undateable, `unbacked` has
 * no document at all. Deterministic, bounded, groups excluded.
 */
export function evidenceRisk(nodes: GraphNode[], edges: GraphEdge[], evidence: Evidence[], reference: string, months = 12, limit = 8): RiskReport | null {
  const cutoff = monthsBefore(reference, months), day = isoDate(reference);
  if (!cutoff || !day) return null;
  const relations = new Map<string, number>();
  for (const edge of edges) {
    if (edge.source === edge.target) continue;
    relations.set(edge.source, (relations.get(edge.source) ?? 0) + 1);
    relations.set(edge.target, (relations.get(edge.target) ?? 0) + 1);
  }
  const backing = nodeBacking(nodes, edges, evidence);
  const states: Record<RiskState, number> = { unbacked: 0, weak: 0, thin: 0, stale: 0, undated: 0, solid: 0 };
  const entries: RiskEntry[] = [];
  let entities = 0;
  for (const node of nodes) {
    if (node.group) continue;
    entities++;
    const own = backing.get(node.id) ?? { documents: 0, sources: 0 };
    const newest = own.newest;
    let state: RiskState;
    if (own.documents === 0) state = 'unbacked';
    else if (own.sources <= 1) state = !newest || newest < cutoff ? 'weak' : 'thin';
    else state = !newest ? 'undated' : newest < cutoff ? 'stale' : 'solid';
    states[state]++;
    entries.push({ id: node.id, label: node.label, type: node.type, documents: own.documents, sources: own.sources, relations: relations.get(node.id) ?? 0, ...(newest ? { last: newest } : {}), state });
  }
  const rank: Record<RiskState, number> = { weak: 0, stale: 1, thin: 2, undated: 3, unbacked: 4, solid: 5 };
  entries.sort((a, b) => rank[a.state] - rank[b.state]
    || a.sources - b.sources
    || (a.last ?? '') .localeCompare(b.last ?? '')
    || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const ranked = entries.filter(entry => entry.state !== 'solid');
  return { reference: day, months: Math.max(1, Math.trunc(months)), entities, states, entries: ranked.slice(0, Math.max(1, limit)), weakCount: ranked.length };
}

/** Wave 23: the weak-file list as RFC-4180 CSV — the part of a case file that
 * needs a follow-up call, ready to hand over. */
export function riskToCSV(report: RiskReport): string {
  const header = ['id', 'label', 'type', 'state', 'documents', 'sources', 'relations', 'last'].map(csvCell).join(',');
  const rows = report.entries.map(entry => [entry.id, entry.label, entry.type, entry.state, entry.documents, entry.sources, entry.relations, entry.last].map(csvCell).join(','));
  return [header, ...rows].join('\n') + '\n';
}

/** Wave 24: what changed between an earlier export and the one on screen.
 * Identity is deliberately conservative: entities by id, relationships by the
 * directed triple `source|type|target`, documents by id — a renamed label or a
 * new telling of the same story is *not* a new relationship. */
export interface DatasetDiff {
  entities: { added: number; removed: number; unchanged: number };
  relations: { added: number; removed: number; unchanged: number };
  documents: { added: number; removed: number; unchanged: number };
  newEntities: { id: string; label: string; type: EntityType }[];
  goneEntities: { id: string; label: string; type: EntityType }[];
  newRelations: { key: string; source: string; target: string; type: string }[];
  goneRelations: { key: string; source: string; target: string; type: string }[];
  newDocuments: { id: string; title?: string; source?: string; date?: string }[];
  /** Pairs that clash now but did not clash in the comparison state. */
  newConflicts: PairConflict[];
  /** Pairs that clashed before and no longer do — possibly resolved, possibly lost. */
  resolvedConflicts: number;
  /** Entities that gained their second independent source since the comparison. */
  corroborated: { id: string; label: string; sources: number }[];
}

const relationKey = (edge: GraphEdge) => `${edge.source}|${edge.type}|${edge.target}`;

/**
 * Wave 24: the diff between a comparison state and the current dataset. Both
 * sides are judged with the same rules the panels use (conflicts, independent
 * sources), so "new contradiction" and "now corroborated" mean exactly what they
 * mean elsewhere. Every list is bounded; the counts are complete.
 */
export function diffDatasets(previous: Dataset, current: Dataset, limit = 6): DatasetDiff {
  const beforeNodes = new Map(previous.nodes.map(node => [node.id, node]));
  const afterNodes = new Map(current.nodes.map(node => [node.id, node]));
  const beforeEdges = new Map(previous.edges.map(edge => [relationKey(edge), edge]));
  const afterEdges = new Map(current.edges.map(edge => [relationKey(edge), edge]));
  const beforeDocs = new Map(previous.evidence.map(doc => [doc.id, doc]));
  const afterDocs = new Map(current.evidence.map(doc => [doc.id, doc]));
  const byId = (a: { id: string }, b: { id: string }) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
  const byKey = (a: { key: string }, b: { key: string }) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0);

  const newEntities = [...afterNodes.values()].filter(node => !beforeNodes.has(node.id))
    .map(node => ({ id: node.id, label: node.label, type: node.type })).sort(byId);
  const goneEntities = [...beforeNodes.values()].filter(node => !afterNodes.has(node.id))
    .map(node => ({ id: node.id, label: node.label, type: node.type })).sort(byId);
  const newRelations = [...afterEdges.entries()].filter(([key]) => !beforeEdges.has(key))
    .map(([key, edge]) => ({ key, source: edge.source, target: edge.target, type: edge.type })).sort(byKey);
  const goneRelations = [...beforeEdges.entries()].filter(([key]) => !afterEdges.has(key))
    .map(([key, edge]) => ({ key, source: edge.source, target: edge.target, type: edge.type })).sort(byKey);
  const newDocuments = [...afterDocs.values()].filter(doc => !beforeDocs.has(doc.id))
    .map(doc => ({ id: doc.id, ...(doc.title ? { title: doc.title } : {}), ...(doc.source ? { source: doc.source } : {}), ...(doc.date ? { date: doc.date } : {}) })).sort(byId);

  const clash = (data: Dataset) => new Map(pairConflicts(data.nodes, data.edges, data.evidence, Number.POSITIVE_INFINITY)
    .filter(entry => entry.kind === 'conflict')
    .map(entry => [entry.pair.join('|'), entry]));
  const beforeClashes = clash(previous), afterClashes = clash(current);
  const newConflicts = [...afterClashes.entries()].filter(([key]) => !beforeClashes.has(key)).sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0)).map(([, entry]) => entry);
  const resolvedConflicts = [...beforeClashes.keys()].filter(key => !afterClashes.has(key)).length;

  const beforeSources = nodeBacking(previous.nodes, previous.edges, previous.evidence);
  const afterSources = nodeBacking(current.nodes, current.edges, current.evidence);
  const corroborated = current.nodes.filter(node => !node.group
    && (beforeSources.get(node.id)?.sources ?? 0) <= 1
    && (afterSources.get(node.id)?.sources ?? 0) >= 2)
    .map(node => ({ id: node.id, label: node.label, sources: afterSources.get(node.id)!.sources })).sort(byId);

  return {
    entities: { added: newEntities.length, removed: goneEntities.length, unchanged: [...afterNodes.keys()].filter(id => beforeNodes.has(id)).length },
    relations: { added: newRelations.length, removed: goneRelations.length, unchanged: [...afterEdges.keys()].filter(key => beforeEdges.has(key)).length },
    documents: { added: newDocuments.length, removed: [...beforeDocs.keys()].filter(id => !afterDocs.has(id)).length, unchanged: [...afterDocs.keys()].filter(id => beforeDocs.has(id)).length },
    newEntities: newEntities.slice(0, limit), goneEntities: goneEntities.slice(0, limit),
    newRelations: newRelations.slice(0, limit), goneRelations: goneRelations.slice(0, limit),
    newDocuments: newDocuments.slice(0, limit),
    newConflicts: newConflicts.slice(0, limit), resolvedConflicts, corroborated: corroborated.slice(0, limit),
  };
}

/** Wave 24: the change list as RFC-4180 CSV — one row per change, ready to mail. */
export function diffToCSV(diff: DatasetDiff): string {
  const rows: (string | number | undefined)[][] = [];
  for (const entry of diff.newEntities) rows.push(['entity', 'added', entry.id, entry.label, entry.type]);
  for (const entry of diff.goneEntities) rows.push(['entity', 'removed', entry.id, entry.label, entry.type]);
  for (const entry of diff.newRelations) rows.push(['relation', 'added', entry.key, '', entry.type]);
  for (const entry of diff.goneRelations) rows.push(['relation', 'removed', entry.key, '', entry.type]);
  for (const entry of diff.newDocuments) rows.push(['document', 'added', entry.id, entry.title ?? '', entry.source ?? '']);
  for (const entry of diff.corroborated) rows.push(['corroboration', 'added', entry.id, entry.label, `${entry.sources} sources`]);
  for (const entry of diff.newConflicts) rows.push(['conflict', 'added', entry.pair.join('|'), entry.labels.join(' ↔ '), entry.kind]);
  return [['kind', 'change', 'ref', 'label', 'detail'].map(csvCell).join(','), ...rows.map(row => row.map(csvCell).join(','))].join('\n') + '\n';
}

export const CONFIDENCE_THRESHOLD = 0.5;

/**
 * Wave 17: where the evidence is thin. Counts the documents behind every visible
 * entity and relationship, lists high-confidence relationships carried by a
 * single source (the methodology rule asks for two independent ones) and the
 * entities without any backing document. LoD display groups are not claims and
 * stay out; the lists are bounded and deterministic, and a slice without gaps
 * still returns honest zero counts instead of null — "nothing missing" is a
 * result, not an absent answer.
 */
export function provenanceGaps(nodes: GraphNode[], edges: GraphEdge[], limit = 8): ProvenanceGaps {
  const singles = nodes.filter(node => !node.group);
  const ids = new Set(singles.map(node => node.id));
  const nodesBacked = singles.filter(node => node.evidenceIds.length > 0).length;
  const visibleEdges = edges.filter(edge => ids.has(edge.source) && ids.has(edge.target) && edge.type !== 'AGGREGATED');
  const edgesBacked = visibleEdges.filter(edge => edge.evidenceIds.length > 0).length;
  const singleSource = visibleEdges
    .filter(edge => edge.evidenceIds.length === 1 && edge.confidence >= CONFIDENCE_THRESHOLD)
    .map(edge => ({ id: edge.id, source: edge.source, target: edge.target, type: edge.type, confidence: edge.confidence, evidence: edge.evidenceIds.length }))
    .sort((a, b) => b.confidence - a.confidence || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const unbacked = singles
    .filter(node => node.evidenceIds.length === 0)
    .map(node => ({ id: node.id, label: node.label, type: node.type }))
    .sort((a, b) => (a.label.toLowerCase() < b.label.toLowerCase() ? -1 : a.label.toLowerCase() > b.label.toLowerCase() ? 1 : a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  return {
    nodes: singles.length, nodesBacked,
    edges: visibleEdges.length, edgesBacked,
    singleSource: singleSource.slice(0, Math.max(1, limit)),
    unbacked: unbacked.slice(0, Math.max(1, limit)),
    singleSourceCount: singleSource.length,
    unbackedNodeCount: unbacked.length,
  };
}

/** Wave 17: reads a case bundle back in (Wave-16 format). Unknown formats,
 * wrong versions and junk are rejected with null instead of partially applying
 * half a case; notes and sessions go through the same sanitizers as the live
 * store. The report is returned as text only — it is never executed. */
export function readCaseBundle(raw: unknown): { notes: Record<string, string>; views: SavedView[]; report: string } | null {
  let parsed: unknown = raw;
  if (typeof raw === 'string') {
    try { parsed = JSON.parse(raw); } catch { return null; }
  }
  const bundle = record(parsed);
  if (bundle.format !== 'puppetnet-case' || Number(bundle.version) !== 1) return null;
  const report = typeof bundle.report === 'string' ? bundle.report.slice(0, 2_000_000) : '';
  return { notes: readNotes(bundle.notes), views: readSavedViews(bundle.views), report };
}

export interface CaseReportInput {
  stats: View['stats'];
  nodes: GraphNode[];
  edges: GraphEdge[];
  analytics?: GraphAnalytics;
  insights?: GraphInsight[];
  timeline?: { month: string; count: number }[];
  duplicates?: { label: string; ids: string[]; sameType: boolean }[];
  vulnerability?: Vulnerability;
  gaps?: ProvenanceGaps;
  sharedSources?: { document: string; entities: { id: string; label: string }[] }[];
  conflicts?: PairConflict[];
  freshness?: Freshness;
  risk?: RiskReport;
  notes?: Record<string, string>;
}
/** Wave 11 case report: a deterministic Markdown file for the case folder —
 * scope, research notes, analytics, rule hits, structural findings, duplicates
 * and the temporal envelope, in the UI language. `generated` is passed in so the
 * report stays reproducible in tests; the closing line keeps hints and proof
 * apart. */
const CASE_REPORT_TEXT = {
  de: {
    title: '# PuppetNET — Fallbericht', generated: 'Erzeugt', scope: 'Ausschnitt', of: 'von',
    level: 'Ansichtsebene', focus: 'Fokus', lod: 'LoD-Gruppen', yes: 'ja', no: 'nein',
    notes: 'Ermittlungsnotizen', noNotes: '_Keine Notizen erfasst._', analytics: 'Analytik',
    communities: 'Communities', density: 'Dichte', range: 'Zeitraum', hubs: 'Hubs',
    structure: 'Struktur', cutEdges: 'Schnittkanten', edges: 'Kanten', kcore: 'max. k-Kern', nodes: 'Knoten',
    provenance: 'Provenienz', backed: 'belegt', unbackedNodes: 'Entitäten ohne Beleg', singleSource: 'Hochsichere Beziehungen mit nur einer Quelle', none: 'keine', sharedDocs: 'Belege, die mehrere Entitäten stützen',
    articulation: 'Artikulationspunkt', degree: 'Grad', splits: 'zerfällt in {parts} Teile, größter {largest}',
    rules: 'Regelhinweise', duplicates: 'Mögliche Dubletten', mixed: 'gemischte Typen',
    timeline: 'Erwähnungen pro Monat',
    conflicts: 'Widersprüchliche Aussagen', conflictsNone: 'keine abweichenden Aussagen', currency: 'Aktualität der Belege',
    risk: 'Belastbarkeit der Akte', riskNone: 'alle Entitäten sind gedeckt', riskStates: 'ohne Beleg / dünn und alt / nur eine Quelle / veraltet / ohne Datum / solide',
    footer: 'Hinweise, keine Beweise: Quellen prüfen; personenbezogene Daten nur mit Rechtsgrundlage verarbeiten.',
  },
  en: {
    title: '# PuppetNET — case report', generated: 'Generated', scope: 'Slice', of: 'of',
    level: 'View level', focus: 'Focus', lod: 'LoD groups', yes: 'yes', no: 'no',
    notes: 'Research notes', noNotes: '_No notes recorded._', analytics: 'Analytics',
    communities: 'Communities', density: 'Density', range: 'Time range', hubs: 'Hubs',
    structure: 'Structure', cutEdges: 'Cut edges', edges: 'edges', kcore: 'max. k-core', nodes: 'nodes',
    provenance: 'Provenance', backed: 'backed', unbackedNodes: 'entities without evidence', singleSource: 'High-confidence relationships with a single source', none: 'none', sharedDocs: 'Documents backing several entities',
    articulation: 'Articulation point', degree: 'degree', splits: 'splits into {parts} parts, largest {largest}',
    rules: 'Rule hits', duplicates: 'Possible duplicates', mixed: 'mixed types',
    timeline: 'Mentions per month',
    conflicts: 'Conflicting claims', conflictsNone: 'no differing claims', currency: 'Currency of the evidence',
    risk: 'How much the file can carry', riskNone: 'every entity is covered', riskStates: 'no document / thin and old / single source / outdated / undated / solid',
    footer: 'Hints, not proof: verify the sources; process personal data only with a legal basis.',
  },
} as const;
export function caseReport(input: CaseReportInput, generated: string, lang: 'de' | 'en' = 'de'): string {
  const text = CASE_REPORT_TEXT[lang] ?? CASE_REPORT_TEXT.de;
  const locale = lang === 'en' ? 'en-GB' : 'de-DE';
  const fill = (template: string, values: Record<string, string | number>) => template.replace(/\{(\w+)\}/g, (_, key: string) => String(values[key] ?? ''));
  const clean = (value: string) => value.replace(/\s+/g, ' ').trim();
  const lines: string[] = [text.title, ''];
  lines.push(`${text.generated}: ${clean(generated)}`);
  const { stats } = input;
  lines.push(`${text.scope}: ${stats.filteredNodes.toLocaleString(locale)} ${text.nodes} / ${stats.filteredEdges.toLocaleString(locale)} ${text.edges} ${text.of} ${stats.totalNodes.toLocaleString(locale)} / ${stats.totalEdges.toLocaleString(locale)}`);
  const levelText = lang === 'en' ? ({ Einzelknoten: 'single nodes', Gruppen: 'groups', Feingruppen: 'fine groups', Übersicht: 'overview' } as Record<string, string>)[clean(stats.level)] ?? clean(stats.level) : clean(stats.level);
  lines.push(`${text.level}: ${levelText} · ${text.focus}: ${stats.focused ? text.yes : text.no} · ${text.lod}: ${stats.grouped ? text.yes : text.no}`);
  const labelOf = new Map(input.nodes.map(node => [node.id, node.label]));
  const notes = Object.entries(input.notes ?? {})
    .map(([id, note]) => ({ id, note: normalizeNote(note), label: clean(labelOf.get(id) ?? id) }))
    .filter(entry => entry.note)
    .sort((a, b) => (a.label.toLowerCase() < b.label.toLowerCase() ? -1 : a.label.toLowerCase() > b.label.toLowerCase() ? 1 : a.id < b.id ? -1 : 1));
  lines.push('', `## ${text.notes} (${notes.length})`, '');
  if (notes.length === 0) lines.push(text.noNotes);
  for (const entry of notes) {
    lines.push(`- **${entry.label}** (\`${entry.id}\`)`);
    entry.note.split('\n').forEach(line => lines.push(`  ${line}`));
  }
  if (input.analytics) {
    const a = input.analytics;
    lines.push('', `## ${text.analytics}`, '');
    lines.push(`- ${text.communities}: ${a.communities} · ${text.density}: ${(a.density * 100).toFixed(2)} %`);
    if (a.temporalRange) lines.push(`- ${text.range}: ${a.temporalRange.first} → ${a.temporalRange.last}`);
    if (a.hubs.length) lines.push(`- ${text.hubs}: ${a.hubs.slice(0, 5).map(hub => `${clean(hub.name)} (${(hub.rank * 100).toFixed(1)} %)`).join(', ')}`);
  }
  if (input.vulnerability) {
    const v = input.vulnerability;
    lines.push('', `## ${text.structure}`, '');
    lines.push(`- ${text.cutEdges}: ${v.cutEdges} · ${text.kcore}: ${v.maxCore} (${v.maxCoreSize} ${text.nodes})`);
    for (const vertex of v.cutVertices) lines.push(`- ${text.articulation} ${clean(vertex.label)} (${text.degree} ${vertex.degree}) — ${fill(text.splits, { parts: vertex.impact.components, largest: vertex.impact.largest })}`);
  }
  if (input.gaps) {
    const g = input.gaps;
    lines.push('', `## ${text.provenance}`, '');
    lines.push(`- ${text.nodes}: ${g.nodesBacked}/${g.nodes} ${text.backed} · ${text.edges}: ${g.edgesBacked}/${g.edges} ${text.backed}`);
    lines.push(`- ${text.singleSource}: ${g.singleSourceCount}`);
    for (const edge of g.singleSource) lines.push(`  - \`${edge.id}\` ${clean(edge.type)} (${(edge.confidence * 100).toFixed(1)} %) — ${clean(labelOf.get(edge.source) ?? edge.source)} → ${clean(labelOf.get(edge.target) ?? edge.target)}`);
    lines.push(`- ${text.unbackedNodes}: ${g.unbackedNodeCount}${g.unbacked.length ? ` — ${g.unbacked.map(node => clean(node.label)).join(', ')}` : ` (${text.none})`}`);
    if (input.sharedSources?.length) lines.push(`- ${text.sharedDocs}: ${input.sharedSources.map(group => `\`${group.document}\` (${group.entities.map(entity => clean(entity.label)).join(', ')})`).join('; ')}`);
  }
  if (input.insights?.length) {
    lines.push('', `## ${text.rules}`, '');
    for (const insight of input.insights) lines.push(`- [${clean(insight.rule)}] ${clean(insight.title)}${insight.entities.length ? ` — ${insight.entities.map(entity => clean(entity.name)).join(', ')}` : ''}`);
  }
  if (input.duplicates?.length) {
    lines.push('', `## ${text.duplicates}`, '');
    for (const group of input.duplicates) lines.push(`- ${clean(group.label)} — ${group.ids.map(id => `\`${id}\``).join(', ')}${group.sameType ? '' : ` (${text.mixed})`}`);
  }
  if (input.freshness) {
    const f = input.freshness;
    lines.push('', `## ${text.currency}`, '');
    lines.push(`- ${text.generated}: ${f.reference} · ${f.months} ${lang === 'en' ? 'months' : 'Monate'}`);
    lines.push(`- ${f.staleCount}/${f.entities} ${lang === 'en' ? 'entities without evidence inside the window' : 'Entitäten ohne Beleg im Fenster'} · ${f.undatedCount} ${lang === 'en' ? 'undated' : 'ohne Datum'}`);
    if (f.stale.length) lines.push(`  - ${f.stale.map(entity => `${clean(entity.label)} (${entity.last})`).join(', ')}`);
  }
  if (input.risk) {
    const r = input.risk;
    lines.push('', `## ${text.risk}`, '');
    lines.push(`- ${r.weakCount}/${r.entities} ${lang === 'en' ? 'entities need a follow-up' : 'Entitäten brauchen eine Nachrecherche'} (${r.months} ${lang === 'en' ? 'months' : 'Monate'})`);
    lines.push(`- ${Object.entries(r.states).map(([state, count]) => `${state}: ${count}`).join(' · ')}`);
    if (!r.weakCount) lines.push(`- ${text.riskNone}`);
    for (const entry of r.entries) lines.push(`  - ${clean(entry.label)} — ${entry.state} (${entry.documents} ${lang === 'en' ? 'doc(s)' : 'Beleg(e)'}, ${entry.sources} ${lang === 'en' ? 'source(s)' : 'Quelle(n)'}, ${entry.relations} ${lang === 'en' ? 'rel.' : 'Bez.'}${entry.last ? `, ${entry.last}` : ''})`);
  }
  if (input.conflicts) {
    lines.push('', `## ${text.conflicts}`, '');
    if (!input.conflicts.length) lines.push(`- ${text.conflictsNone}`);
    for (const conflict of input.conflicts) {
      lines.push(`- ${clean(conflict.labels[0])} ↔ ${clean(conflict.labels[1])} — ${conflict.kind}`);
      for (const claim of conflict.claims) lines.push(`  - ${clean(claim.type)}: ${claim.documents} ${lang === 'en' ? 'doc(s)' : 'Beleg(e)'}${claim.first ? ` · ${claim.first}${claim.last && claim.last !== claim.first ? ` → ${claim.last}` : ''}` : ''}`);
    }
  }
  if (input.timeline?.length) {
    lines.push('', `## ${text.timeline}`, '');
    lines.push(input.timeline.map(bucket => `${bucket.month}: ${bucket.count}`).join(' · '));
  }
  lines.push('', '---', text.footer);
  return lines.join('\n') + '\n';
}

export function nodesToCSV(nodes: GraphNode[]): string {
  const header = 'id,label,type,confidence,degree,rank,community,firstSeen,lastSeen';
  const rows = nodes.map(n => [n.id, n.label, n.type, n.confidence.toFixed(3), n.degree, n.rank,
    n.community, n.firstSeen, n.lastSeen].map(csvCell).join(','));
  return [header, ...rows].join('\r\n') + '\r\n';
}
export function edgesToCSV(edges: GraphEdge[]): string {
  const header = 'id,source,target,type,confidence,weight';
  const rows = edges.map(e => [e.id, e.source, e.target, e.type, e.confidence.toFixed(3), e.weight.toFixed(3)].map(csvCell).join(','));
  return [header, ...rows].join('\r\n') + '\r\n';
}
export function viewToJSON(view: { nodes: GraphNode[]; edges: GraphEdge[]; analytics?: GraphAnalytics; insights?: GraphInsight[] }): string {
  return JSON.stringify({ nodes: view.nodes, edges: view.edges, ...(view.analytics ? { analytics: view.analytics } : {}),
    ...(view.insights?.length ? { insights: view.insights } : {}) }, null, 2) + '\n';
}

/** Shareable permalinks: bounded, validated URL state (no secrets, no free text). */
export interface ViewUrlState { mode?: 'network' | 'map' | 'list'; q?: string; focus?: string; community?: string; from?: string; to?: string; color?: 'type' | 'community'; sources?: SourceFilter; outlet?: string; fresh?: FreshWindow; dispute?: boolean; weak?: boolean }
export function encodeViewState(state: ViewUrlState): string {
  const params = new URLSearchParams();
  if (state.mode) params.set('mode', state.mode);
  if (state.q?.trim()) params.set('q', state.q.trim().slice(0, 120));
  if (state.focus) params.set('focus', state.focus.slice(0, 200));
  if (state.community) params.set('community', state.community.slice(0, 24));
  if (state.color === 'community') params.set('color', 'community');
  if (state.from) params.set('from', state.from);
  if (state.to) params.set('to', state.to);
  if (state.sources && SOURCE_FILTERS.includes(state.sources)) params.set('sources', state.sources);
  const outlet = state.outlet?.trim().slice(0, 80);
  if (outlet) params.set('outlet', outlet);
  if (state.dispute) params.set('dispute', '1');
  if (state.weak) params.set('weak', '1');
  if (state.fresh && isFreshWindow(state.fresh) && state.fresh !== DEFAULT_FRESH_MONTHS) params.set('fresh', String(state.fresh));
  return params.toString();
}
export function decodeViewState(search: string): ViewUrlState {
  const params = new URLSearchParams(search), state: ViewUrlState = {};
  const bounded = (key: string, max: number) => { const v = params.get(key); return v && v.length <= max ? v : undefined; };
  const mode = bounded('mode', 10);
  if (mode === 'network' || mode === 'map' || mode === 'list') state.mode = mode;
  const q = bounded('q', 120); if (q) state.q = q;
  const focus = bounded('focus', 200); if (focus) state.focus = focus;
  const community = bounded('community', 24); if (community) state.community = community;
  if (params.get('color') === 'community') state.color = 'community';
  const from = isoDate(params.get('from')), to = isoDate(params.get('to'));
  if (from) state.from = from;
  if (to) state.to = to;
  const sources = params.get('sources') as SourceFilter | null;
  if (sources && SOURCE_FILTERS.includes(sources)) state.sources = sources;
  const outlet = bounded('outlet', 80);
  if (outlet) state.outlet = outlet;
  if (params.get('dispute') === '1') state.dispute = true;
  if (params.get('weak') === '1') state.weak = true;
  const fresh = Number(params.get('fresh'));
  if (isFreshWindow(fresh)) state.fresh = fresh;
  return state;
}

/* Wave 2b: on-demand graph algorithms in the worker (betweenness brokers,
 * Louvain refinement). Both are bounded and deterministic; the naming and
 * ranking helpers live here so they are unit-testable without a DOM. */

/** Honest compute caps: above these the UI explains the limit instead of freezing. */
export const ANALYSIS_LIMITS = { broker: 1200, louvain: 20_000 } as const;

/** Stable names for an algorithm partition: largest community first, ties by
 * smallest member id → "l000", "l001", … (distinct from export "c###"). */
export function namePartition(partition: Record<string, string | number>): Map<string, string> {
  const groups = new Map<string, string[]>();
  for (const id of Object.keys(partition).sort()) {
    const key = String(partition[id]);
    const list = groups.get(key);
    if (list) list.push(id); else groups.set(key, [id]);
  }
  const ranked = [...groups.values()].sort((a, b) => b.length - a.length || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  const out = new Map<string, string>();
  ranked.forEach((members, index) => {
    const name = `l${String(index).padStart(3, '0')}`;
    for (const id of members) out.set(id, name);
  });
  return out;
}

/** Top brokers by betweenness share (value / total), deterministic tie-break. */
export function brokerShares(values: Record<string, number>, limit = 10): { id: string; share: number }[] {
  const entries = Object.entries(values).filter(([, value]) => Number.isFinite(value) && value > 0);
  const total = entries.reduce((sum, [, value]) => sum + value, 0);
  if (!total) return [];
  entries.sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  return entries.slice(0, Math.max(1, limit)).map(([id, value]) => ({ id, share: value / total }));
}
