import type { Dataset, Evidence, Filters, GraphNode, View } from './graph-data';
export type WorkerRequest =
  | { kind: 'demo'; version: number; count: number; filters: Filters }
  | { kind: 'import'; version: number; text?: string; file?: File; filters: Filters }
  | { kind: 'filter'; version: number; filters: Filters }
  | { kind: 'drill'; version: number; group: string; filters: Filters }
  | { kind: 'reset'; version: number; filters: Filters }
  | { kind: 'detail'; id: string }
  | { kind: 'layout'; running: boolean };
export type WorkerResponse =
  | { kind: 'view'; view: View }
  | { kind: 'positions'; version: number; positions: Float32Array }
  | { kind: 'layout'; running: boolean }
  | { kind: 'detail'; node?: GraphNode; evidence: Evidence[] }
  | { kind: 'error'; version: number; message: string };
export type SourceData = Dataset;
