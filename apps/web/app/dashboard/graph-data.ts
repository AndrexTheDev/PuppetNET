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
export interface GraphEdge { id: string; source: string; target: string; type: string; confidence: number; weight: number; evidenceIds: string[]; count?: number }
export interface Dataset { nodes: GraphNode[]; edges: GraphEdge[]; evidence: Evidence[]; truncated?: boolean; analytics?: GraphAnalytics; insights?: GraphInsight[] }
export interface Filters { confidence: number; weight: number; types: EntityType[]; lod: boolean; ratio: number; community?: string; from?: string; to?: string }
export interface MapPoint { id: string; lat: number; lon: number; count: number; label: string; exampleId: string }
export interface View {
  version: number; nodes: GraphNode[]; edges: GraphEdge[]; mapPoints: MapPoint[];
  stats: { totalNodes: number; totalEdges: number; filteredNodes: number; filteredEdges: number; locatedNodes: number; grouped: boolean; level: string; focused: boolean; truncated: boolean };
  analytics?: GraphAnalytics;
  insights?: GraphInsight[];
  /** Wave 7: first mentions per month in the filtered slice (deterministic buckets). */
  timeline?: { month: string; count: number }[];
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
  const edges = data.edges.filter(e => e.confidence >= filters.confidence && e.weight >= filters.weight && keep.has(e.source) && keep.has(e.target));
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

const csvCell = (value: string | number | undefined): string => {
  const s = value === undefined || value === null ? '' : String(value);
  return /[",\n\r]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
};

/** RFC-4180 CSV of the visible view — client-side reporting without a server. */
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
export interface ViewUrlState { mode?: 'network' | 'map' | 'list'; q?: string; focus?: string; community?: string; from?: string; to?: string; color?: 'type' | 'community' }
export function encodeViewState(state: ViewUrlState): string {
  const params = new URLSearchParams();
  if (state.mode) params.set('mode', state.mode);
  if (state.q?.trim()) params.set('q', state.q.trim().slice(0, 120));
  if (state.focus) params.set('focus', state.focus.slice(0, 200));
  if (state.community) params.set('community', state.community.slice(0, 24));
  if (state.color === 'community') params.set('color', 'community');
  if (state.from) params.set('from', state.from);
  if (state.to) params.set('to', state.to);
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
