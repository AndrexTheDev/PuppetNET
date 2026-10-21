'use client';
import dynamic from 'next/dynamic';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import EntityTable, { type TableRow } from './EntityTable';
import { COLORS, TYPES, LIMITS, NOTE_STORAGE_KEY, WATCH_STORAGE_KEY, FRESH_WINDOWS, DEFAULT_FRESH_MONTHS, riskToCSV, watchToCSV, watchReport, watchChanges, watchChangesToCSV, entityBrief, watchBrief, caseArchive, zipStore, evidenceToCSV, entityTimeline, readWatchlist, toggleWatch, diffToCSV, searchCase, caseReport, decodeViewState, edgesToCSV, encodeViewState, monthWindow, nextPlaybackMonth, nodesToCSV, caseBundle, compareDuplicates, mergeNotes, parseCaseNotes, provenanceGaps, readCaseBundle, searchNoteIndex, removeSavedView, upsertSavedView, readSavedViews, SAVED_VIEW_STORAGE_KEY, normalizeNote, rankSearch, readNotes, resolveNodeRef, viewToJSON, type Evidence, type Filters, type GraphEdge, type GraphNode, type PathTimeline, type View, type ViewUrlState, type DatasetDiff, type CaseHit, type EntityTimeline } from './graph-data';
import type { NetworkHandle, PathHighlight } from './NetworkGraph';
import type { WorkerRequest, WorkerResponse } from './protocol';
import { loadStaticGraph, requestResearch, type Standby } from '../../src/lib/dataFetcher';
import StandbyPanel, { LoadingStage } from './BootState';
import SidePanel from './SidePanel';
import { downloadBinary, downloadText } from './download';
import { useLanguage, useT } from './i18n';
import { NUMBER_LOCALE } from '../../lib/i18n-strings';
function DynamicStage({ k }: { k: 'webgl' | 'map' }) { const t = useT(); return <div className="stage-loading">{k === 'webgl' ? t('dash.loadingWebgl') : t('dash.loadingMap')}</div>; }
const NetworkGraph = dynamic(() => import('./NetworkGraph'), { ssr: false, loading: () => <DynamicStage k="webgl" /> });
const MapModule = dynamic(() => import('./MapModule'), { ssr: false, loading: () => <DynamicStage k="map" /> });
const initialFilters: Filters = { confidence: 0, weight: 0, types: [...TYPES], lod: true, ratio: 1 };
interface PathResult { ids: string[]; labels: string[]; types: string[]; edgeIds: string[]; hops: number; timeline: PathTimeline | null }
export default function Dashboard() {
  const worker = useRef<Worker | null>(null), network = useRef<NetworkHandle>(null), version = useRef(0), selectedId = useRef<string | null>(null), selectedEdgeId = useRef<string | null>(null);
  // Shareable permalink state: mode, query, focus, community, time window, color mode.
  const initialUrl = useRef<ViewUrlState>(typeof window === 'undefined' ? {} : decodeViewState(window.location.search));
  const [view, setView] = useState<View | null>(null), [busy, setBusy] = useState(true);
  const [filters, setFilters] = useState<Filters>(() => ({ ...initialFilters, ...(initialUrl.current.community ? { community: initialUrl.current.community } : {}), ...(initialUrl.current.from ? { from: initialUrl.current.from } : {}), ...(initialUrl.current.to ? { to: initialUrl.current.to } : {}), ...(initialUrl.current.sources ? { sources: initialUrl.current.sources } : {}), ...(initialUrl.current.outlet ? { outlet: initialUrl.current.outlet } : {}), ...(initialUrl.current.fresh ? { fresh: initialUrl.current.fresh } : {}), ...(initialUrl.current.dispute ? { dispute: true } : {}), ...(initialUrl.current.weak ? { weak: true } : {}) }));
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
  const [diff, setDiff] = useState<DatasetDiff | null>(null), [baselineName, setBaselineName] = useState('');
  const [caseQuery, setCaseQuery] = useState('');
  const [analysisNotice, setAnalysisNotice] = useState('');
  // Wave 13: entity resolution and failure simulation are case actions, not
  // background analytics - both work on the slice the investigator sees.
  const [dupeCompareIds, setDupeCompareIds] = useState<string[]>([]);
  const [impactIds, setImpactIds] = useState<string[]>([]);
  const [impactResult, setImpactResult] = useState<{ removed: number; components: number; largest: number; isolated: number } | null>(null);
  // Wave 14: saved views (case sessions) and the evidence export live here too.
  const [savedViews, setSavedViews] = useState<{ name: string; query: string; saved: string }[]>([]);
  const [viewName, setViewName] = useState('');
  const [viewsStatus, setViewsStatus] = useState('');
  const viewsReady = useRef(false);
  const [notesImportStatus, setNotesImportStatus] = useState('');
  const [noteQuery, setNoteQuery] = useState('');
  const [bundleStatus, setBundleStatus] = useState('');
  const bundleInput = useRef<HTMLInputElement | null>(null);
  const notesImport = useRef<HTMLInputElement | null>(null);
  // Wave 12: cumulative playback — the month list is frozen while it runs
  // (a time filter shrinks the visible timeline, so it cannot be re-derived).
  const [playing, setPlaying] = useState(false);
  const [playbackMonths, setPlaybackMonths] = useState<string[]>([]);
  const playbackMonthsRef = useRef<string[]>([]); playbackMonthsRef.current = playbackMonths;
  const [louvainActive, setLouvainActive] = useState(false), [louvainCount, setLouvainCount] = useState(0);
  const [standby, setStandby] = useState<Standby | null>(null);
  const loadingApi = useRef<AbortController | null>(null), loadingDetail = useRef<AbortController | null>(null);
  const staticRequest = useRef<AbortController | null>(null), reloadStatic = useRef<(() => void) | null>(null);
  const pendingSource = useRef('Bereitschaftsmodus'), committedSource = useRef('Bereitschaftsmodus');
  const [notice, setNotice] = useState('');
  // Wave 11: research notes live in localStorage only — loaded once, persisted
  // after every change, never sent to a server.
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [watch, setWatch] = useState<string[]>([]), watchReady = useRef(false);
  const notesReady = useRef(false);
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
  const requestView = useCallback((kind: 'filter' | 'reset' = 'filter') => { setBusy(true); setError(''); clearSelected(); setPathResult(null); setPathError(''); setBrokers(null); setAnalysisNotice(''); if (kind === 'reset') { setEgoStatus(''); setPlaying(false); setPlaybackMonths([]); setDupeCompareIds([]); setImpactIds([]); setImpactResult(null); } send({ kind, version: ++version.current, filters: filtersRef.current }); }, [send, clearSelected]);
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
        setPathResult({ ids: data.nodes, labels: data.nodes.map(labelOf), types: data.edges.map(e => e.type), edgeIds: data.edges.map(e => e.id), hops: data.hops, timeline: data.timeline ?? null });
        setPathError('');
        setAnnouncement(tRef.current('dash.annPathFound', { hops: data.hops, from: labelOf(data.nodes[0]), to: labelOf(data.nodes[data.nodes.length - 1]) }));
        return;
      }
      if (data.kind === 'diff') {
        if (data.version !== version.current) return; // stale answer for an older comparison
        setDiff(data.diff); setBusy(false);
        return;
      }
      if (data.kind === 'evidenceCsv') {
        if (data.version !== version.current) return; // stale answer for an older slice
        downloadText('puppetnet-belege.csv', data.csv, 'text/csv;charset=utf-8');
        setAnnouncement(tRef.current('dash.annEvidence', { count: number(data.count) }));
        return;
      }
      if (data.kind === 'impact') {
        if (data.version !== version.current) return; // stale answer for an older slice
        setImpactResult({ removed: data.removed, components: data.components, largest: data.largest, isolated: data.isolated });
        setAnnouncement(tRef.current('dash.annImpact', { parts: data.components, largest: data.largest }));
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
  const stepPlayback = useCallback((direction: 1 | -1) => {
    let months = playbackMonthsRef.current;
    if (!months.length) {
      months = (viewLatest.current?.timeline ?? []).map(bucket => bucket.month);
      if (months.length < 2) { setPlaying(false); setAnnouncement(tRef.current('dash.playEmpty')); return; }
      playbackMonthsRef.current = months; setPlaybackMonths(months);
    }
    const current = filtersRef.current.to ? filtersRef.current.to.slice(0, 7) : undefined;
    const next = nextPlaybackMonth(months, current, direction);
    const window = next ? monthWindow(next) : null;
    if (!next || !window) { setPlaying(false); setAnnouncement(tRef.current('dash.annPlaybackEnd')); return; }
    setFilters(f => ({ ...f, from: undefined, to: window.to }));
    setAnnouncement(tRef.current(direction > 0 ? 'dash.annPlayback' : 'dash.annPlaybackBack', { month: next }));
  }, []);
  const stepPlaybackRef = useRef(stepPlayback); stepPlaybackRef.current = stepPlayback;
  useEffect(() => {
    if (!playing) return;
    const timer = setInterval(() => stepPlaybackRef.current(1), 1_400);
    return () => clearInterval(timer);
  }, [playing]);
  const startPlayback = () => {
    const months = (viewLatest.current?.timeline ?? []).map(bucket => bucket.month);
    if (months.length < 2) { setAnnouncement(tRef.current('dash.playEmpty')); return; }
    playbackMonthsRef.current = months; setPlaybackMonths(months);
    const current = filtersRef.current.to ? filtersRef.current.to.slice(0, 7) : undefined;
    if (!current || !months.includes(current)) {
      const first = monthWindow(months[0]);
      if (first) setFilters(f => ({ ...f, from: undefined, to: first.to }));
      setAnnouncement(tRef.current('dash.annPlayback', { month: months[0] }));
    }
    setPlaying(true);
  };
  useEffect(() => {
    try { setNotes(readNotes(window.localStorage.getItem(NOTE_STORAGE_KEY))); } catch { /* storage blocked: notes stay in memory */ }
    notesReady.current = true;
  }, []);
  useEffect(() => {
    try { setSavedViews(readSavedViews(window.localStorage.getItem(SAVED_VIEW_STORAGE_KEY))); } catch { /* storage blocked: sessions stay in memory */ }
    viewsReady.current = true;
  }, []);
  useEffect(() => {
    if (!viewsReady.current) return;
    try {
      if (savedViews.length) window.localStorage.setItem(SAVED_VIEW_STORAGE_KEY, JSON.stringify(savedViews));
      else window.localStorage.removeItem(SAVED_VIEW_STORAGE_KEY);
    } catch { /* private mode / quota: the session keeps working */ }
  }, [savedViews]);
  useEffect(() => {
    if (!notesReady.current) return;
    try {
      if (Object.keys(notes).length) window.localStorage.setItem(NOTE_STORAGE_KEY, JSON.stringify(notes));
      else window.localStorage.removeItem(NOTE_STORAGE_KEY);
    } catch { /* private mode / quota: the session keeps working, nothing is lost silently in the UI */ }
  }, [notes]);
  // Wave 27: the watchlist is case work in this browser only — capped, sanitized
  // on read, and written back on every change so a session survives a reload.
  useEffect(() => {
    try { setWatch(readWatchlist(window.localStorage.getItem(WATCH_STORAGE_KEY))); } catch { /* storage blocked: the list stays in memory */ }
    watchReady.current = true;
  }, []);
  useEffect(() => {
    if (!watchReady.current) return;
    try {
      if (watch.length) window.localStorage.setItem(WATCH_STORAGE_KEY, JSON.stringify(watch));
      else window.localStorage.removeItem(WATCH_STORAGE_KEY);
    } catch { /* private mode / quota: the session keeps working */ }
  }, [watch]);
  // Shareable permalinks: replaceState mirrors the investigation state into the
  // URL without adding history entries (bound values only, no free text blobs).
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const query = encodeViewState({ mode, q: search || undefined, focus: selected?.id || undefined, community: filters.community, from: filters.from, to: filters.to, color: colorMode, sources: filters.sources, outlet: filters.outlet, fresh: filters.fresh, dispute: filters.dispute, weak: filters.weak });
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
  function loadBaseline(file: File) {
    if (file.size > LIMITS.bytes) { setError(t('dash.errImportSize')); return; }
    setBusy(true); setError(''); setBaselineName(file.name);
    send({ kind: 'diff', version: ++version.current, file });
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
  const caseHits = useMemo<CaseHit[]>(() => view
    ? searchCase(caseQuery, { nodes: view.nodes, conflicts: view.conflicts, risk: view.risk, sourceRegistry: view.sourceRegistry, notes })
    : [], [view, caseQuery, notes]);
  const watched = useMemo(() => view
    ? watchReport(watch, view.nodes, view.edges, evidence, { risk: view.risk, conflicts: view.conflicts })
    : null, [watch, view, evidence]);
  // Wave 31: the selected entity's own chronology — computed from the same slice the
  // panels show, so on-screen timeline and brief can never disagree.
  const selectedTimeline = useMemo<EntityTimeline | undefined>(() => {
    if (!view || !selected || selected.group) return undefined;
    return entityTimeline(selected.id, view.nodes, view.edges, evidence, view.conflicts ?? []);
  }, [view, selected, evidence]);
  const watchDigest = useMemo(() => diff && view ? watchChanges(watch, diff, view.nodes) : [], [diff, watch, view]);
  const communities = useMemo(() => {
    const set = new Set<string>();
    for (const n of view?.nodes ?? []) if (n.community && set.size < 100) set.add(n.community);
    return [...set].sort();
  }, [view]);
  const pathHighlight = useMemo<PathHighlight | null>(() => pathResult && view && !view.stats.grouped ? { nodes: pathResult.ids, edges: pathResult.edgeIds } : null, [pathResult, view]);
  // Wave 29: the hand-over documents. They are built from the same view data the
  // panels read, so a brief cannot disagree with what is on screen.
  const briefContext = useCallback(() => ({
    nodes: viewLatest.current?.nodes ?? [], edges: viewLatest.current?.edges ?? [], evidence,
    risk: viewLatest.current?.risk, conflicts: viewLatest.current?.conflicts, notes,
  }), [evidence, notes]);
  const exportEntityBrief = useCallback(() => {
    const node = selectedId.current; if (!node) return;
    const context = briefContext();
    const changes = watchDigest.filter(change => change.id === node);
    downloadText(`puppetnet-kurzbericht-${node.replace(/[^\w.-]+/g, '_').slice(0, 60)}.md`,
      entityBrief({ ...context, id: node, watched: watch.includes(node), changes,
        timeline: viewLatest.current ? entityTimeline(node, viewLatest.current.nodes, viewLatest.current.edges, evidence, viewLatest.current.conflicts ?? []) : undefined },
        new Date().toISOString(), lang), 'text/markdown;charset=utf-8');
  }, [briefContext, watch, watchDigest, evidence, lang]);
  const exportWatchBrief = useCallback(() => {
    downloadText('puppetnet-beobachtungen-kurzberichte.md',
      watchBrief(watch, briefContext(), new Date().toISOString(), lang), 'text/markdown;charset=utf-8');
  }, [briefContext, watch, lang]);
  // Wave 30: one archive built from the very functions the single buttons use, so
  // the ZIP can never contain a different report than the one on screen.
  const exportArchive = useCallback(() => {
    if (!view) return;
    const generated = new Date().toISOString();
    const report = caseReport({ stats: view.stats, nodes: view.nodes, edges: view.edges, analytics: view.analytics, insights: view.insights, timeline: view.timeline, duplicates: view.duplicates, vulnerability: view.vulnerability, gaps: view.gaps, sharedSources: view.sharedSources, conflicts: view.conflicts, freshness: view.freshness, risk: view.risk, notes }, generated, lang);
    const files = caseArchive({
      report,
      ...(watch.length ? { watchBrief: watchBrief(watch, briefContext(), generated, lang) } : {}),
      nodesCsv: nodesToCSV(view.nodes),
      edgesCsv: edgesToCSV(view.edges),
      ...(view.sharedSources || view.gaps ? { evidenceCsv: evidenceToCSV({ nodes: view.nodes, edges: view.edges, evidence }, view.nodes, view.edges) } : {}),
      ...(watched ? { watchCsv: watchToCSV(watched) } : {}),
      ...(watchDigest.length ? { changesCsv: watchChangesToCSV(watchDigest) } : {}),
      generated,
    });
    downloadBinary('puppetnet-fallakte.zip', zipStore(files), 'application/zip');
  }, [view, evidence, notes, watch, watchDigest, watched, briefContext, lang]);
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
  const dupeComparison = useMemo(() => (dupeCompareIds.length >= 2 && view && !view.stats.grouped ? compareDuplicates({ nodes: view.nodes, edges: view.edges, evidence: [] }, dupeCompareIds) : null), [dupeCompareIds, view]);
  const toggleImpact = (id: string) => {
    const next = impactIds.includes(id) ? impactIds.filter(current => current !== id) : [...impactIds, id].slice(0, 3);
    setImpactIds(next); setImpactResult(null);
    if (next.length >= 2) send({ kind: 'impact', version: version.current, ids: next });
  };
  const saveCurrentView = () => {
    const name = viewName.trim();
    if (!name) { setViewsStatus(t('dash.viewsNeedName')); return; }
    const query = encodeViewState({ mode, q: search || undefined, community: filters.community, from: filters.from, to: filters.to, color: colorMode, focus: selected?.id, sources: filters.sources, outlet: filters.outlet, fresh: filters.fresh, dispute: filters.dispute, weak: filters.weak });
    setSavedViews(list => upsertSavedView(list, name, query || 'mode=list', new Date().toISOString()));
    setViewName('');
    setViewsStatus(t('dash.viewsSaved', { name }));
  };
  const applySavedView = (entry: { name: string; query: string }) => {
    const state = decodeViewState(entry.query);
    if (state.mode) changeMode(state.mode);
    setSearch(state.q ?? ''); setListPage(0);
    setColorMode(state.color ?? 'type');
    setFilters(f => ({ ...f, community: state.community, from: state.from, to: state.to }));
    if (state.focus && viewLatest.current?.nodes.some(node => node.id === state.focus)) {
      select(state.focus);
      requestAnimationFrame(() => network.current?.focus(state.focus!));
    }
    setViewsStatus(t('dash.viewsApplied', { name: entry.name }));
    setAnnouncement(t('dash.annViewsApplied', { name: entry.name }));
  };
  const importCaseNotes = async (file: File | null | undefined) => {
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) { setNotesImportStatus(t('dash.notesImportTooBig')); return; }
    try {
      const parsed = parseCaseNotes(await file.text());
      const count = Object.keys(parsed).length;
      if (count === 0) { setNotesImportStatus(t('dash.notesImportEmpty')); return; }
      // The merge runs on the state this render sees (a user action, never a
      // background update) so the summary is exact instead of optimistic.
      const merged = mergeNotes(notes, parsed);
      setNotes(merged.notes);
      setNotesImportStatus(t('dash.notesImported', { added: number(merged.added), unchanged: number(merged.unchanged), skipped: number(merged.skipped) }));
    } catch { setNotesImportStatus(t('dash.notesImportError')); }
    finally { if (notesImport.current) notesImport.current.value = ''; }
  };
  const noteHits = useMemo(() => (view && Object.keys(notes).length ? searchNoteIndex(view.nodes, notes, noteQuery) : []), [notes, noteQuery, view]);
  const exportCaseBundle = () => {
    if (!view) return;
    const report = caseReport({ stats: view.stats, nodes: view.nodes, edges: view.edges, analytics: view.analytics, insights: view.insights, timeline: view.timeline, duplicates: view.duplicates, vulnerability: view.vulnerability, gaps: view.gaps, sharedSources: view.sharedSources, conflicts: view.conflicts, freshness: view.freshness, risk: view.risk, notes }, new Date().toISOString(), lang);
    downloadText('puppetnet-fallakte.json', caseBundle({ notes, views: savedViews, report, generated: new Date().toISOString() }), 'application/json');
    setAnnouncement(t('dash.annBundle', { notes: number(Object.keys(notes).length), views: number(savedViews.length) }));
  };
  const importCaseBundle = async (file: File | null | undefined) => {
    if (!file) return;
    if (file.size > 20 * 1024 * 1024) { setBundleStatus(t('dash.notesImportTooBig')); return; }
    try {
      const bundle = readCaseBundle(await file.text());
      if (!bundle) { setBundleStatus(t('dash.bundleImportInvalid')); return; }
      const merged = mergeNotes(notes, bundle.notes);
      setNotes(merged.notes);
      const addedViews = bundle.views.length;
      setSavedViews(current => {
        let list = current;
        for (const entry of bundle.views) list = upsertSavedView(list, entry.name, entry.query, entry.saved);
        return list;
      });
      setBundleStatus(t('dash.bundleImported', {
        added: number(merged.added), skipped: number(merged.skipped), views: number(addedViews),
        report: bundle.report ? t('dash.bundleReportYes') : t('dash.bundleReportNo'),
      }));
    } catch { setBundleStatus(t('dash.notesImportError')); }
    finally { if (bundleInput.current) bundleInput.current.value = ''; }
  };
  const gaps = useMemo(() => (view ? view.gaps ?? provenanceGaps(view.nodes, view.edges) : null), [view]);
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
    <div className="source-strip"><span className="source-pill">{sourceLabel.includes('Fiktiv') ? t('dash.pillDemo') : t('dash.pillData')}</span><strong>{sourceLabel}</strong><span>{t('dash.stripNote')}</span><button onClick={() => demo(900)}>{t('dash.demoShort')}</button><button disabled={busy} onClick={() => demo(30_000)}>{t('dash.stressTest')}</button><label className="import-button">{t('dash.importJson')}<input type="file" accept="application/json,.json" onChange={e => { const file = e.target.files?.[0]; if (file) importFile(file); e.target.value = ''; }} /></label><label className="import-button">{t('dash.diffLoad')}<input type="file" accept="application/json,.json" aria-label={t('dash.diffLoadAria')} onChange={e => { const file = e.target.files?.[0]; if (file) loadBaseline(file); e.target.value = ''; }} /></label></div>
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
        <div className="sources-filter"><h3>{t('dash.sourcesTitle')}</h3>
          <select id="sources-select" disabled={busy} aria-label={t('dash.sourcesTitle')} value={filters.sources ?? 'any'} onChange={e => { const value = e.target.value; setFilters(f => ({ ...f, sources: value === 'any' ? undefined : value as 'corroborated' | 'thin' })); }}>
            <option value="any">{t('dash.sourcesAny')}</option>
            <option value="corroborated">{t('dash.sourcesCorroborated')}</option>
            <option value="thin">{t('dash.sourcesThin')}</option>
          </select>
          {filters.sources && view && <p className="muted small-text">{view.edges.length ? t('dash.sourcesActive', { edges: number(view.edges.length), total: number(view.stats.filteredEdges) }) : t('dash.sourcesEmpty')}</p>}
          <p>{t('dash.sourcesText')}</p></div>
        <div className="lod-setting"><label><input type="checkbox" disabled={busy} checked={filters.lod} onChange={e => { if (!e.target.checked && (view?.stats.filteredNodes ?? 0) > 20_000 && !window.confirm(tRef.current('dash.lodConfirm'))) return; setFilters(f => ({ ...f, lod: e.target.checked })); }} /> {t('dash.lodLabel')}</label><p>{t('dash.lodNote')}</p></div>
        <div className="lod-setting"><label><input type="checkbox" disabled={busy} checked={!!filters.bundle} onChange={e => setFilters(f => ({ ...f, bundle: e.target.checked }))} /> {t('dash.bundleLabel')}</label><p>{t('dash.bundleNote')}</p>{filters.bundle && view && view.edges.length !== view.stats.filteredEdges && <p className="muted">{t('dash.bundleStats', { shown: number(view.edges.length), total: number(view.stats.filteredEdges) })}</p>}</div>
        {view?.analytics && <div className="analytics-panel"><div className="section-heading"><h3>{t('dash.analyticsTitle')}</h3></div>
          <dl className="analytics-facts"><div><dt>Communities</dt><dd>{number(view.analytics.communities)}</dd></div><div><dt>{t('dash.density')}</dt><dd>{(view.analytics.density * 100).toFixed(2)} %</dd></div>{view.analytics.temporalRange && <div><dt>{t('dash.rangeLabel')}</dt><dd>{view.analytics.temporalRange.first} → {view.analytics.temporalRange.last}</dd></div>}</dl>
          {view.timeline && view.timeline.length > 0 && <>
            <h4 className="hub-heading">{t('dash.timelineTitle')}</h4>
            <ol className="timeline-bars">{view.timeline.map(bucket => {
              const window = monthWindow(bucket.month);
              return <li key={bucket.month}>
                <button type="button" className="timeline-month" disabled={busy || !window} aria-label={t('dash.timelineSelect', { month: bucket.month })} onClick={() => { if (!window) return; setFilters(f => ({ ...f, from: window.from, to: window.to })); setAnalysisNotice(t('dash.annTimeline', { month: bucket.month })); }}>{bucket.month}</button>
                <span className="timeline-bar" aria-hidden="true" style={{ width: `${timelineMax ? Math.max(2, Math.round((bucket.count / timelineMax) * 100)) : 0}%` }} />
                <strong>{number(bucket.count)}</strong>
              </li>;
            })}</ol>
            <div className="playback">
              <button type="button" className="outline-button playback-toggle" aria-pressed={playing} onClick={() => (playing ? setPlaying(false) : startPlayback())}>{playing ? `⏸ ${t('dash.playStop')}` : `▶ ${t('dash.playStart')}`}</button>
              <button type="button" className="text-button" aria-label={t('dash.playPrev')} onClick={() => stepPlayback(-1)}>‹</button>
              <button type="button" className="text-button" aria-label={t('dash.playNext')} onClick={() => stepPlayback(1)}>›</button>
              <span className="playback-month">{filters.to ? filters.to.slice(0, 7) : '—'}</span>
            </div>
            <p className="muted">{t('dash.playNote')}</p>
            <p className="muted">{t('dash.timelineNote')}</p>
          </>}
          {view.duplicates && view.duplicates.length > 0 && <>
            <h4 className="hub-heading">{t('dash.dupeTitle')}</h4>
            <ul className="dupe-list">{view.duplicates.map(group => (
              <li key={group.ids.join('|')}>
                <span className="dupe-label">{group.label}</span>
                <span className="dupe-count">{number(group.ids.length)}{group.sameType ? '' : ` · ${t('dash.dupeMixed')}`}</span>
                <button type="button" className="text-button dupe-compare" aria-label={t('dash.dupeCompareAria', { label: group.label })} onClick={() => { if (mode !== 'list') changeMode('list'); setSearch(group.label); setListPage(0); setAnalysisNotice(t('dash.annDupe', { count: number(group.ids.length), label: group.label })); }}>{t('dash.dupeCompare')}</button>
                <button type="button" className="text-button dupe-diff" aria-label={t('dash.dupeCompareActionAria', { label: group.label })} onClick={() => { setDupeCompareIds(group.ids.slice(0, 4)); setAnalysisNotice(t('dash.annDupeCompare', { count: number(Math.min(4, group.ids.length)) })); }}>{t('dash.dupeCompareAction')}</button>
              </li>
            ))}</ul>
            <p className="muted">{t('dash.dupeText')}</p>
            {dupeCompareIds.length >= 2 && (view.stats.grouped
              ? <p className="muted">{t('dash.dupeGrouped')}</p>
              : dupeComparison && <div className="dupe-diff-panel">
                <div className="section-heading"><h4 className="hub-heading">{t('dash.dupeCompareTitle')}</h4><button type="button" className="text-button" onClick={() => setDupeCompareIds([])}>{t('dash.dupeCompareClose')}</button></div>
                <table className="dupe-table">
                  <caption className="muted">{t('dash.dupeCompareCaption')}</caption>
                  <thead><tr><th scope="col">{t('dash.dupeField')}</th>{dupeComparison.profiles.map(profile => <th key={profile.id} scope="col"><button type="button" className="text-button" onClick={() => { if (mode !== 'network') changeMode('network'); select(profile.id); requestAnimationFrame(() => network.current?.focus(profile.id)); }}>{profile.label}</button><code className="dupe-id">{profile.id}</code></th>)}</tr></thead>
                  <tbody>
                    <tr><th scope="row">{t('inspector.entityType')}</th>{dupeComparison.profiles.map(profile => <td key={profile.id} className={dupeComparison.conflicts.includes('type') ? 'conflict' : ''}>{t(`type.${profile.type}`)}</td>)}</tr>
                    <tr><th scope="row">{t('inspector.degree')}</th>{dupeComparison.profiles.map(profile => <td key={profile.id}>{number(profile.degree)}</td>)}</tr>
                    <tr><th scope="row">{t('inspector.community')}</th>{dupeComparison.profiles.map(profile => <td key={profile.id} className={dupeComparison.conflicts.includes('community') ? 'conflict' : ''}>{profile.community ?? '—'}</td>)}</tr>
                    <tr><th scope="row">{t('inspector.firstSeen')}</th>{dupeComparison.profiles.map(profile => <td key={profile.id} className={dupeComparison.conflicts.includes('firstSeen') ? 'conflict' : ''}>{profile.firstSeen ?? '—'}</td>)}</tr>
                    <tr><th scope="row">{t('inspector.lastSeen')}</th>{dupeComparison.profiles.map(profile => <td key={profile.id} className={dupeComparison.conflicts.includes('lastSeen') ? 'conflict' : ''}>{profile.lastSeen ?? '—'}</td>)}</tr>
                    <tr><th scope="row">{t('dash.dupeEvidence')}</th>{dupeComparison.profiles.map(profile => <td key={profile.id}>{number(profile.evidence)}</td>)}</tr>
                    <tr><th scope="row">{t('dash.dupeShared')}</th><td colSpan={dupeComparison.profiles.length}>{dupeComparison.shared.length ? dupeComparison.shared.map(peer => peer.label).join(', ') : '—'}</td></tr>
                    <tr><th scope="row">{t('dash.dupeUnique')}</th>{dupeComparison.profiles.map(profile => <td key={profile.id}>{dupeComparison.unique[profile.id].join(', ') || '—'}</td>)}</tr>
                  </tbody>
                </table>
                <p className="muted">{t('dash.dupeCompareNote')}</p>
              </div>)}
          </>}
          {view.analytics.hubs.length > 0 && <><h4 className="hub-heading">{t('dash.topHubs')}</h4><ol className="hub-list">{view.analytics.hubs.map(hub => <li key={hub.id}><button type="button" className="text-button" onClick={() => { if (mode !== 'network') changeMode('network'); select(hub.id); requestAnimationFrame(() => network.current?.focus(hub.id)); }}>{hub.name}</button><span className="hub-rank">{(hub.rank * 100).toFixed(1)} %</span></li>)}</ol></>}
          {view.vulnerability && <div className="vuln-panel">
            <h4 className="hub-heading">{t('dash.vulnTitle')}</h4>
            <dl className="analytics-facts"><div><dt>{t('dash.vulnCutEdges')}</dt><dd>{number(view.vulnerability.cutEdges)}</dd></div><div><dt>k-Core</dt><dd>{t('dash.vulnCore', { core: view.vulnerability.maxCore, size: number(view.vulnerability.maxCoreSize) })}</dd></div></dl>
            {view.vulnerability.cutVertices.length > 0
              ? <><p className="muted small-text">{t('dash.impactSelect')}</p><ol className="hub-list">{view.vulnerability.cutVertices.map(node => <li key={node.id}><input type="checkbox" className="impact-check" checked={impactIds.includes(node.id)} disabled={!impactIds.includes(node.id) && impactIds.length >= 3} aria-label={t('dash.impactAria', { label: node.label })} onChange={() => toggleImpact(node.id)} /><button type="button" className="text-button" aria-label={t('dash.vulnAria', { label: node.label })} onClick={() => { if (mode !== 'network') changeMode('network'); select(node.id); requestAnimationFrame(() => network.current?.focus(node.id)); }}>{node.label}</button><span className="hub-rank">{number(node.degree)}</span><span className="vuln-impact">{t('dash.vulnImpact', { parts: node.impact.components, largest: number(node.impact.largest) })}</span></li>)}</ol>
                {impactResult && <p className="muted" role="status">{t('dash.impactResult', { removed: number(impactResult.removed), parts: impactResult.components, largest: number(impactResult.largest), isolated: number(impactResult.isolated) })} <button type="button" className="text-button" onClick={() => { setImpactIds([]); setImpactResult(null); }}>{t('dash.impactClear')}</button></p>}</>
              : <p className="muted">{t('dash.vulnNone')}</p>}
            <p className="muted">{t('dash.vulnText')}</p>
          </div>}
          {gaps && <div className="gaps-panel">
            <h4 className="hub-heading">{t('dash.gapsTitle')}</h4>
            <p className="muted small-text">{t('dash.gapsCoverage', { backed: number(gaps.nodesBacked), total: number(gaps.nodes), edgesBacked: number(gaps.edgesBacked), edges: number(gaps.edges) })}</p>
            {gaps.singleSourceCount === 0 && gaps.unbackedNodeCount === 0
              ? <p className="muted" role="status">{t('dash.gapsNone')}</p>
              : <>
                {gaps.singleSource.length > 0 && <><h5 className="gaps-heading">{t('dash.gapsSingle')} ({number(gaps.singleSourceCount)})</h5>
                <ul className="hub-list">{gaps.singleSource.map(edge => <li key={edge.id}><button type="button" className="text-button" aria-label={t('dash.gapsEdgeAria', { type: edge.type, from: edge.source, to: edge.target })} onClick={() => { if (mode !== 'network') changeMode('network'); requestAnimationFrame(() => network.current?.focus(edge.source)); }}>{edge.type}</button><span className="hub-rank">{(edge.confidence * 100).toFixed(0)} %</span></li>)}</ul></>}
                {gaps.unbacked.length > 0 && <><h5 className="gaps-heading">{t('dash.gapsUnbacked')} ({number(gaps.unbackedNodeCount)})</h5>
                <ul className="hub-list">{gaps.unbacked.map(node => <li key={node.id}><button type="button" className="text-button" onClick={() => { if (mode !== 'network') changeMode('network'); select(node.id); requestAnimationFrame(() => network.current?.focus(node.id)); }}>{node.label}</button><span className="hub-rank">{node.type}</span></li>)}</ul></>}
              </>}
            <p className="muted">{t('dash.gapsText')}</p>
          </div>}
          {view.sharedSources && view.sharedSources.length > 0 && <div className="shared-panel">
            <h4 className="hub-heading">{t('dash.sharedTitle')}</h4>
            <ul className="shared-list">{view.sharedSources.map(group => <li key={group.document}>
              <span className="shared-doc">{group.title ?? group.document}</span>
              <span className="shared-meta">{t('dash.sharedDoc', { count: number(group.entities.length), id: group.document })}</span>
              <span className="shared-entities">{group.entities.map(entity => <button key={entity.id} type="button" className="text-button" aria-label={t('dash.sharedAria', { label: entity.label })} onClick={() => { if (mode !== 'network') changeMode('network'); select(entity.id); requestAnimationFrame(() => network.current?.focus(entity.id)); }}>{entity.label}</button>)}</span>
            </li>)}</ul>
            <p className="muted">{t('dash.sharedText')}</p>
          </div>}
          {diff && <div className="diff-panel">
            <h4 className="hub-heading">{t('dash.diffTitle')}</h4>
            <p className="muted small-text">{baselineName} → {sourceLabel}</p>
            <p className="muted small-text">{t('dash.diffCounts', { entitiesAdded: number(diff.entities.added), entitiesRemoved: number(diff.entities.removed), relationsAdded: number(diff.relations.added), relationsRemoved: number(diff.relations.removed), documentsAdded: number(diff.documents.added) })}</p>
            {diff.entities.added === 0 && diff.entities.removed === 0 && diff.relations.added === 0 && diff.relations.removed === 0 && diff.documents.added === 0
              ? <p className="muted" role="status">{t('dash.diffNone')}</p>
              : <>
                {diff.newEntities.length > 0 && <><h5 className="gaps-heading">{t('dash.diffNewEntities')} ({number(diff.entities.added)})</h5>
                  <ul className="hub-list">{diff.newEntities.map(entity => <li key={entity.id}><button type="button" className="text-button" aria-label={t('dash.diffAria', { label: entity.label })} onClick={() => { if (mode !== 'network') changeMode('network'); select(entity.id); requestAnimationFrame(() => network.current?.focus(entity.id)); }}>{entity.label}</button><span className="hub-rank">{entity.type}</span></li>)}</ul></>}
                {diff.goneEntities.length > 0 && <><h5 className="gaps-heading">{t('dash.diffGoneEntities')} ({number(diff.entities.removed)})</h5>
                  <ul className="hub-list">{diff.goneEntities.map(entity => <li key={entity.id}><span>{entity.label}</span><span className="hub-rank">{entity.type}</span></li>)}</ul></>}
                {diff.corroborated.length > 0 && <><h5 className="gaps-heading">{t('dash.diffCorroborated')} ({number(diff.corroborated.length)})</h5>
                  <ul className="hub-list">{diff.corroborated.map(entity => <li key={entity.id}><button type="button" className="text-button" onClick={() => { if (mode !== 'network') changeMode('network'); select(entity.id); requestAnimationFrame(() => network.current?.focus(entity.id)); }}>{entity.label}</button><span className="hub-rank">{number(entity.sources)}</span></li>)}</ul></>}
                {diff.newConflicts.length > 0 && <><h5 className="gaps-heading">{t('dash.diffNewConflicts')} ({number(diff.newConflicts.length)})</h5>
                  <ul className="conflict-claims">{diff.newConflicts.map(conflict => <li key={`${conflict.pair[0]}|${conflict.pair[1]}`}>{conflict.labels[0]} ↔ {conflict.labels[1]}</li>)}</ul></>}
                {diff.resolvedConflicts > 0 && <p className="muted small-text" role="status">{t('dash.diffResolved', { count: number(diff.resolvedConflicts) })}</p>}
                <button type="button" className="text-button" onClick={() => downloadText('puppetnet-aenderungen.csv', diffToCSV(diff), 'text/csv;charset=utf-8')}>{t('dash.diffCsv')}</button>
              </>}
            <p><button type="button" className="text-button" onClick={() => { setDiff(null); setBaselineName(''); }}>{t('dash.diffClear')}</button></p>
            <p className="muted">{t('dash.diffText')}</p>
          </div>}
          {view.conflicts && <div className="conflict-panel">
            <h4 className="hub-heading">{t('dash.conflictTitle')}</h4>
            {view.conflicts.length === 0
              ? <p className="muted" role="status">{t('dash.conflictNoneInScope')}</p>
              : <p className="muted small-text">{t('dash.conflictCount', { count: number(view.conflicts.length) })}</p>}
            {view.conflicts.length > 0 && (filters.dispute
              ? <p className="muted small-text" role="status">{t('dash.conflictActive', { edges: number(view.edges.length), nodes: number(view.nodes.length) })} <button type="button" className="text-button" onClick={() => setFilters(f => ({ ...f, dispute: undefined }))}>{t('dash.conflictShowAll')}</button></p>
              : <button type="button" className="text-button" aria-label={t('dash.conflictOnlyAria')} onClick={() => setFilters(f => ({ ...f, dispute: true }))}>{t('dash.conflictOnly')}</button>)}
            {view.conflicts.length > 0 && <ul className="conflict-list">{view.conflicts.map(conflict => <li key={`${conflict.pair[0]}|${conflict.pair[1]}`} className={`conflict-${conflict.kind}`}>
              <span className="conflict-pair">{conflict.labels[0]} ↔ {conflict.labels[1]}</span>
              <span className="conflict-kind">{t(`dash.conflictKind${conflict.kind === 'conflict' ? 'Conflict' : conflict.kind === 'sequence' ? 'Sequence' : 'Undated'}`)}</span>
              <ul className="conflict-claims">{conflict.claims.map(claim => <li key={claim.type}>{t('dash.conflictClaim', { type: claim.type, documents: number(claim.documents), sources: number(claim.sources) })}{claim.first ? ` · ${claim.first}${claim.last && claim.last !== claim.first ? ` → ${claim.last}` : ''}` : ''}</li>)}</ul>
            </li>)}</ul>}
            <p className="muted">{t('dash.conflictText')}</p>
          </div>}
          {watched && watched.watched > 0 && <div className="watch-panel">
            <h4 className="hub-heading">{t('dash.watchTitle')}</h4>
            <p className="muted small-text">{t('dash.watchSummary', { watched: number(watched.watched), visible: number(watched.visible), settled: number(watched.settled) })}</p>
            {watched.gone.length > 0 && <p className="muted small-text" role="status">{t('dash.watchGone', { count: number(watched.gone.length) })}</p>}
            <ul className="hub-list watch-list">{watched.entries.map(entry => <li key={entry.id} className={`watch-${entry.state ?? 'plain'}${entry.conflicts.length ? ' watch-disputed' : ''}`}>
              {entry.visible
                ? <button type="button" className="text-button" aria-label={t('dash.watchAria', { label: entry.label })} onClick={() => { if (mode !== 'network') changeMode('network'); select(entry.id); requestAnimationFrame(() => network.current?.focus(entry.id)); }}>{entry.label}</button>
                : <span>{entry.label}</span>}
              {entry.state && <span className="watch-state">{t(`dash.risk${entry.state[0].toUpperCase()}${entry.state.slice(1)}`)}</span>}
              {entry.conflicts.length > 0 && <span className="watch-state watch-dispute">{t('dash.watchDisputed', { types: entry.conflicts.join(' / ') })}</span>}
              {entry.last && <span className="hub-rank">{entry.last}</span>}
              <button type="button" className="text-button" aria-label={t('dash.watchRemoveAria', { label: entry.label })} onClick={() => setWatch(current => toggleWatch(current, entry.id))}>×</button>
            </li>)}</ul>
            <button type="button" className="text-button" onClick={() => downloadText('puppetnet-beobachtung.csv', watchToCSV(watched), 'text/csv;charset=utf-8')}>{t('dash.watchCsv')}</button>
            <button type="button" className="text-button" aria-label={t('dash.watchBriefAria')} onClick={exportWatchBrief}>{t('dash.watchBrief')}</button>
            {diff && <div className="watch-changes">
              <h5 className="gaps-heading">{t('dash.watchChangesTitle')}</h5>
              {watchDigest.length === 0
                ? <p className="muted" role="status">{t('dash.watchChangesNone')}</p>
                : <><ul className="hub-list">{watchDigest.map(change => <li key={change.id} className="watch-change">
                  <button type="button" className="text-button" aria-label={t('dash.watchAria', { label: change.label })} onClick={() => { if (mode !== 'network') changeMode('network'); select(change.id); requestAnimationFrame(() => network.current?.focus(change.id)); }}>{change.label}</button>
                  <span className="watch-change-kinds">{change.kinds.map(kind => <span key={kind} className={`watch-kind watch-kind-${kind}`}>{t(`dash.watchKind${kind[0].toUpperCase()}${kind.slice(1)}`)}</span>)}</span>
                </li>)}</ul>
                  <button type="button" className="text-button" onClick={() => downloadText('puppetnet-beobachtung-aenderungen.csv', watchChangesToCSV(watchDigest), 'text/csv;charset=utf-8')}>{t('dash.watchChangesCsv')}</button></>}
              {diff.watch.capped && <p className="muted small-text">{t('dash.watchChangesCapped', { cap: number(diff.watch.cap) })}</p>}
            </div>}
            <p className="muted">{t('dash.watchText')}</p>
          </div>}
          <div className="case-search">
            <h4 className="hub-heading">{t('dash.caseSearchTitle')}</h4>
            <label className="case-search-field"><input type="search" value={caseQuery} placeholder={t('dash.caseSearchPlaceholder')} aria-label={t('dash.caseSearchAria')} onChange={e => setCaseQuery(e.target.value)} />{caseQuery && <button type="button" className="text-button" onClick={() => setCaseQuery('')}>{t('dash.caseSearchClear')}</button>}</label>
            {caseQuery.trim() && (caseHits.length === 0
              ? <p className="muted" role="status">{t('dash.caseSearchEmpty')}</p>
              : <><p className="muted small-text">{t('dash.caseSearchCount', { count: number(caseHits.length) })}</p>
                <ul className="hub-list case-hits">{caseHits.map(hit => <li key={`${hit.kind}:${hit.id}`} className={`case-hit case-hit-${hit.kind}`}>
                  {hit.kind === 'conflict'
                    ? <button type="button" className="text-button" onClick={() => setFilters(f => ({ ...f, dispute: true }))}>{hit.label}</button>
                    : hit.kind === 'outlet'
                      ? <button type="button" className="text-button" onClick={() => setFilters(f => ({ ...f, outlet: hit.id }))}>{hit.label}</button>
                      : <button type="button" className="text-button" onClick={() => { if (mode !== 'network') changeMode('network'); select(hit.id); requestAnimationFrame(() => network.current?.focus(hit.id)); }}>{hit.label}</button>}
                  <span className="case-kind">{t(`dash.caseKind${hit.kind[0].toUpperCase()}${hit.kind.slice(1)}`)}</span>
                  {hit.detail && <span className="hub-rank">{hit.detail}</span>}
                </li>)}</ul></>)}
            <p className="muted">{t('dash.caseSearchHint')}</p>
          </div>
          {view.risk && <div className="risk-panel">
            <h4 className="hub-heading">{t('dash.riskTitle')}</h4>
            <p className="muted small-text">{t('dash.riskSummary', { weak: number(view.risk.weakCount), entities: number(view.risk.entities), months: view.risk.months })}</p>
            {filters.weak
              ? <p className="muted small-text" role="status">{t('dash.riskActive', { nodes: number(view.nodes.length) })} <button type="button" className="text-button" onClick={() => setFilters(f => ({ ...f, weak: undefined }))}>{t('dash.riskShowAll')}</button></p>
              : <button type="button" className="text-button" aria-label={t('dash.riskOnlyWeakAria')} onClick={() => setFilters(f => ({ ...f, weak: true }))}>{t('dash.riskOnlyWeak')}</button>}
            {view.risk.weakCount === 0
              ? <p className="muted" role="status">{t('dash.riskNone')}</p>
              : <><h5 className="gaps-heading">{t('dash.riskListHeading', { count: number(view.risk.weakCount) })}</h5>
                <ul className="hub-list risk-list">{view.risk.entries.map(entry => <li key={entry.id} className={`risk-${entry.state}`}>
                  <button type="button" className="text-button" aria-label={t('dash.riskAria', { label: entry.label, state: t(`dash.risk${entry.state[0].toUpperCase()}${entry.state.slice(1)}`) })} onClick={() => { if (mode !== 'network') changeMode('network'); select(entry.id); requestAnimationFrame(() => network.current?.focus(entry.id)); }}>{entry.label}</button>
                  <span className="risk-state">{t(`dash.risk${entry.state[0].toUpperCase()}${entry.state.slice(1)}`)}</span>
                  <span className="hub-rank">{t('dash.riskDocs', { documents: number(entry.documents), sources: number(entry.sources), relations: number(entry.relations) })}</span>
                </li>)}</ul>
                <button type="button" className="text-button" onClick={() => downloadText(riskToCSV(view.risk!), 'puppetnet-belastbarkeit.csv', 'text/csv')}>{t('dash.riskCsv')}</button></>}
            <p className="muted">{t('dash.riskText')}</p>
          </div>}
          {view.freshness && <div className="fresh-panel">
            <h4 className="hub-heading">{t('dash.freshTitle')}</h4>
            <div className="fresh-window" role="group" aria-label={t('dash.freshWindow')}>{FRESH_WINDOWS.map(months => <button key={months} type="button" disabled={busy} className={`window-button${view.freshness?.months === months ? ' is-active' : ''}`} aria-pressed={view.freshness?.months === months} aria-label={t('dash.freshWindowAria', { months })} onClick={() => setFilters(f => ({ ...f, fresh: months === DEFAULT_FRESH_MONTHS ? undefined : months }))}>{t('dash.freshWindowLabel', { months })}</button>)}</div>
            <p className="muted small-text">{t('dash.freshSummary', { stale: number(view.freshness.staleCount), entities: number(view.freshness.entities), months: view.freshness.months, undated: number(view.freshness.undatedCount) })}</p>
            {view.freshness.staleCount === 0
              ? <p className="muted" role="status">{t('dash.freshNone')}</p>
              : <><h5 className="gaps-heading">{t('dash.freshStale')} ({number(view.freshness.staleCount)})</h5>
                <ul className="hub-list">{view.freshness.stale.map(entity => <li key={entity.id}><button type="button" className="text-button" aria-label={t('dash.freshAria', { label: entity.label })} onClick={() => { if (mode !== 'network') changeMode('network'); select(entity.id); requestAnimationFrame(() => network.current?.focus(entity.id)); }}>{entity.label}</button><span className="hub-rank">{entity.last}</span></li>)}</ul></>}
            <p className="muted">{t('dash.freshText', { months: view.freshness.months })}</p>
          </div>}
          {view.sourceRegistry && view.sourceRegistry.length > 0 && <div className="registry-panel">
            <h4 className="hub-heading">{t('dash.registryTitle')}</h4>
            {filters.outlet && <p className="muted" role="status">{t('dash.registryActive', { source: filters.outlet })} <button type="button" className="text-button" onClick={() => setFilters(f => ({ ...f, outlet: undefined }))}>{t('dash.registryClear')}</button></p>}
            <ul className="registry-list">{view.sourceRegistry.map(entry => <li key={entry.name || 'unnamed'} className={filters.outlet && filters.outlet.toLowerCase() === entry.name.toLowerCase() ? 'active' : undefined}>
              {entry.name
                ? <button type="button" className="text-button registry-name" aria-label={t('dash.registryApply', { source: entry.name })} onClick={() => { setFilters(f => ({ ...f, outlet: entry.name })); setAnnouncement(t('dash.annOutlet', { source: entry.name })); }}>{entry.name}</button>
                : <span className="registry-name muted">{t('dash.registryUnnamed')}</span>}
              <span className="registry-counts">{t('dash.registryDocs', { docs: number(entry.documents), entities: number(entry.entities), edges: number(entry.edges) })}</span>
              {entry.first && <span className="registry-window">{entry.first}{entry.last && entry.last !== entry.first ? ` → ${entry.last}` : ''}</span>}
            </li>)}</ul>
            <p className="muted">{t('dash.registryText')}</p>
          </div>}
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
            {pathResult.timeline && pathResult.timeline.hops.length > 0 && <div className="path-timeline">
              <h4 className="hub-heading">{t('dash.pathTimelineTitle')}</h4>
              <ol className="path-dates">{pathResult.timeline.hops.map((hop, index) => <li key={`${hop.from}:${hop.to}:${index}`}>
                <span className="path-date">{hop.first ?? '—'}{hop.last && hop.last !== hop.first ? ` → ${hop.last}` : ''}</span>
                <span className="path-hop-type">{hop.type}</span>
                {!hop.ordered && <strong className="inline-warning">{t('dash.pathAnachronism')}</strong>}
              </li>)}</ol>
              {pathResult.timeline.anachronisms > 0
                ? <p className="inline-warning" role="status">{t('dash.pathAnachronismNote', { count: pathResult.timeline.anachronisms })}</p>
                : <p className="muted" role="status">{t('dash.pathChronoOk')}</p>}
            </div>}
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
        <div className="views-block"><h3>{t('dash.viewsTitle')}</h3>
          <label htmlFor="view-name">{t('dash.viewsName')}<input id="view-name" type="text" maxLength={60} autoComplete="off" value={viewName} onChange={e => setViewName(e.target.value)} /></label>
          <button type="button" className="outline-button" onClick={saveCurrentView}>{t('dash.viewsSave')}</button>
          {savedViews.length === 0
            ? <p className="muted small-text">{t('dash.viewsEmpty')}</p>
            : <ul className="saved-views">{savedViews.map(entry => <li key={entry.name}>
              <button type="button" className="text-button" title={entry.name} aria-label={t('dash.viewsApply', { name: entry.name })} onClick={() => applySavedView(entry)}>{entry.name}</button>
              <span className="saved-date">{entry.saved}</span>
              <button type="button" className="text-button" aria-label={t('dash.viewsDelete', { name: entry.name })} onClick={() => { setSavedViews(list => removeSavedView(list, entry.name)); setViewsStatus(t('dash.viewsRemoved', { name: entry.name })); }}>×</button>
            </li>)}</ul>}
          <p className="muted small-text">{t('dash.viewsText')}</p>
          <p className="muted" role="status">{viewsStatus}</p>
        </div>
        <div className="notes-import">
          <label className="import-button outlined-import">{t('dash.notesImport')}
            <input ref={notesImport} type="file" accept=".md,text/markdown,text/plain" aria-label={t('dash.notesImportAria')} onChange={e => { void importCaseNotes(e.target.files?.[0]); }} />
          </label>
          {notesImportStatus && <p className="muted" role="status">{notesImportStatus}</p>}
          <label className="import-button outlined-import bundle-import">{t('dash.bundleImport')}
            <input ref={bundleInput} type="file" accept=".json,application/json" aria-label={t('dash.bundleImportAria')} onChange={e => { void importCaseBundle(e.target.files?.[0]); }} />
          </label>
          {bundleStatus && <p className="muted" role="status">{bundleStatus}</p>}
        </div>
        {Object.keys(notes).length > 0 && <div className="notes-index">
          <p className="muted" role="status">{t('dash.notesCount', { count: number(Object.keys(notes).length) })}</p>
          <label htmlFor="note-search">{t('dash.notesSearch')}<input id="note-search" type="search" autoComplete="off" value={noteQuery} placeholder={t('dash.notesSearchPlaceholder')} onChange={e => setNoteQuery(e.target.value)} /></label>
          {noteQuery.trim() && <p className="muted small-text">{noteHits.length ? t('dash.notesHits', { count: number(noteHits.length) }) : t('dash.notesNoHits')}</p>}
          {(noteQuery.trim() ? noteHits : noteHits.slice(0, 5)).length > 0 && <ul className="note-hits">{(noteQuery.trim() ? noteHits : noteHits.slice(0, 5)).map(hit => <li key={hit.id}>
            <button type="button" className="text-button note-hit" aria-label={t('dash.noteHitAria', { label: hit.label })} onClick={() => { if (mode !== 'network') changeMode('network'); select(hit.id); requestAnimationFrame(() => network.current?.focus(hit.id)); }}>{hit.label}</button>
            <span className="note-snippet">{hit.snippet}</span>
          </li>)}</ul>}
          <p className="muted small-text">{t('dash.caseBundleText')}</p>
        </div>}
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
        <div className="visualization-footer"><span><span className="status-dot" /> {t('dash.nodesLoaded', { count: number(view?.stats.totalNodes) })}</span><span className="view-export"><button type="button" onClick={() => downloadText('puppetnet-knoten.csv', nodesToCSV(view?.nodes ?? []), 'text/csv;charset=utf-8')}>{t('dash.nodesCsv')}</button><button type="button" onClick={() => downloadText('puppetnet-kanten.csv', edgesToCSV(view?.edges ?? []), 'text/csv;charset=utf-8')}>{t('dash.edgesCsv')}</button><button type="button" onClick={() => downloadText('puppetnet-ansicht.json', viewToJSON({ nodes: view?.nodes ?? [], edges: view?.edges ?? [], analytics: view?.analytics, insights: view?.insights }), 'application/json')}>JSON ↓</button><button type="button" aria-label={t('dash.evidenceCsvAria')} onClick={() => { if (!view) return; send({ kind: 'evidenceCsv', version: version.current }); }}>{t('dash.evidenceCsv')}</button><button type="button" aria-label={t('dash.caseBundleAria')} onClick={exportCaseBundle}>{t('dash.caseBundle')}</button><button type="button" aria-label={t('dash.archiveAria')} onClick={exportArchive}>{t('dash.archive')}</button><button type="button" aria-label={t('dash.reportAria')} onClick={() => { if (!view) return; downloadText('puppetnet-fallbericht.md', caseReport({ stats: view.stats, nodes: view.nodes, edges: view.edges, analytics: view.analytics, insights: view.insights, timeline: view.timeline, duplicates: view.duplicates, vulnerability: view.vulnerability, gaps: view.gaps, sharedSources: view.sharedSources, conflicts: view.conflicts, freshness: view.freshness, risk: view.risk, notes }, new Date().toISOString(), lang), 'text/markdown;charset=utf-8'); }}>{t('dash.reportButton')}</button></span><span>{view?.stats.focused ? <button onClick={() => requestView('reset')}>{t('dash.showAll')}</button> : t('dash.clickHintFoot')}</span></div>
      </section>
      <SidePanel node={selected} evidence={evidence} edge={selectedEdge} edgeEvidence={edgeEvidence} edgeLabels={{
        source: selectedEdge ? view?.nodes.find(n => n.id === selectedEdge.source)?.label ?? selectedEdge.source : '',
        target: selectedEdge ? view?.nodes.find(n => n.id === selectedEdge.target)?.label ?? selectedEdge.target : '',
      }} detailError={detailError} note={selected && !selected.group ? notes[selected.id] ?? '' : ''} onEgo={id => requestEgo(id)}
      watched={Boolean(selected && watch.includes(selected.id))} onWatch={() => { if (!selected || selected.group) return; setWatch(current => toggleWatch(current, selected.id)); }} onBrief={selected && !selected.group ? exportEntityBrief : undefined} timeline={selectedTimeline} onNote={text => { if (!selected || selected.group) return; const clean = normalizeNote(text); setNotes(current => { const next = { ...current }; if (clean) next[selected.id] = clean; else delete next[selected.id]; return next; }); }} onClose={() => { clearSelected(); setAnnouncement(t('dash.detailsClosed')); }} onDrill={group => { clearSelected(); setBusy(true); send({ kind: 'drill', group, version: ++version.current, filters: filtersRef.current }); }} />
    </div>}
    <footer className="dashboard-footer"><span>PUPPETNET / OPEN-SOURCE INTELLIGENCE</span><span>Sigma.js 4 · Graphology · Leaflet · OpenStreetMap</span><a className="footer-legal" href="/legal/">{t('dash.legalLink')}</a></footer>
  </main>;
}
