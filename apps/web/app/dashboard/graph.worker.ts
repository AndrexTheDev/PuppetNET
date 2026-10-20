/** All JSON parsing, filtering, aggregation and ForceAtlas2 run off the UI thread. */
import { MultiDirectedGraph } from 'graphology';
import forceAtlas2 from 'graphology-layout-forceatlas2';
import { demoDataset, normalizeDataset, filterDataset, groupDataset, geographicPoints, evidenceFor, levelFor, shortestPath, LIMITS, TYPES, type Dataset, type Filters, type View } from './graph-data';
import type { WorkerRequest, WorkerResponse } from './protocol';
const scope = self as unknown as { onmessage: ((event: MessageEvent<WorkerRequest>) => void) | null; postMessage: (value: WorkerResponse, transfer?: Transferable[]) => void };
let dataset: Dataset = { nodes: [], edges: [], evidence: [] };
let generation = 0, version = 0;
let graph: MultiDirectedGraph | null = null, view: View | null = null;
let members = new Map<string, string>(), focus: Set<string> | null = null;
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
/** The currently visible slice: active filters plus any drill-down focus set. */
function currentFiltered(filters: Filters): Dataset {
  let filtered = filterDataset(dataset, filters);
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
  view = { version, nodes: grouped.nodes, edges: grouped.edges, mapPoints: geo.points, stats: { totalNodes: dataset.nodes.length, totalEdges: dataset.edges.length, filteredNodes: filtered.nodes.length, filteredEdges: filtered.edges.length, locatedNodes: geo.count, grouped: !!members.size, level: levelFor(filtered.nodes.length, filters), focused: !!focus, truncated: dataset.truncated === true }, ...(dataset.analytics ? { analytics: dataset.analytics } : {}) };
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
  if (data.kind === 'path') {
    // Pathfinder runs on the visible slice (filters + drill focus) — an honest
    // "no path in this excerpt" instead of a hidden full-dataset answer.
    const result = shortestPath(currentFiltered(lastFilters), data.source, data.target);
    send({ kind: 'path', source: data.source, target: data.target, nodes: result?.nodes ?? null,
      edges: result ? result.edges.map(e => ({ id: e.id, source: e.source, target: e.target, type: e.type })) : null,
      hops: result ? Math.max(0, result.nodes.length - 1) : 0 });
    return;
  }
  if (data.kind === 'layout') { layoutWanted = data.running; if (data.running) start(); else stop(); return; }
  const request = ++generation;
  version = data.version;
  stop();
  try {
    if (data.kind === 'demo') { dataset = demoDataset(data.count); focus = null; }
    if (data.kind === 'import') {
      if (data.file && data.file.size > LIMITS.bytes) throw new Error('JSON ist größer als 64 MiB.');
      const text = data.file ? await data.file.text() : data.text ?? '';
      if (request !== generation) return;
      if (text.length > LIMITS.bytes) throw new Error('JSON ist größer als das Importlimit.');
      const next = normalizeDataset(JSON.parse(text));
      dataset = next; focus = null;
    }
    if (data.kind === 'drill') { focus = new Set([...members].filter(([, group]) => group === data.group).map(([id]) => id)); }
    if (data.kind === 'reset') focus = null;
    build(data.filters);
  } catch (error) { send({ kind: 'error', version: data.version, message: error instanceof Error ? error.message : 'Verarbeitung fehlgeschlagen' }); }
};
