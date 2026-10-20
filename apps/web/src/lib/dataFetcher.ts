/** Browser transport for the single reviewed public static graph. No Worker is
 * needed to read data. Profile HTML reads the same aggregate at build time.
 */
import { normalizeDataset, type Dataset } from '../../app/dashboard/graph-data';
import { readResponse } from '../../app/dashboard/api';

export const STATIC_GRAPH_PATH = '/data/graph_latest.json';
export interface StaticGraph {
  nodes: unknown[];
  edges: unknown[];
  evidence?: unknown[];
  demo?: boolean;
  revision?: string;
  generatedAt?: string;
  [key: string]: unknown;
}
export type GraphLoad =
  | { source: 'static'; graph: StaticGraph; dataset: Dataset; message: string }
  | { source: 'demo'; message: string };

/** Structural/graph validation before a snapshot is accepted by the UI worker. */
export function validateStaticGraph(value: unknown): { graph: StaticGraph; dataset: Dataset } {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Ungültiger Datensatz.');
  const graph = value as StaticGraph;
  if (!Array.isArray(graph.nodes) || !Array.isArray(graph.edges)) throw new Error('Datensatz benötigt nodes[] und edges[].');
  return { graph, dataset: normalizeDataset(graph) };
}

/** Caller cancellation propagates; offline/HTTP/schema errors yield labelled demo.
 * Demo generation stays in graph.worker.ts rather than blocking the UI thread.
 */
export async function loadStaticGraph(options: { signal?: AbortSignal; fetcher?: typeof fetch; timeoutMs?: number } = {}): Promise<GraphLoad> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  options.signal?.addEventListener('abort', abort, { once: true });
  if (options.signal?.aborted) controller.abort();
  const timer = setTimeout(abort, options.timeoutMs ?? 5000);
  try {
    const response = await (options.fetcher ?? fetch)(STATIC_GRAPH_PATH, {
      method: 'GET', credentials: 'omit', signal: controller.signal,
      headers: { Accept: 'application/json' }, cache: 'default', redirect: 'error',
    });
    const text = await readResponse(response);
    if (controller.signal.aborted) throw new DOMException('Abgebrochen', 'AbortError');
    const { graph, dataset } = validateStaticGraph(JSON.parse(text));
    return { source: 'static', graph, dataset,
      message: graph.demo === true ? 'Fiktive veröffentlichte Demo' : 'Veröffentlichter CDN-Datensatz' };
  } catch (error) {
    if (options.signal?.aborted) throw error;
    return { source: 'demo', message: 'CDN-Datensatz fehlt oder ist offline/ungültig. Es werden fiktive Beispieldaten angezeigt.' };
  } finally {
    clearTimeout(timer); options.signal?.removeEventListener('abort', abort);
  }
}

/** No same-origin assumption: Pages has no /api/search backend of its own. */
export function searchWorkerOrigin(input: string): string {
  if (!input.trim()) throw new Error('Für On-Demand-Recherchen ist eine separate Worker-URL erforderlich. CDN-Daten benötigen keine Worker-URL.');
  const url = new URL(input);
  if (url.protocol !== 'https:' || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('Worker-URL muss ein HTTPS-Ursprung ohne Pfad, Zugangsdaten oder Parameter sein.');
  }
  return url.origin;
}

export async function requestResearch(input: { workerUrl: string; token: string; query: string; signal?: AbortSignal }, fetcher: typeof fetch = fetch): Promise<void> {
  const origin = searchWorkerOrigin(input.workerUrl);
  if (!input.token || input.token.length < 32 || /[\r\n]/.test(input.token)) throw new Error('Ein gültiger Suchzugangstoken ist erforderlich.');
  const query = input.query.trim();
  if (!query || [...input.query].length > 120 || /[\u0000-\u001f\u007f]/.test(input.query)) throw new Error('Suchbegriff: 1–120 Zeichen, keine Steuerzeichen.');
  const controller = new AbortController();
  const abort = () => controller.abort();
  input.signal?.addEventListener('abort', abort, { once: true });
  if (input.signal?.aborted) controller.abort();
  const timeout = setTimeout(abort, 15_000);
  try {
    const response = await fetcher(`${origin}/api/search`, { method: 'POST', credentials: 'omit',
      redirect: 'error', signal: controller.signal,
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${input.token}` },
      body: JSON.stringify({ query }) });
    if (response.status !== 202) throw new Error(`Recherche nicht angenommen (HTTP ${response.status}).`);
    const body = JSON.parse(await readResponse(response));
    if (body.status !== 'accepted') throw new Error('Ungültige Bestätigung des Such-Workers.');
  } finally { clearTimeout(timeout); input.signal?.removeEventListener('abort', abort); }
}
