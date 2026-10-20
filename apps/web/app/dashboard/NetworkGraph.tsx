'use client';
import palette from '../../src/styles/palette.json';
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react';
import Sigma from 'sigma';
import { MultiDirectedGraph } from 'graphology';
import { COLORS, communityColor, hash, type View } from './graph-data';
import { describeGraphNode, graphInstructions, graphKeyAction, nextIndex } from '../../lib/graph-keyboard';
import { useLanguage, useT } from './i18n';
export interface NetworkHandle {
  positions: (version: number, values: Float32Array) => void;
  zoom: (factor: number) => void;
  reset: () => void;
  focus: (id: string) => void;
}
export interface PathHighlight { nodes: string[]; edges: string[] }
interface Props { view: View | null; onSelect: (id: string, group: boolean) => void; onSelectEdge: (id: string) => void; onRatio: (ratio: number) => void; onClear: () => void;
  colorMode?: 'type' | 'community'; path?: PathHighlight | null }
const nextFrame = () => new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
const NetworkGraph = forwardRef<NetworkHandle, Props>(function NetworkGraph({ view, onSelect, onSelectEdge, onRatio, onClear, colorMode = 'type', path = null }, ref) {
  const host = useRef<HTMLDivElement>(null), sigma = useRef<Sigma | null>(null), current = useRef<{ version: number; graph: MultiDirectedGraph; keys: string[] } | null>(null);
  const callbacks = useRef({ onSelect, onSelectEdge, onRatio, onClear }); callbacks.current = { onSelect, onSelectEdge, onRatio, onClear };
  const viewRef = useRef<View | null>(view); viewRef.current = view;
  const focusRef = useRef('');
  const pendingFocus = useRef<string | null>(null);
  const highlight = useRef<{ id: string; size: unknown; color: unknown } | null>(null);
  const pathRef = useRef<PathHighlight | null>(path); pathRef.current = path;
  const pathMark = useRef<{ nodes: { id: string; size: unknown; color: unknown }[]; edges: { key: string; size: unknown; color: unknown }[] }>({ nodes: [], edges: [] });
  const [error, setError] = useState(''), [building, setBuilding] = useState(false);
  const [focused, setFocused] = useState(''), [announcement, setAnnouncement] = useState('');
  // Wave 6 i18n: refs keep the mount-time and callback closures stable while
  // the visible strings follow the active language.
  const { lang } = useLanguage(); const t = useT();
  const tRef = useRef(t); tRef.current = t;
  const langRef = useRef(lang); langRef.current = lang;

  // Pathfinder highlight: same restore-then-merge pattern as the keyboard focus.
  // The visible attributes change, graph data and layout stay untouched.
  const applyPath = useCallback(() => {
    const renderer = sigma.current, graph = current.current?.graph;
    if (!renderer || !graph) return;
    for (const item of pathMark.current.nodes) if (graph.hasNode(item.id)) graph.mergeNodeAttributes(item.id, { size: item.size, color: item.color });
    for (const item of pathMark.current.edges) if (graph.hasEdge(item.key)) graph.mergeEdgeAttributes(item.key, { size: item.size, color: item.color });
    pathMark.current = { nodes: [], edges: [] };
    const active = pathRef.current;
    if (active) {
      for (const id of active.nodes) if (graph.hasNode(id)) {
        const attr = graph.getNodeAttributes(id);
        pathMark.current.nodes.push({ id, size: attr.size, color: attr.color });
        graph.mergeNodeAttributes(id, { color: palette.gold, size: Number(attr.size ?? 3) * 1.7 });
      }
      for (const key of active.edges) if (graph.hasEdge(key)) {
        const attr = graph.getEdgeAttributes(key);
        pathMark.current.edges.push({ key, size: attr.size, color: attr.color });
        graph.mergeEdgeAttributes(key, { color: palette.gold, size: Number(attr.size ?? .5) * 4 });
      }
    }
    renderer.refresh();
  }, []);
  useEffect(() => { applyPath(); }, [path, applyPath]);

  const centerOn = useCallback((id: string) => {
    const renderer = sigma.current, graph = current.current?.graph;
    if (!renderer || !graph || !graph.hasNode(id)) return;
    const { x, y } = graph.getNodeAttributes(id);
    if (Number.isFinite(x) && Number.isFinite(y)) renderer.getCamera().animate({ x, y }, { duration: 180 });
  }, []);

  const focusNode = useCallback((id: string, announce = true) => {
    const nodes = viewRef.current?.nodes ?? [];
    const index = nodes.findIndex(node => node.id === id);
    if (index < 0) return;
    pendingFocus.current = id;
    focusRef.current = id;
    setFocused(id);
    if (announce) setAnnouncement(describeGraphNode(nodes[index], index, nodes.length, langRef.current));
    centerOn(id);
  }, [centerOn]);

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
    reset() { sigma.current?.getCamera().reset({ duration: 200 }); },
    focus(id) { focusNode(id); },
  }), [focusNode]);

  useEffect(() => {
    if (!host.current) return;
    let renderer: Sigma;
    try {
      if (!document.createElement('canvas').getContext('webgl2')) throw new Error(tRef.current('network.webgl2'));
      renderer = new Sigma(new MultiDirectedGraph(), host.current, {
        settings: { hideEdgesOnMove: true, hideLabelsOnMove: true, labelDensity: .35, labelRenderedSizeThreshold: 8, minCameraRatio: .015, maxCameraRatio: 8, enableNodeDrag: false, allowInvalidContainer: true, itemSizesReference: 'screen' },
        styles: {
          nodes: { x: { attribute: 'x' }, y: { attribute: 'y' }, size: { whenState: 'isHovered', then: 7, else: { attribute: 'size', defaultValue: 3 } }, color: { attribute: 'color', defaultValue: palette.green }, label: { attribute: 'label' }, labelColor: palette.white, labelSize: 12, labelFont: 'sans-serif' },
          edges: { color: { attribute: 'color', defaultValue: palette['forest-700'] }, size: { attribute: 'size', defaultValue: .5 } },
        },
      });
      sigma.current = renderer;
      renderer.on('clickNode', ({ node }) => {
        focusRef.current = node;
        setFocused(node);
        callbacks.current.onSelect(node, Boolean(current.current?.graph.getNodeAttribute(node, 'group')));
      });
      renderer.on('clickEdge', ({ edge }) => callbacks.current.onSelectEdge(edge));
      renderer.getCamera().on('updated', ({ ratio }) => callbacks.current.onRatio(ratio));
      const canvas = host.current.querySelector('canvas');
      const lost = (event: Event) => { event.preventDefault(); setError(tRef.current('network.contextLost')); };
      canvas?.addEventListener('webglcontextlost', lost);
      const observer = new ResizeObserver(() => renderer.resize()); observer.observe(host.current);
      return () => { observer.disconnect(); canvas?.removeEventListener('webglcontextlost', lost); renderer.kill(); sigma.current = null; current.current = null; };
    } catch (cause) { setError(cause instanceof Error ? cause.message : tRef.current('network.launchFailed')); }
  }, []);

  // The keyboard cursor is drawn, not scrolled: the focused node is enlarged and
  // whitened through its display attributes, then restored. The graph data and
  // the layout are untouched.
  useEffect(() => {
    const renderer = sigma.current, graph = current.current?.graph;
    if (!renderer || !graph) return;
    const previous = highlight.current;
    if (previous && graph.hasNode(previous.id)) {
      graph.mergeNodeAttributes(previous.id, { size: previous.size, color: previous.color });
    }
    highlight.current = null;
    if (focused && graph.hasNode(focused)) {
      const attributes = graph.getNodeAttributes(focused);
      highlight.current = { id: focused, size: attributes.size, color: attributes.color };
      graph.mergeNodeAttributes(focused, { size: Number(attributes.size ?? 3) * 2.4, color: palette.white });
    }
    renderer.refresh();
  }, [focused]);

  useEffect(() => {
    if (!view || !sigma.current) return;
    let cancelled = false; setBuilding(true);
    current.current = null;
    focusRef.current = ''; highlight.current = null; setFocused(''); setAnnouncement('');
    (async () => {
      const graph = new MultiDirectedGraph();
      // Yield between batches: imported 100k graphs must not be one synchronous React render.
      for (let i = 0; i < view.nodes.length; i += 2000) {
        if (cancelled) return;
        for (const n of view.nodes.slice(i, i + 2000)) graph.addNode(n.id, {
          x: n.x + ((hash(n.id) % 100) - 50) * .0001, y: n.y,
          label: n.label, color: colorMode === 'community' && n.community ? communityColor(n.community) : COLORS[n.type], group: n.group,
          size: n.group ? Math.min(19, 4 + Math.log2((n.count ?? 1) + 1)) : view.nodes.length > 10_000 ? 1.5 : 3.2,
        });
        await nextFrame();
      }
      for (let i = 0; i < view.edges.length; i += 4000) {
        if (cancelled) return;
        for (const e of view.edges.slice(i, i + 4000)) graph.addDirectedEdgeWithKey(e.id, e.source, e.target, { size: .25 + e.weight * .5, color: e.type === 'EVIDENCED_BY' ? palette['gold-muted'] : palette['forest-700'], label: e.type });
        await nextFrame();
      }
      if (cancelled || !sigma.current) return;
      sigma.current.setGraph(graph);
      sigma.current.setSetting('renderLabels', graph.order < 10_000);
      current.current = { graph, version: view.version, keys: view.nodes.map(n => n.id) };
      if (host.current) { host.current.dataset.renderedNodes = String(graph.order); host.current.dataset.renderedEdges = String(graph.size); }
      setBuilding(false);
      const waiting = pendingFocus.current;
      if (waiting) { pendingFocus.current = null; focusNode(waiting); }
      applyPath();
    })().catch(cause => { if (!cancelled) { setError(String(cause)); setBuilding(false); } });
    return () => { cancelled = true; };
  }, [view, focusNode, colorMode, applyPath]);

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    const action = graphKeyAction(event.key);
    if (action.type === 'none') return;
    const nodes = viewRef.current?.nodes ?? [];
    if (!nodes.length) return;
    event.preventDefault();
    const index = Math.max(0, nodes.findIndex(node => node.id === focusRef.current));
    switch (action.type) {
      case 'move': focusNode(nodes[nextIndex(index, action.delta, nodes.length)].id); break;
      case 'first': focusNode(nodes[0].id); break;
      case 'last': focusNode(nodes[nodes.length - 1].id); break;
      case 'select': {
        const node = nodes[index];
        callbacks.current.onSelect(node.id, Boolean(node.group));
        setAnnouncement(tRef.current('network.detailsOpened', { label: node.label }));
        break;
      }
      case 'clear':
        focusRef.current = '';
        setFocused('');
        callbacks.current.onClear();
        setAnnouncement(tRef.current('network.cleared'));
        break;
      case 'zoom': {
        const camera = sigma.current?.getCamera();
        if (camera) camera.animate({ ratio: camera.ratio * action.factor }, { duration: 160 });
        setAnnouncement(action.factor < 1 ? tRef.current('network.zoomedIn') : tRef.current('network.zoomedOut'));
        break;
      }
      case 'reset':
        sigma.current?.getCamera().reset({ duration: 160 });
        setAnnouncement(tRef.current('network.viewReset'));
        break;
    }
  }

  return <div className="network-stage">
    <div
      ref={host}
      className="sigma-host"
      data-testid="sigma-host"
      role="application"
      tabIndex={0}
      aria-roledescription={t('network.role')}
      aria-describedby="graph-keyboard-help"
      onKeyDown={onKeyDown}
      onFocus={() => { if (!focusRef.current && viewRef.current?.nodes.length) focusNode(viewRef.current.nodes[0].id); }}
    />
    <p id="graph-keyboard-help" className="visually-hidden">{graphInstructions(lang)}</p>
    <p className="visually-hidden" role="status" aria-live="polite" aria-atomic="true">{announcement}</p>
    {focused && <div className="graph-focus-badge" aria-hidden="true">{t('network.focusBadge', { label: viewRef.current?.nodes.find(node => node.id === focused)?.label ?? '' })}</div>}
    {error && <div className="stage-error" role="alert">{error}</div>}
    {building && <div className="stage-loading" role="status">{t('network.preparing')}</div>}
    {!error && !building && view?.nodes.length === 0 && <div className="stage-error">{t('network.noNodes')}</div>}
    <div className="graph-caption"><span className="status-dot" /> {t('network.engine')} <span>{t('network.captionKeys')}</span></div>
  </div>;
});
export default NetworkGraph;
