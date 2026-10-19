/** Shared, deterministic data contract. Executed by the data/layout worker, also unit-tested offline. */
export const TYPES = ['Person', 'Organization', 'Location', 'Craft', 'Document', 'Other'] as const;
export type EntityType = typeof TYPES[number];
export const COLORS: Record<EntityType, string> = { Person: '#60dfd0', Organization: '#a397ff', Location: '#f3c773', Craft: '#ff9e91', Document: '#7daeff', Other: '#9bacc5' };
export interface Evidence { id: string; title: string; url?: string; source?: string; date?: string }
export interface GraphNode {
  id: string; label: string; type: EntityType; confidence: number; x: number; y: number;
  cluster: string; lat?: number; lon?: number; evidenceIds: string[];
  count?: number; examples?: string[]; internalEdges?: number; group?: boolean;
}
export interface GraphEdge { id: string; source: string; target: string; type: string; confidence: number; weight: number; evidenceIds: string[]; count?: number }
export interface Dataset { nodes: GraphNode[]; edges: GraphEdge[]; evidence: Evidence[]; truncated?: boolean }
export interface Filters { confidence: number; weight: number; types: EntityType[]; lod: boolean; ratio: number }
export interface MapPoint { id: string; lat: number; lon: number; count: number; label: string; exampleId: string }
export interface View {
  version: number; nodes: GraphNode[]; edges: GraphEdge[]; mapPoints: MapPoint[];
  stats: { totalNodes: number; totalEdges: number; filteredNodes: number; filteredEdges: number; locatedNodes: number; grouped: boolean; level: string; focused: boolean; truncated: boolean };
}
export const LIMITS = { nodes: 200_000, edges: 500_000, bytes: 64 * 1024 * 1024 };
const record = (v: unknown): Record<string, unknown> => v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {};
const text = (v: unknown, max = 500) => typeof v === 'string' || typeof v === 'number' ? String(v).slice(0, max) : '';
const score = (v: unknown) => v === null || v === '' || !Number.isFinite(Number(v)) ? 0 : Math.max(0, Math.min(1, Number(v)));
const ids = (v: unknown) => Array.isArray(v) ? v.slice(0, 200).map(x => text(x)).filter(Boolean) : [];
export function hash(value: string): number { let n = 2166136261; for (let i = 0; i < value.length; i++) n = Math.imul(n ^ value.charCodeAt(i), 16777619); return n >>> 0; }
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
      ...(hasGeo ? { lat: latitude, lon: longitude } : {}), evidenceIds: [...ids(attr.evidenceIds ?? attr.doc_ids), ...embedded.map(d => d.id)] };
  });
  let dangling = 0;
  const edges = raw.edges.map((item, i): GraphEdge | null => {
    const e = record(item), attr = { ...record(e.attributes), ...e }, source = text(attr.source), target = text(attr.target);
    if (!seen.has(source) || !seen.has(target)) { dangling++; return null; }
    return { id: `${text(attr.id ?? attr.key ?? 'edge')}:${i}`, source, target, type: text(attr.type ?? 'RELATED_TO', 80), confidence: score(attr.confidence), weight: score(attr.weight), evidenceIds: [...ids(attr.evidenceIds ?? attr.doc_ids), ...(attr.doc_id ? [text(attr.doc_id)] : [])] };
  }).filter((e): e is GraphEdge => e !== null);
  if (dangling) throw new Error(`${dangling} Kanten verweisen auf fehlende Knoten. Bitte einen geschlossenen Teilgraphen importieren.`);
  return { nodes, edges, evidence, truncated: raw.truncated === true };
}
export function filterDataset(data: Dataset, filters: Filters): Dataset {
  const types = new Set(filters.types);
  const nodes = data.nodes.filter(n => types.has(n.type) && n.confidence >= filters.confidence);
  const keep = new Set(nodes.map(n => n.id));
  const edges = data.edges.filter(e => e.confidence >= filters.confidence && e.weight >= filters.weight && keep.has(e.source) && keep.has(e.target));
  return { nodes, edges, evidence: data.evidence };
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
