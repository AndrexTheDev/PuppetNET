'use client';
import dynamic from 'next/dynamic';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import EntityTable, { type TableRow } from './EntityTable';
import { COLORS, TYPES, LIMITS, decodeViewState, edgesToCSV, encodeViewState, nodesToCSV, rankSearch, resolveNodeRef, viewToJSON, type Evidence, type Filters, type GraphEdge, type GraphNode, type View, type ViewUrlState } from './graph-data';
import type { NetworkHandle, PathHighlight } from './NetworkGraph';
import type { WorkerRequest, WorkerResponse } from './protocol';
import { loadStaticGraph, requestResearch, type Standby } from '../../src/lib/dataFetcher';
import StandbyPanel, { LoadingStage } from './BootState';
import SidePanel from './SidePanel';
import { downloadText } from './download';
import { useLanguage, useT } from './i18n';
import { NUMBER_LOCALE } from '../../lib/i18n-strings';
function DynamicStage({ k }: { k: 'webgl' | 'map' }) { const t = useT(); return <div className="stage-loading">{k === 'webgl' ? t('dash.loadingWebgl') : t('dash.loadingMap')}</div>; }
const NetworkGraph = dynamic(() => import('./NetworkGraph'), { ssr: false, loading: () => <DynamicStage k="webgl" /> });
const MapModule = dynamic(() => import('./MapModule'), { ssr: false, loading: () => <DynamicStage k="map" /> });
const initialFilters: Filters = { confidence: 0, weight: 0, types: [...TYPES], lod: true, ratio: 1 };
interface PathResult { ids: string[]; labels: string[]; types: string[]; edgeIds: string[]; hops: number }
export default function Dashboard() {
  const worker = useRef<Worker | null>(null), network = useRef<NetworkHandle>(null), version = useRef(0), selectedId = useRef<string | null>(null), selectedEdgeId = useRef<string | null>(null);
  // Shareable permalink state: mode, query, focus, community, time window, color mode.
  const initialUrl = useRef<ViewUrlState>(typeof window === 'undefined' ? {} : decodeViewState(window.location.search));
  const [view, setView] = useState<View | null>(null), [busy, setBusy] = useState(true);
  const [filters, setFilters] = useState<Filters>(() => ({ ...initialFilters, ...(initialUrl.current.community ? { community: initialUrl.current.community } : {}), ...(initialUrl.current.from ? { from: initialUrl.current.from } : {}), ...(initialUrl.current.to ? { to: initialUrl.current.to } : {}) }));
  const filtersRef = useRef(filters); filtersRef.current = filters;
  const busyRef = useRef(busy); busyRef.current = busy;
  const viewLatest = useRef<View | null>(null); viewLatest.current = view;
  const [mode, setMode] = useState<'network' | 'map' | 'list'>(initialUrl.current.mode ?? 'list');
  const [colorMode, setColorMode] = useState<'type' | 'community'>(initialUrl.current.color ?? 'type');
  const [announcement, setAnnouncement] = useState('');
  const [selected, setSelected] = useState<GraphNode | null>(null), [evidence, setEvidence] = useState<Evidence[]>([]);
  const [selectedEdge, setSelectedEdge] = useState<GraphEdge | null>(null), [edgeEvidence, setEdgeEvidence] = useState<Evidence[]>([]);
  const [error, setError] = useState(''), [detailError, setDetailError] = useState(''), [layoutRunning, setLayoutRunning] = useState(false);
  const [sourceLabel, setSourceLabel] = useState('Bereitschaftsmodus'), [sourceForm, setSourceForm] = useState(false), [base, setBase] = useState(''), [token, setToken] = useState('');
  const [search, setSearch] = useState(initialUrl.current.q ?? ''), [listPage, setListPage] = useState(0);
  const [researchQuery, setResearchQuery] = useState(''), [researchBusy, setResearchBusy] = useState(false);
  const [pathSource, setPathSource] = useState(''), [pathTarget, setPathTarget] = useState('');
  const [pathResult, setPathResult] = useState<PathResult | null>(null), [pathError, setPathError] = useState('');
  const [egoQuery, setEgoQuery] = useState(''), [egoDepth, setEgoDepth] = useState(2), [egoStatus, setEgoStatus] = useState('');
  const [brokers, setBrokers] = useState<{ id: string; label: string; share: number }[] | null>(null);
  const [analysisNotice, setAnalysisNotice] = useState('');
  const [louvainActive, setLouvainActive] = useState(false), [louvainCount, setLouvainCount] = useState(0);
  const [standby, setStandby] = useState<Standby | null>(null);
  const loadingApi = useRef<AbortController | null>(null), loadingDetail = useRef<AbortController | null>(null);
  const staticRequest = useRef<AbortController | null>(null), reloadStatic = useRef<(() => void) | null>(null);
  const pendingSource = useRef('Bereitschaftsmodus'), committedSource = useRef('Bereitschaftsmodus');
  const [notice, setNotice] = useState('');
  const initialFocus = useRef(initialUrl.current.focus ?? null);
  // Wave 6 i18n: refs keep the mount-effect and worker-handler closures stable
  // while the visible strings follow the active language.
  const { lang, setLang } = useLanguage(); const t = useT();
  const number = (n = 0) => n.toLocaleString(NUMBER_LOCALE[lang]);
  const tRef = useRef(t); tRef.current = t;
  const langRef = useRef(lang); langRef.current = lang;
  const changeMode = useCallback((next: 'network' | 'map' | 'list') => {
    setMode(next);
    if (next !== 'network') worker.current?.postMessage({ kind: 'layout', running: false });
    setAnnouncement(next === 'list'
      ? tRef.current('dash.annList')
      : next === 'map' ? tRef.current('dash.annMap') : tRef.current('dash.annNetwork'));
  }, []);
  const clearSelected = useCallback(() => { selectedId.current = null; setSelected(null); setEvidence([]); selectedEdgeId.current = null; setSelectedEdge(null); setEdgeEvidence([]); loadingDetail.current?.abort(); setDetailError(''); }, []);
  const send = useCallback((request: WorkerRequest) => worker.current?.postMessage(request), []);
  const requestView = useCallback((kind: 'filter' | 'reset' = 'filter') => { setBusy(true); setError(''); clearSelected(); setPathResult(null); setPathError(''); setBrokers(null); setAnalysisNotice(''); if (kind === 'reset') setEgoStatus(''); send({ kind, version: ++version.current, filters: filtersRef.current }); }, [send, clearSelected]);
  useEffect(() => {
    const instance = new Worker(new URL('./graph.worker.ts', import.meta.url)); worker.current = instance;
    instance.onmessage = ({ data }: MessageEvent<WorkerResponse>) => {
      if (data.kind === 'view' && data.view.version === version.current) { setView(data.view); setBusy(false); setSourceLabel(pendingSource.current); committedSource.current = pendingSource.current; setListPage(0); }
      if (data.kind === 'positions') network.current?.positions(data.version, data.positions);
      if (data.kind === 'layout') setLayoutRunning(data.running);
      if (data.kind === 'detail' && data.node?.id === selectedId.current) { setSelected(data.node); setEvidence(old => [...new Map([...old, ...data.evidence].map(d => [d.id, d])).values()]); }
      if (data.kind === 'edgeDetail' && data.id === selectedEdgeId.current) setEdgeEvidence(data.evidence);
      if (data.kind === 'path') {
        if (!data.nodes || !data.edges) {
          setPathResult(null);
          setPathError(tRef.current('dash.pathNone'));
          setAnnouncement(tRef.current('dash.annPathNone'));
          return;
        }
        const labelOf = (id: string) => viewLatest.current?.nodes.find(n => n.id === id)?.label ?? id;
        setPathResult({ ids: data.nodes, labels: data.nodes.map(labelOf), types: data.edges.map(e => e.type), edgeIds: data.edges.map(e => e.id), hops: data.hops });
        setPathError('');
        setAnnouncement(tRef.current('dash.annPathFound', { hops: data.hops, from: labelOf(data.nodes[0]), to: labelOf(data.nodes[data.nodes.length - 1]) }));
        return;
      }
      if (data.kind === 'broker') {
        if (data.version !== version.current) return; // stale answer for an older slice
        setBrokers(data.brokers); setAnalysisNotice(data.notice ?? '');
        setAnnouncement(data.notice ? tRef.current('dash.annBrokerLimited', { notice: data.notice }) : tRef.current('dash.annBrokerDone', { count: data.brokers.length }));
        return;
      }
      if (data.kind === 'louvain') {
        setLouvainActive(data.active); setLouvainCount(data.communities);
        if (data.notice) setAnalysisNotice(data.notice);
        setAnnouncement(data.active ? tRef.current('dash.annLouvainOn', { count: data.communities }) : tRef.current('dash.annLouvainOff'));
        return;
      }
      if (data.kind === 'ego') {
        // Wave 7: the worker answers after the rebuilt view — size null means
        // the center is not part of the filtered slice (honest absence).
        const label = viewLatest.current?.nodes.find(n => n.id === data.id)?.label ?? data.id;
        const message = data.size === null
          ? tRef.current('dash.egoMissing')
          : tRef.current('dash.egoActive', { size: data.size, depth: data.depth, s: data.depth === 1 ? '' : 's', label });
        setEgoStatus(message); setAnnouncement(message);
        return;
      }
      if (data.kind === 'error' && data.version === version.current) { setError(data.message); setBusy(false); pendingSource.current = committedSource.current; }
    };
    instance.onerror = event => { setError(tRef.current('dash.workerError', { message: event.message || tRef.current('dash.workerFallback') })); setBusy(false); };
    const loadPublished = () => {
      staticRequest.current?.abort();
      const controller = new AbortController(); staticRequest.current = controller;
      const bootVersion = ++version.current;
      setBusy(true); setError(''); clearSelected(); setStandby(null); setPathResult(null); setPathError('');
      setBrokers(null); setAnalysisNotice(''); setLouvainActive(false); setLouvainCount(0);
      loadStaticGraph({ signal: controller.signal }).then(data => {
        if (controller.signal.aborted || version.current !== bootVersion || worker.current !== instance) return;
        if (data.source === 'static') {
          pendingSource.current = data.message; setNotice(tRef.current('dash.cdnNotice'));
          instance.postMessage({ kind: 'import', text: JSON.stringify(data.graph), version: bootVersion, filters: filtersRef.current } satisfies WorkerRequest);
        } else {
          // No usable publication (missing/offline/invalid or an empty dataset
          // after a full withdrawal): stay in standby instead of silently
          // swapping in fictional demo data. The user decides explicitly.
          setStandby({ reason: data.reason, message: data.message });
          setBusy(false); setNotice('');
          setSourceLabel('Bereitschaftsmodus'); pendingSource.current = committedSource.current = 'Bereitschaftsmodus';
          setAnnouncement(tRef.current('dash.annStandby'));
        }
      }).catch(() => { /* caller cancellation/unmount: no replacement of newer user data */ });
    };
    reloadStatic.current = loadPublished;
    loadPublished();
    return () => { staticRequest.current?.abort(); reloadStatic.current = null; instance.terminate(); worker.current = null; loadingApi.current?.abort(); loadingDetail.current?.abort(); };

  }, []);
  const boot = useRef(true);
  useEffect(() => { if (boot.current) { boot.current = false; return; } const timeout = setTimeout(() => requestView(), 180); return () => clearTimeout(timeout); }, [filters, requestView]);
  // Shareable permalinks: replaceState mirrors the investigation state into the
  // URL without adding history entries (bound values only, no free text blobs).
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const query = encodeViewState({ mode, q: search || undefined, focus: selected?.id || undefined, community: filters.community, from: filters.from, to: filters.to, color: colorMode });
    window.history.replaceState(null, '', query ? `${window.location.pathname}?${query}` : window.location.pathname);
  }, [mode, search, selected, filters.community, filters.from, filters.to, colorMode]);
  const onRatio = useCallback((ratio: number) => { if (busyRef.current) return; const bucket = ratio >= .55 ? 1 : ratio >= .18 ? .35 : .1; setFilters(f => f.ratio === bucket ? f : { ...f, ratio: bucket }); }, []);
  const select = useCallback((id: string, group = false) => {
    loadingDetail.current?.abort(); setDetailError(''); setEvidence([]); selectedId.current = id;
    selectedEdgeId.current = null; setSelectedEdge(null); setEdgeEvidence([]);
    const node = view?.nodes.find(n => n.id === id);
    setSelected(node ?? null);
    setAnnouncement(node ? tRef.current('dash.annDetailsOpened', { label: node.label }) : tRef.current('network.detailsOpenedPlain'));
    if (group) return;
    send({ kind: 'detail', id });

  }, [view, send]);
  // Wave 5 edge provenance: clicking an edge shows exactly the documents that
  // carry this one connection — the audit criterion of the niche.
  const selectEdge = useCallback((id: string) => {
    loadingDetail.current?.abort(); setDetailError(''); setEvidence([]);
    selectedId.current = null; setSelected(null);
    const edge = view?.edges.find(e => e.id === id);
    if (!edge) return;
    selectedEdgeId.current = id; setSelectedEdge(edge); setEdgeEvidence([]);
    const labelOf = (key: string) => view?.nodes.find(n => n.id === key)?.label ?? key;
    if (edge.type === 'AGGREGATED') {
      setAnnouncement(tRef.current('dash.annEdgeAggregated', { count: edge.count?.toLocaleString(NUMBER_LOCALE[langRef.current]) ?? 0 }));
      return;
    }
    setAnnouncement(tRef.current('dash.annEdge', { type: edge.type, source: labelOf(edge.source), target: labelOf(edge.target) }));
    send({ kind: 'edgeDetail', id });
  }, [view, send]);
  function demo(count: number) {
    staticRequest.current?.abort(); pendingSource.current = count > 900 ? `Fiktiver Lasttest · ${number(count * 4)} Elemente` : 'Fiktive Demo';
    clearSelected(); setBusy(true); setError(''); setNotice(''); setStandby(null); setPathResult(null); setPathError(''); setBrokers(null); setAnalysisNotice(''); setLouvainActive(false); setLouvainCount(0); send({ kind: 'demo', count, version: ++version.current, filters: filtersRef.current });
  }
  function importFile(file: File) {
    if (file.size > LIMITS.bytes) { setError(t('dash.errImportSize')); return; }
    staticRequest.current?.abort(); pendingSource.current = file.name; setBusy(true); setError(''); clearSelected(); setPathResult(null); setPathError(''); setBrokers(null); setAnalysisNotice(''); setLouvainActive(false); setLouvainCount(0);
    send({ kind: 'import', file, version: ++version.current, filters: filtersRef.current });
  }
  async function startResearch() {
    if (researchBusy) return;
    setError(''); loadingApi.current?.abort(); const controller = new AbortController(); loadingApi.current = controller;
    setResearchBusy(true);
    try {
      await requestResearch({ workerUrl: base, token, query: researchQuery, signal: controller.signal });
      setNotice(t('dash.researchAccepted'));
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : t('dash.errResearchRejected'));
    } finally { if (loadingApi.current === controller) setResearchBusy(false); }
  }
  // Fuzzy-ranked search: exact substring first, then subsequence matches, stable order.
  const list = useMemo(() => rankSearch(view?.nodes ?? [], search, Number.MAX_SAFE_INTEGER), [view, search]);
  const communities = useMemo(() => {
    const set = new Set<string>();
    for (const n of view?.nodes ?? []) if (n.community && set.size < 100) set.add(n.community);
    return [...set].sort();
  }, [view]);
  const pathHighlight = useMemo<PathHighlight | null>(() => pathResult && view && !view.stats.grouped ? { nodes: pathResult.ids, edges: pathResult.edgeIds } : null, [pathResult, view]);
  const requestPath = useCallback(() => {
    setPathError(''); setPathResult(null);
    const nodes = viewLatest.current?.nodes ?? [];
    if (viewLatest.current?.stats.grouped) { setPathError(tRef.current('dash.pathNeedsSingle')); return; }
    const source = resolveNodeRef(nodes, pathSource), target = resolveNodeRef(nodes, pathTarget);
    if (!source || !target) { setPathError(tRef.current('dash.pathNotFound')); return; }
    if (source.id === target.id) { setPathError(tRef.current('dash.pathSame')); return; }
    send({ kind: 'path', source: source.id, target: target.id });
  }, [pathSource, pathTarget, send]);
  // Wave 7 ego expansion: resolve the center against the visible slice (name or
  // id, fuzzy like the pathfinder); an LoD group expands around its first
  // member, because the group itself has no identity in the published dataset.
  const requestEgo = useCallback((raw?: string) => {
    const nodes = viewLatest.current?.nodes ?? [];
    const ref = resolveNodeRef(nodes, (raw ?? egoQuery).trim());
    if (!ref) {
      const message = tRef.current('dash.egoMissing');
      setEgoStatus(message); setAnnouncement(message);
      return;
    }
    const center = ref.group && ref.examples?.length ? ref.examples[0] : ref.id;
    setEgoStatus('');
    send({ kind: 'ego', version: ++version.current, id: center, depth: egoDepth, filters: filtersRef.current });
  }, [egoQuery, egoDepth, send]);
  const pageSize = 50;
  const shown = list.slice(listPage * pageSize, (listPage + 1) * pageSize);
  const degrees = useMemo(() => {
    const map = new Map<string, number>();
    for (const edge of view?.edges ?? []) {
      map.set(edge.source, (map.get(edge.source) ?? 0) + 1);
      map.set(edge.target, (map.get(edge.target) ?? 0) + 1);
    }
    return map;
  }, [view]);
  const tableRows: TableRow[] = shown.map(n => ({ ...n, degree: degrees.get(n.id) ?? 0 }));
  const timelineMax = useMemo(() => (view?.timeline ?? []).reduce((max, bucket) => Math.max(max, bucket.count), 0), [view]);
  useEffect(() => {
    const wanted = initialFocus.current;
    if (!wanted || !view) return;
    initialFocus.current = null;
    if (view.nodes.some(n => n.id === wanted)) {
      select(wanted);
      requestAnimationFrame(() => network.current?.focus(wanted));
    }
  }, [view, select]);
  return <main id="main-content" tabIndex={-1} className="dashboard-shell" aria-busy={busy} lang={lang === 'de' ? undefined : 'en'}>
    <p className="visually-hidden" role="status" aria-live="polite" aria-atomic="true">{announcement}</p>
    <header className="dashboard-header"><a className="brand" href="/"><img className="brand-art" src="/media/logo.webp" width="52" height="52" alt="" /><div>PUPPET<span>NET</span><small>INTELLIGENCE WORKSPACE</small></div></a><div className="header-path">Workspace <span>/</span> <strong>{t('dash.headerSection')}</strong></div><div className="header-status"><span className="status-dot" /> {t('dash.headerStatus')} <span className="version-tag">2.0</span></div></header>
    <section className="workspace-intro"><div><span className="eyebrow">{t('dash.introEyebrow')}</span><h1>{t('dash.introTitle')}</h1><p>{t('dash.introText')}</p></div><div className="intro-controls"><button type="button" className="outline-button lang-toggle" onClick={() => setLang(lang === 'de' ? 'en' : 'de')} aria-label={t('dash.langSwitchAria')}>{t('dash.langSwitch')}</button><button onClick={() => setSourceForm(s => !s)} className="outline-button">{t('boot.researchButton')}</button></div></section>
    <div className="source-strip"><span className="source-pill">{sourceLabel.includes('Fiktiv') ? t('dash.pillDemo') : t('dash.pillData')}</span><strong>{sourceLabel}</strong><span>{t('dash.stripNote')}</span><button onClick={() => demo(900)}>{t('dash.demoShort')}</button><button disabled={busy} onClick={() => demo(30_000)}>{t('dash.stressTest')}</button><label className="import-button">{t('dash.importJson')}<input type="file" accept="application/json,.json" onChange={e => { const file = e.target.files?.[0]; if (file) importFile(file); e.target.value = ''; }} /></label></div>
    <p className="cdn-mode" role="note">{t('dash.cdnText')} <button type="button" disabled={busy} onClick={() => reloadStatic.current?.()}>{t('dash.cdnReload')}</button></p>
    {sourceForm && <form className="connection-form" onSubmit={e => { e.preventDefault(); startResearch(); }}>
      <p>{t('dash.formIntro')}</p>
      <label>{t('dash.searchTerm')}<input required maxLength={120} value={researchQuery} onChange={e => setResearchQuery(e.target.value)} /></label>
      <label>{t('dash.workerUrlLabel')}<input required type="url" value={base} onChange={e => setBase(e.target.value)} placeholder="https://dein-suchrelay.workers.dev" /></label>
      <label>{t('dash.tokenLabel')}<input required type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} /></label>
      <button className="primary-button" disabled={researchBusy} type="submit">{researchBusy ? t('dash.submitting') : t('dash.requestResearch')}</button>
      <p>{t('dash.formFoot')}</p>
    </form>}

    {(error || notice || view?.stats.truncated) && <div className={`dashboard-alert ${error ? 'error' : ''}`} role={error ? 'alert' : 'status'}>{error || notice || t('dash.alertTruncated')}</div>}
    {standby
      ? <StandbyPanel standby={standby} busy={busy} onDemo={() => demo(900)} onResearch={() => setSourceForm(true)} onReload={() => reloadStatic.current?.()} />
      : !view ? <LoadingStage />
      : <div className="workspace-grid">
      <aside className="filter-panel"><span className="eyebrow">{t('dash.filterEyebrow')}</span><div className="section-heading"><h2>{t('dash.filtersTitle')}</h2><button className="text-button" onClick={() => { filtersRef.current = initialFilters; setFilters(initialFilters); requestView('reset'); }}>{t('dash.resetButton')}</button></div>
        <label className="slider-label" htmlFor="confidence"><span>Confidence <strong>{filters.confidence.toFixed(2)}</strong></span><input id="confidence" disabled={busy} type="range" min="0" max="1" step="0.01" value={filters.confidence} onChange={e => setFilters(f => ({ ...f, confidence: Number(e.target.value) }))} /><small>{t('dash.confidenceHint')}</small></label>
        <label className="slider-label" htmlFor="weight"><span>{t('dash.weightLabel')} <strong>{filters.weight.toFixed(2)}</strong></span><input id="weight" disabled={busy} type="range" min="0" max="1" step="0.01" value={filters.weight} onChange={e => setFilters(f => ({ ...f, weight: Number(e.target.value) }))} /><small>{t('dash.weightHint')}</small></label>
        <h3>{t('dash.typesTitle')}</h3><div className="type-filters">{TYPES.map(type => <label key={type}><input type="checkbox" disabled={busy} checked={filters.types.includes(type)} onChange={e => setFilters(f => ({ ...f, types: e.target.checked ? [...f.types, type] : f.types.filter(t => t !== type) }))} /><span className="type-dot" style={{ background: COLORS[type] }} />{type}</label>)}</div>
        {communities.length > 0 && <div className="community-filter"><h3>Community</h3>
          <label className="visually-hidden" htmlFor="community-select">{t('dash.communityFilterLabel')}</label>
          <select id="community-select" disabled={busy} value={filters.community ?? ''} onChange={e => setFilters(f => ({ ...f, community: e.target.value || undefined }))}>
            <option value="">{t('dash.allCommunities')}</option>
            {communities.map(name => <option key={name} value={name}>{name}</option>)}
          </select>
          <p>{t('dash.communityText')}</p></div>}
        <div className="timeline-filter"><h3>{t('dash.timeTitle')}</h3>
          <div className="timeline-row">
            <label htmlFor="time-from">{t('dash.fromLabel')}<input id="time-from" type="date" disabled={busy} value={filters.from ?? ''} max={filters.to} onChange={e => setFilters(f => ({ ...f, from: e.target.value || undefined }))} /></label>
            <label htmlFor="time-to">{t('dash.toLabel')}<input id="time-to" type="date" disabled={busy} value={filters.to ?? ''} min={filters.from} onChange={e => setFilters(f => ({ ...f, to: e.target.value || undefined }))} /></label>
          </div>
          {(filters.from || filters.to) && <button type="button" className="text-button" onClick={() => setFilters(f => ({ ...f, from: undefined, to: undefined }))}>{t('dash.clearTimeFilter')}</button>}
          <p>{t('dash.timeNote')}</p></div>
        <div className="lod-setting"><label><input type="checkbox" disabled={busy} checked={filters.lod} onChange={e => { if (!e.target.checked && (view?.stats.filteredNodes ?? 0) > 20_000 && !window.confirm(tRef.current('dash.lodConfirm'))) return; setFilters(f => ({ ...f, lod: e.target.checked })); }} /> {t('dash.lodLabel')}</label><p>{t('dash.lodNote')}</p></div>
        {view?.analytics && <div className="analytics-panel"><div className="section-heading"><h3>{t('dash.analyticsTitle')}</h3></div>
          <dl className="analytics-facts"><div><dt>Communities</dt><dd>{number(view.analytics.communities)}</dd></div><div><dt>{t('dash.density')}</dt><dd>{(view.analytics.density * 100).toFixed(2)} %</dd></div>{view.analytics.temporalRange && <div><dt>{t('dash.rangeLabel')}</dt><dd>{view.analytics.temporalRange.first} → {view.analytics.temporalRange.last}</dd></div>}</dl>
          {view.timeline && view.timeline.length > 0 && <>
            <h4 className="hub-heading">{t('dash.timelineTitle')}</h4>
            <ol className="timeline-bars">{view.timeline.map(bucket => <li key={bucket.month}><span className="timeline-month">{bucket.month}</span><span className="timeline-bar" aria-hidden="true" style={{ width: `${timelineMax ? Math.max(2, Math.round((bucket.count / timelineMax) * 100)) : 0}%` }} /><strong>{number(bucket.count)}</strong></li>)}</ol>
            <p className="muted">{t('dash.timelineNote')}</p>
          </>}
          {view.analytics.hubs.length > 0 && <><h4 className="hub-heading">{t('dash.topHubs')}</h4><ol className="hub-list">{view.analytics.hubs.map(hub => <li key={hub.id}><button type="button" className="text-button" onClick={() => { if (mode !== 'network') changeMode('network'); select(hub.id); requestAnimationFrame(() => network.current?.focus(hub.id)); }}>{hub.name}</button><span className="hub-rank">{(hub.rank * 100).toFixed(1)} %</span></li>)}</ol></>}
          {view.insights && view.insights.length > 0 && <>
            <h4 className="hub-heading">{t('dash.insightsTitle')}</h4>
            <ul className="insight-list">{view.insights.map((insight, index) => (
              <li key={`${insight.rule}-${index}`}>
                <span className="insight-rule">{insight.rule}</span>
                <strong>{insight.title}</strong>
                {insight.description && <p className="insight-description">{insight.description}</p>}
                <span className="insight-entities">{insight.entities.map(entity => (
                  <button key={entity.id} type="button" className="text-button" onClick={() => { if (mode !== 'network') changeMode('network'); select(entity.id); requestAnimationFrame(() => network.current?.focus(entity.id)); }}>{entity.name}</button>
                ))}</span>
              </li>
            ))}</ul>
            <p className="muted">{t('dash.insightsNote')}</p>
          </>}
        </div>}
        <div className="algorithm-panel"><div className="section-heading"><h3>{t('dash.algoTitle')}</h3></div>
          <p className="muted">{t('dash.algoText')}</p>
          <div className="algorithm-actions">
            <button type="button" disabled={busy} onClick={() => { setAnalysisNotice(''); setBrokers(null); send({ kind: 'broker' }); }}>{t('dash.brokerRun')}</button>
            <button type="button" aria-pressed={louvainActive} disabled={busy} onClick={() => { setAnalysisNotice(''); send({ kind: 'louvain', active: !louvainActive }); }}>{louvainActive ? t('dash.louvainReset') : t('dash.louvainRun')}</button>
          </div>
          {analysisNotice && <p className="inline-warning" role="status">{analysisNotice}</p>}
          {louvainActive && <p className="muted" role="status">{t('dash.louvainStatus', { count: louvainCount })}</p>}
          {brokers !== null && brokers.length > 0 && <><h4 className="hub-heading">{t('dash.topBrokers')}</h4><ol className="hub-list">{brokers.map(broker => <li key={broker.id}><button type="button" className="text-button" onClick={() => { if (mode !== 'network') changeMode('network'); select(broker.id); requestAnimationFrame(() => network.current?.focus(broker.id)); }}>{broker.label}</button><span className="hub-rank">{(broker.share * 100).toFixed(1)} %</span></li>)}</ol><p className="muted">{t('dash.brokerNote')}</p></>}
          {brokers !== null && brokers.length === 0 && !analysisNotice && <p className="muted" role="status">{t('dash.noBrokers')}</p>}
        </div>
        <div className="pathfinder"><div className="section-heading"><h3>{t('dash.pathTitle')}</h3></div>
          <p className="muted">{t('dash.pathText')}</p>
          <label htmlFor="path-source">{t('dash.fromLabel')}<input id="path-source" list="pathfinder-options" autoComplete="off" value={pathSource} onChange={e => setPathSource(e.target.value)} placeholder={t('dash.pathPlaceholder')} /></label>
          <label htmlFor="path-target">{t('dash.pathTo')}<input id="path-target" list="pathfinder-options" autoComplete="off" value={pathTarget} onChange={e => setPathTarget(e.target.value)} placeholder={t('dash.pathPlaceholder')} /></label>
          <datalist id="pathfinder-options">{(view?.nodes ?? []).slice(0, 200).map(n => <option key={n.id} value={n.label}>{n.id}</option>)}</datalist>
          <button type="button" className="primary-button" disabled={busy} onClick={requestPath}>{t('dash.pathCompute')}</button>
          {pathError && <p className="inline-warning" role="status">{pathError}</p>}
          {pathResult && <div className="path-result" role="status" aria-label={t('dash.pathFoundLabel')}>
            <strong>{t('dash.hopCount', { hops: pathResult.hops, s: pathResult.hops === 1 ? '' : 's' })}</strong>
            <ol className="path-hops">{pathResult.labels.map((label, index) => <li key={`${pathResult.ids[index]}:${index}`}>
              <button type="button" className="text-button" onClick={() => { select(pathResult.ids[index]); requestAnimationFrame(() => network.current?.focus(pathResult.ids[index])); }}>{label}</button>
              {index < pathResult.types.length && <span className="hop-edge" aria-label={t('dash.viaAria', { type: pathResult.types[index] })}> —{pathResult.types[index]}→</span>}
            </li>)}</ol>
            {view?.stats.grouped && <p className="muted">{t('dash.pathGroupedNote')}</p>}
            <button type="button" className="text-button" onClick={() => { setPathResult(null); setPathError(''); }}>{t('dash.pathClear')}</button>
          </div>}
        </div>
        <div className="ego-panel"><div className="section-heading"><h3>{t('dash.egoTitle')}</h3></div>
          <p className="muted">{t('dash.egoText')}</p>
          <label htmlFor="ego-center">{t('dash.egoCenter')}<input id="ego-center" list="pathfinder-options" autoComplete="off" value={egoQuery} onChange={e => setEgoQuery(e.target.value)} placeholder={t('dash.pathPlaceholder')} /></label>
          <label htmlFor="ego-depth">{t('dash.egoDepth')}<select id="ego-depth" value={egoDepth} onChange={e => setEgoDepth(Number(e.target.value))}><option value={1}>1</option><option value={2}>2</option><option value={3}>3</option></select></label>
          <div className="algorithm-actions">
            <button type="button" className="primary-button" disabled={busy} onClick={() => requestEgo()}>{t('dash.egoRun')}</button>
            {view?.stats.focused && <button type="button" className="text-button" onClick={() => requestView('reset')}>{t('dash.showAll')}</button>}
          </div>
          {egoStatus && <p className="muted" role="status">{egoStatus}</p>}
        </div>
        <div className="filter-foot"><span>○</span><p>{t('dash.footNote')}</p></div>
      </aside>
      <section className="visualization-panel" aria-label={t('dash.vizAria')}><div className="visualization-toolbar"><div className="view-tabs" role="group" aria-label={t('dash.tabsAria')}><button aria-pressed={mode === 'network'} aria-controls="visualization-stage" onClick={() => changeMode('network')}>◈ {t('dash.tabNetwork')}</button><button aria-pressed={mode === 'map'} aria-controls="visualization-stage" onClick={() => changeMode('map')}>◎ {t('dash.tabMap')}</button><button aria-pressed={mode === 'list'} aria-controls="visualization-stage" onClick={() => changeMode('list')}>☷ {t('dash.tabList')}</button></div><button type="button" className="outline-button color-toggle" aria-pressed={colorMode === 'community'} disabled={mode !== 'network'} onClick={() => setColorMode(c => c === 'type' ? 'community' : 'type')}>{t('dash.colorLabel', { mode: colorMode === 'community' ? 'Community' : t('dash.colorType') })}</button><span className="live-indicator" role="status">{busy ? t('dash.liveProcessing') : layoutRunning ? t('dash.liveLayout') : t('dash.liveReady')}</span></div>
        <div className="stats-bar"><div><strong>{number(view?.stats.filteredNodes)}</strong><span>{t('dash.statNodesFilter')}</span></div><div><strong>{number(view?.stats.filteredEdges)}</strong><span>{t('dash.statEdges')}</span></div><div><strong>{number(view?.nodes.length)}</strong><span>{t('dash.statGpu')}</span></div><span className="lod-pill">{view?.stats.level ?? t('dash.loadingLevel')}</span></div>
        <details className="keyboard-help"><summary>{t('dash.keyTab')}</summary><ul>
          <li dangerouslySetInnerHTML={{ __html: t('dash.kbd1') }} />
          <li dangerouslySetInnerHTML={{ __html: t('dash.kbd2') }} />
          <li dangerouslySetInnerHTML={{ __html: t('dash.kbd3') }} />
          <li dangerouslySetInnerHTML={{ __html: t('dash.kbd4') }} />
          <li dangerouslySetInnerHTML={{ __html: t('dash.kbd5') }} />
        </ul></details>
        <div id="visualization-stage" className={mode === 'network' ? 'graph-pane' : 'graph-pane hidden-pane'}>{mode === 'network' && <NetworkGraph ref={network} view={view} onSelect={select} onSelectEdge={selectEdge} onRatio={onRatio} onClear={clearSelected} colorMode={colorMode} path={pathHighlight} />}<div className="graph-tools"><button aria-label={t('dash.zoomInAria')} onClick={() => network.current?.zoom(.65)}>+</button><button aria-label={t('dash.zoomOutAria')} onClick={() => network.current?.zoom(1.5)}>−</button><button aria-label={t('dash.fitAria')} onClick={() => network.current?.reset()}>⌖</button><button onClick={() => send({ kind: 'layout', running: !layoutRunning })}>{layoutRunning ? 'Pause' : 'Layout'}</button></div></div>
        {mode === 'map' && <><MapModule points={view?.mapPoints ?? []} onSelect={id => select(id)} /><div className="data-footnote">{t('dash.mapFootnote', { count: number(view?.stats.locatedNodes) })}</div></>}
        {mode === 'list' && <EntityTable rows={tableRows} total={list.length} page={listPage} pageSize={pageSize} query={search} onQuery={value => { setSearch(value); setListPage(0); }} onPage={setListPage} onSelect={select} onFocusGraph={id => { changeMode('network'); select(id); requestAnimationFrame(() => network.current?.focus(id)); }} />}
        <div className="visualization-footer"><span><span className="status-dot" /> {t('dash.nodesLoaded', { count: number(view?.stats.totalNodes) })}</span><span className="view-export"><button type="button" onClick={() => downloadText('puppetnet-knoten.csv', nodesToCSV(view?.nodes ?? []), 'text/csv;charset=utf-8')}>{t('dash.nodesCsv')}</button><button type="button" onClick={() => downloadText('puppetnet-kanten.csv', edgesToCSV(view?.edges ?? []), 'text/csv;charset=utf-8')}>{t('dash.edgesCsv')}</button><button type="button" onClick={() => downloadText('puppetnet-ansicht.json', viewToJSON({ nodes: view?.nodes ?? [], edges: view?.edges ?? [], analytics: view?.analytics, insights: view?.insights }), 'application/json')}>JSON ↓</button></span><span>{view?.stats.focused ? <button onClick={() => requestView('reset')}>{t('dash.showAll')}</button> : t('dash.clickHintFoot')}</span></div>
      </section>
      <SidePanel node={selected} evidence={evidence} edge={selectedEdge} edgeEvidence={edgeEvidence} edgeLabels={{
        source: selectedEdge ? view?.nodes.find(n => n.id === selectedEdge.source)?.label ?? selectedEdge.source : '',
        target: selectedEdge ? view?.nodes.find(n => n.id === selectedEdge.target)?.label ?? selectedEdge.target : '',
      }} detailError={detailError} onEgo={id => requestEgo(id)} onClose={() => { clearSelected(); setAnnouncement(t('dash.detailsClosed')); }} onDrill={group => { clearSelected(); setBusy(true); send({ kind: 'drill', group, version: ++version.current, filters: filtersRef.current }); }} />
    </div>}
    <footer className="dashboard-footer"><span>PUPPETNET / OPEN-SOURCE INTELLIGENCE</span><span>Sigma.js 4 · Graphology · Leaflet · OpenStreetMap</span><a className="footer-legal" href="/legal/">{t('dash.legalLink')}</a></footer>
  </main>;
}
