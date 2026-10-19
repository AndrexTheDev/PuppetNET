'use client';
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import Sigma from 'sigma';
import { MultiDirectedGraph } from 'graphology';
import { COLORS, hash, type View } from './graph-data';
export interface NetworkHandle { positions: (version: number, values: Float32Array) => void; zoom: (factor: number) => void; reset: () => void }
interface Props { view: View | null; onSelect: (id: string, group: boolean) => void; onRatio: (ratio: number) => void }
const nextFrame = () => new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
const NetworkGraph = forwardRef<NetworkHandle, Props>(function NetworkGraph({ view, onSelect, onRatio }, ref) {
  const host = useRef<HTMLDivElement>(null), sigma = useRef<Sigma | null>(null), current = useRef<{ version: number; graph: MultiDirectedGraph; keys: string[] } | null>(null);
  const callbacks = useRef({ onSelect, onRatio }); callbacks.current = { onSelect, onRatio };
  const [error, setError] = useState(''), [building, setBuilding] = useState(false);
  useImperativeHandle(ref, () => ({
    positions(version, values) {
      const active = current.current;
      if (!active || active.version !== version || values.length !== active.keys.length * 2) return;
      let index = 0;
      active.graph.updateEachNodeAttributes((_id, attr) => {
        const x = values[index++], y = values[index++];
        return Number.isFinite(x) && Number.isFinite(y) ? { ...attr, x, y } : attr;
      }, { attributes: ['x', 'y'] });
      if (host.current) host.current.dataset.layoutUpdates = String(Number(host.current.dataset.layoutUpdates ?? 0) + 1);
    },
    zoom(factor) { const camera = sigma.current?.getCamera(); if (camera) camera.animate({ ratio: camera.ratio * factor }, { duration: 200 }); },
    reset() { sigma.current?.getCamera().reset({ duration: 200 }); }
  }), []);
  useEffect(() => {
    if (!host.current) return;
    let renderer: Sigma;
    try {
      if (!document.createElement('canvas').getContext('webgl2')) throw new Error('WebGL 2 ist nicht verfügbar. Bitte GPU-Beschleunigung aktivieren. Die Datenliste und Karte bleiben nutzbar.');
      renderer = new Sigma(new MultiDirectedGraph(), host.current, {
        settings: { hideEdgesOnMove: true, hideLabelsOnMove: true, labelDensity: .35, labelRenderedSizeThreshold: 8, minCameraRatio: .015, maxCameraRatio: 8, enableNodeDrag: false, allowInvalidContainer: true, itemSizesReference: 'screen' },
        styles: {
          nodes: { x: { attribute: 'x' }, y: { attribute: 'y' }, size: { whenState: 'isHovered', then: 7, else: { attribute: 'size', defaultValue: 3 } }, color: { attribute: 'color', defaultValue: '#60dfd0' }, label: { attribute: 'label' }, labelColor: '#dbe7f7', labelSize: 12, labelFont: 'sans-serif' },
          edges: { color: { attribute: 'color', defaultValue: '#243b50' }, size: { attribute: 'size', defaultValue: .5 } },
        },
      });
      sigma.current = renderer;
      renderer.on('clickNode', ({ node }) => callbacks.current.onSelect(node, Boolean(current.current?.graph.getNodeAttribute(node, 'group'))));
      renderer.getCamera().on('updated', ({ ratio }) => callbacks.current.onRatio(ratio));
      const canvas = host.current.querySelector('canvas');
      const lost = (event: Event) => { event.preventDefault(); setError('WebGL-Kontext verloren. Seite neu laden oder weniger Daten anzeigen.'); };
      canvas?.addEventListener('webglcontextlost', lost);
      const observer = new ResizeObserver(() => renderer.resize()); observer.observe(host.current);
      return () => { observer.disconnect(); canvas?.removeEventListener('webglcontextlost', lost); renderer.kill(); sigma.current = null; current.current = null; };
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'WebGL konnte nicht gestartet werden.'); }
  }, []);
  useEffect(() => {
    if (!view || !sigma.current) return;
    let cancelled = false; setBuilding(true);
    current.current = null;
    (async () => {
      const graph = new MultiDirectedGraph();
      // Yield between batches: imported 100k graphs must not be one synchronous React render.
      for (let i = 0; i < view.nodes.length; i += 2000) {
        if (cancelled) return;
        for (const n of view.nodes.slice(i, i + 2000)) graph.addNode(n.id, {
          x: n.x + ((hash(n.id) % 100) - 50) * .0001, y: n.y,
          label: n.label, color: COLORS[n.type], group: n.group,
          size: n.group ? Math.min(19, 4 + Math.log2((n.count ?? 1) + 1)) : view.nodes.length > 10_000 ? 1.5 : 3.2,
        });
        await nextFrame();
      }
      for (let i = 0; i < view.edges.length; i += 4000) {
        if (cancelled) return;
        for (const e of view.edges.slice(i, i + 4000)) graph.addDirectedEdgeWithKey(e.id, e.source, e.target, { size: .25 + e.weight * .5, color: e.type === 'EVIDENCED_BY' ? '#7769c3' : '#243b50', label: e.type });
        await nextFrame();
      }
      if (cancelled || !sigma.current) return;
      sigma.current.setGraph(graph);
      sigma.current.setSetting('renderLabels', graph.order < 10_000);
      current.current = { graph, version: view.version, keys: view.nodes.map(n => n.id) };
      if (host.current) { host.current.dataset.renderedNodes = String(graph.order); host.current.dataset.renderedEdges = String(graph.size); }
      setBuilding(false);
    })().catch(cause => { if (!cancelled) { setError(String(cause)); setBuilding(false); } });
    return () => { cancelled = true; };
  }, [view]);
  return <div className="network-stage">
    <div ref={host} className="sigma-host" data-testid="sigma-host" aria-label="Interaktiver Netzwerkgraph. Knoten anklicken oder über die Datenliste auswählen." role="img" />
    {error && <div className="stage-error" role="alert">{error}</div>}
    {building && <div className="stage-loading" role="status">GPU-Daten werden vorbereitet …</div>}
    {!error && !building && view?.nodes.length === 0 && <div className="stage-error">Keine Knoten für diese Filter.</div>}
    <div className="graph-caption"><span className="status-dot" /> SIGMA.JS 4 · WEBGL 2 <span>Layout im Web Worker</span></div>
  </div>;
});
export default NetworkGraph;
