import type { Dataset, Evidence, Filters, GraphNode, PathTimeline, View } from './graph-data';
export type WorkerRequest =
  | { kind: 'demo'; version: number; count: number; filters: Filters }
  | { kind: 'import'; version: number; text?: string; file?: File; filters: Filters }
  | { kind: 'filter'; version: number; filters: Filters }
  | { kind: 'drill'; version: number; group: string; filters: Filters }
  | { kind: 'ego'; version: number; id: string; depth: number; filters: Filters }
  | { kind: 'reset'; version: number; filters: Filters }
  | { kind: 'detail'; id: string }
  | { kind: 'edgeDetail'; id: string }
  | { kind: 'path'; source: string; target: string }
  | { kind: 'broker' }
  | { kind: 'louvain'; active: boolean }
  | { kind: 'impact'; version: number; ids: string[] }
  | { kind: 'layout'; running: boolean };
export type WorkerResponse =
  | { kind: 'view'; view: View }
  | { kind: 'positions'; version: number; positions: Float32Array }
  | { kind: 'layout'; running: boolean }
  | { kind: 'detail'; node?: GraphNode; evidence: Evidence[] }
  | { kind: 'edgeDetail'; id: string; evidence: Evidence[] }
  | { kind: 'path'; source: string; target: string; nodes: string[] | null;
      edges: { id: string; source: string; target: string; type: string }[] | null; hops: number;
      timeline: PathTimeline | null }
  | { kind: 'broker'; version: number; brokers: { id: string; label: string; share: number }[]; notice?: string }
  | { kind: 'louvain'; active: boolean; communities: number; notice?: string }
  | { kind: 'ego'; id: string; depth: number; size: number | null }
  | { kind: 'impact'; version: number; removed: number; components: number; largest: number; isolated: number }
  | { kind: 'error'; version: number; message: string };
export type SourceData = Dataset;
