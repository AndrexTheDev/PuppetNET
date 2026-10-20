/** All JSON parsing, filtering, aggregation and ForceAtlas2 run off the UI thread. */
import { MultiDirectedGraph, UndirectedGraph } from 'graphology';
import forceAtlas2 from 'graphology-layout-forceatlas2';
import betweenness from 'graphology-metrics/centrality/betweenness';
import louvain from 'graphology-communities-louvain';
import { demoDataset, normalizeDataset, filterDataset, groupDataset, geographicPoints, evidenceFor, evidenceForEdge, levelFor, shortestPath, namePartition, brokerShares, egoNetwork, mentionTimeline, duplicateCandidates, bundleEdges, structuralVulnerability, pathTimeline, multiFailureImpact, EGO_DEPTH_LIMIT, ANALYSIS_LIMITS, LIMITS, TYPES, type Dataset, type Filters, type View } from './graph-data';
import type { WorkerRequest, WorkerResponse } from './protocol';
const scope = self as unknown as { onmessage: ((event: MessageEvent<WorkerRequest>) => void) | null; postMessage: (value: WorkerResponse, transfer?: Transferable[]) => void };
let dataset: Dataset = { nodes: [], edges: [], evidence: [] };
let generation = 0, version = 0;
let graph: MultiDirectedGraph | null = null, view: View | null = null;
let members = new Map<string, string>(), focus: Set<string> | null = null;
let louvainPartition: Map<string, string> | null = null;
let lastFilters: Filters = { confidence: 0, weight: 0, types: [...TYPES], lod: true, ratio: 1 };
let timer: ReturnType<typeof setTimeout> | undefined;
let passes = 0, started = 0, layoutWanted = false;
const send = (value: WorkerResponse, transfer?: Transferable[]) => scope.postMessage(value, transfer);
function stop() { clearTimeout(timer); timer = undefined; send({ kind: 'layout', running: false }); }
function tick() {
  if (!graph || !graph.order) { stop(); return; }
  try {
    forceAtlas2.assign(graph, { iterations: graph.order > 5000 ? 1 : 2, settings: { ...forceAtlas2.inferSettings(graph), barnesHutOptimize: true, barnesHutTheta: .7, gravity: 1, scalingRatio: 8, slowDown: 8 }, getEdgeWeight: 'weight' });
    const positions = new Float32Array(graph.order * 2);
    let offset = 0;
    graph.forEachNode((_id, attr) => { positions[offset++] = attr.x; positions[offset++] = attr.y; });
    send({ kind: 'positions', version, positions }, [positions.buffer]);
    passes++;
    if (passes >= (graph.order > 5000 ? 12 : 70) || performance.now() - started > 15_000) { stop(); return; }
    timer = setTimeout(tick, graph.order > 5000 ? 600 : 160);
  } catch (error) { stop(); send({ kind: 'error', version, message: `Layout: ${error instanceof Error ? error.message : 'Fehler'}` }); }
}
function start() { stop(); if (!graph || !graph.order) return; passes = 0; started = performance.now(); send({ kind: 'layout', running: true }); timer = setTimeout(tick, 80); }
/** The dataset with an active Louvain partition applied (view-based refinement
 * replaces the export communities until the user resets it). */
function effectiveDataset(): Dataset {
  if (!louvainPartition) return dataset;
  const partition = louvainPartition;
  return { ...dataset, nodes: dataset.nodes.map(n => { const c = partition.get(n.id); return c ? { ...n, community: c } : n; }) };
}
/** Weighted undirected analysis graph: parallel edges merge (weights add),
 * self-loops drop — betweenness and Louvain both want a simple weighted graph. */
function weightedUndirected(data: Dataset): UndirectedGraph {
  const g = new UndirectedGraph();
  for (const n of data.nodes) if (!g.hasNode(n.id)) g.addNode(n.id);
  for (const e of data.edges) {
    if (e.source === e.target || !g.hasNode(e.source) || !g.hasNode(e.target)) continue;
    const w = Math.max(.001, Number.isFinite(e.weight) ? e.weight : .001);
    if (g.hasEdge(e.source, e.target)) g.setEdgeAttribute(e.source, e.target, 'weight', g.getEdgeAttribute(e.source, e.target, 'weight') + w);
    else g.addEdge(e.source, e.target, { weight: w });
  }
  return g;
}
/** The currently visible slice: active filters plus any drill-down focus set. */
function currentFiltered(filters: Filters): Dataset {
  let filtered = filterDataset(effectiveDataset(), filters);
  if (focus) {
    const nodes = filtered.nodes.filter(n => focus!.has(n.id)), ids = new Set(nodes.map(n => n.id));
    filtered = { ...filtered, nodes, edges: filtered.edges.filter(e => ids.has(e.source) && ids.has(e.target)) };
  }
  return filtered;
}
function build(filters: Filters) {
  stop();
  lastFilters = filters;
  const filtered = currentFiltered(filters);
  const grouped = groupDataset(filtered, filters), geo = geographicPoints(filtered.nodes);
  members = grouped.members;
  // Wave 7: temporal analytics ride along with every view — computed on the
  // filtered single nodes (before LoD grouping), empty slices stay empty.
  const timeline = mentionTimeline(filtered.nodes);
  // Wave 8: entity-resolution hints on the same filtered single nodes.
  const duplicates = duplicateCandidates(filtered.nodes);
  // Wave 9: structural vulnerability of the visible slice + optional bundling
  // of parallel relationships (grouped edges are what the user actually sees).
  const vulnerability = structuralVulnerability(filtered.nodes, filtered.edges);
  const viewEdges = filters.bundle ? bundleEdges(grouped.edges) : grouped.edges;
  view = { version, nodes: grouped.nodes, edges: viewEdges, mapPoints: geo.points, stats: { totalNodes: dataset.nodes.length, totalEdges: dataset.edges.length, filteredNodes: filtered.nodes.length, filteredEdges: filtered.edges.length, locatedNodes: geo.count, grouped: !!members.size, level: levelFor(filtered.nodes.length, filters), focused: !!focus, truncated: dataset.truncated === true }, ...(dataset.analytics ? { analytics: dataset.analytics } : {}), ...(dataset.insights?.length ? { insights: dataset.insights } : {}), ...(timeline.length ? { timeline } : {}), ...(duplicates.length ? { duplicates } : {}), ...(vulnerability ? { vulnerability } : {}) };
  graph = new MultiDirectedGraph();
  for (const n of view.nodes) graph.addNode(n.id, { x: n.x, y: n.y, size: 2 });
  for (const e of view.edges) graph.addDirectedEdgeWithKey(e.id, e.source, e.target, { weight: Math.max(.001, e.weight) });
  send({ kind: 'view', view });
  // Start only once the view has been posted. Sigma construction is chunked on the UI.
  if (layoutWanted) start();
}
scope.onmessage = async ({ data }) => {
  if (data.kind === 'detail') {
    send({ kind: 'detail', node: dataset.nodes.find(n => n.id === data.id), evidence: evidenceFor(dataset, data.id) }); return;
  }
  if (data.kind === 'edgeDetail') {
    // Wave 5 edge provenance: every published edge carries its own source list.
    send({ kind: 'edgeDetail', id: data.id, evidence: evidenceForEdge(dataset, data.id) }); return;
  }
  if (data.kind === 'path') {
    // Pathfinder runs on the visible slice (filters + drill focus) — an honest
    // "no path in this excerpt" instead of a hidden full-dataset answer.
    const slice = currentFiltered(lastFilters);
    const result = shortestPath(slice, data.source, data.target);
    send({ kind: 'path', source: data.source, target: data.target, nodes: result?.nodes ?? null,
      edges: result ? result.edges.map(e => ({ id: e.id, source: e.source, target: e.target, type: e.type })) : null,
      hops: result ? Math.max(0, result.nodes.length - 1) : 0,
      // Wave 10: the documented window per hop travels with the chain.
      timeline: result ? pathTimeline(slice, result.nodes) : null });
    return;
  }
  if (data.kind === 'broker') {
    // Betweenness (Brandes) is O(V·E): bounded to the visible slice and capped,
    // above the cap the UI explains the limit instead of freezing the worker.
    const filtered = currentFiltered(lastFilters);
    if (filtered.nodes.length > ANALYSIS_LIMITS.broker) {
      send({ kind: 'broker', version, brokers: [], notice: `Broker-Analyse benötigt höchstens ${ANALYSIS_LIMITS.broker.toLocaleString('de-DE')} Knoten im Ausschnitt — aktuell ${filtered.nodes.length.toLocaleString('de-DE')}. Bitte Filter eingrenzen.` });
      return;
    }
    const g = weightedUndirected(filtered);
    if (!g.order) { send({ kind: 'broker', version, brokers: [], notice: 'Keine Knoten im aktuellen Ausschnitt.' }); return; }
    const labels = new Map(filtered.nodes.map(n => [n.id, n.label]));
    const brokers = brokerShares(betweenness(g)).map(b => ({ ...b, label: labels.get(b.id) ?? b.id }));
    send({ kind: 'broker', version, brokers });
    return;
  }
  if (data.kind === 'louvain') {
    if (!data.active) {
      louvainPartition = null;
      build(lastFilters);
      send({ kind: 'louvain', active: false, communities: 0 });
      return;
    }
    // Louvain refines the whole loaded dataset (not just the current filter),
    // so switching filters keeps one coherent partition. randomWalk:false makes
    // it deterministic — the same dataset always yields the same communities.
    if (dataset.nodes.length > ANALYSIS_LIMITS.louvain) {
      send({ kind: 'louvain', active: !!louvainPartition, communities: louvainPartition ? new Set(louvainPartition.values()).size : 0,
        notice: `Louvain-Nachschärfung unterstützt höchstens ${ANALYSIS_LIMITS.louvain.toLocaleString('de-DE')} geladene Knoten — aktuell ${dataset.nodes.length.toLocaleString('de-DE')}.` });
      return;
    }
    const partition = louvain(weightedUndirected(dataset), { randomWalk: false, getEdgeWeight: 'weight' });
    louvainPartition = namePartition(partition);
    build(lastFilters);
    send({ kind: 'louvain', active: true, communities: new Set(louvainPartition.values()).size });
    return;
  }
  if (data.kind === 'layout') { layoutWanted = data.running; if (data.running) start(); else stop(); return; }
  const request = ++generation;
  version = data.version;
  stop();
  try {
    if (data.kind === 'ego') {
      // Wave 7 ego expansion: absolute neighborhood within the filtered slice.
      // An older drill/ego focus is deliberately ignored so every expansion
      // starts from the published state, never from a stale sub-selection.
      const neighborhood = egoNetwork(filterDataset(effectiveDataset(), data.filters), data.id, data.depth);
      if (neighborhood) focus = new Set(neighborhood);
      build(data.filters);
      send({ kind: 'ego', id: data.id, depth: Math.max(1, Math.min(EGO_DEPTH_LIMIT, Math.trunc(data.depth) || 1)), size: neighborhood ? neighborhood.length : null });
      return;
    }
    if (data.kind === 'impact') {
      // Wave 13: failure simulation for the current selection — computed on the
      // same filtered slice the investigator is looking at, never on the full
      // export behind it.
      const slice = currentFiltered(lastFilters);
      const impact = multiFailureImpact(slice.nodes, slice.edges, data.ids);
      send({ kind: 'impact', version: data.version, removed: impact?.removed ?? 0, components: impact?.components ?? 0, largest: impact?.largest ?? 0, isolated: impact?.isolated ?? 0 });
      return;
    }
    if (data.kind === 'demo') { dataset = demoDataset(data.count); focus = null; louvainPartition = null; }
    if (data.kind === 'import') {
      if (data.file && data.file.size > LIMITS.bytes) throw new Error('JSON ist größer als 64 MiB.');
      const text = data.file ? await data.file.text() : data.text ?? '';
      if (request !== generation) return;
      if (text.length > LIMITS.bytes) throw new Error('JSON ist größer als das Importlimit.');
      const next = normalizeDataset(JSON.parse(text));
      dataset = next; focus = null; louvainPartition = null;
    }
    if (data.kind === 'drill') { focus = new Set([...members].filter(([, group]) => group === data.group).map(([id]) => id)); }
    if (data.kind === 'reset') focus = null;
    build(data.filters);
  } catch (error) { send({ kind: 'error', version: data.version, message: error instanceof Error ? error.message : 'Verarbeitung fehlgeschlagen' }); }
};
