'use client';
import dynamic from 'next/dynamic';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import EntityTable, { type TableRow } from './EntityTable';
import { COLORS, TYPES, LIMITS, decodeViewState, edgesToCSV, encodeViewState, nodesToCSV, rankSearch, resolveNodeRef, viewToJSON, type Evidence, type Filters, type GraphNode, type View, type ViewUrlState } from './graph-data';
import type { NetworkHandle, PathHighlight } from './NetworkGraph';
import type { WorkerRequest, WorkerResponse } from './protocol';
import { loadStaticGraph, requestResearch, type Standby } from '../../src/lib/dataFetcher';
import StandbyPanel, { LoadingStage } from './BootState';
import SidePanel from './SidePanel';
import { downloadText } from './download';
const NetworkGraph = dynamic(() => import('./NetworkGraph'), { ssr: false, loading: () => <div className="stage-loading">WebGL-Modul laden …</div> });
const MapModule = dynamic(() => import('./MapModule'), { ssr: false, loading: () => <div className="stage-loading">Kartenmodul laden …</div> });
const initialFilters: Filters = { confidence: 0, weight: 0, types: [...TYPES], lod: true, ratio: 1 };
const number = (n = 0) => n.toLocaleString('de-DE');
interface PathResult { ids: string[]; labels: string[]; types: string[]; edgeIds: string[]; hops: number }
export default function Dashboard() {
  const worker = useRef<Worker | null>(null), network = useRef<NetworkHandle>(null), version = useRef(0), selectedId = useRef<string | null>(null);
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
  const [error, setError] = useState(''), [detailError, setDetailError] = useState(''), [layoutRunning, setLayoutRunning] = useState(false);
  const [sourceLabel, setSourceLabel] = useState('Bereitschaftsmodus'), [sourceForm, setSourceForm] = useState(false), [base, setBase] = useState(''), [token, setToken] = useState('');
  const [search, setSearch] = useState(initialUrl.current.q ?? ''), [listPage, setListPage] = useState(0);
  const [researchQuery, setResearchQuery] = useState(''), [researchBusy, setResearchBusy] = useState(false);
  const [pathSource, setPathSource] = useState(''), [pathTarget, setPathTarget] = useState('');
  const [pathResult, setPathResult] = useState<PathResult | null>(null), [pathError, setPathError] = useState('');
  const [standby, setStandby] = useState<Standby | null>(null);
  const loadingApi = useRef<AbortController | null>(null), loadingDetail = useRef<AbortController | null>(null);
  const staticRequest = useRef<AbortController | null>(null), reloadStatic = useRef<(() => void) | null>(null);
  const pendingSource = useRef('Bereitschaftsmodus'), committedSource = useRef('Bereitschaftsmodus');
  const [notice, setNotice] = useState('');
  const initialFocus = useRef(initialUrl.current.focus ?? null);
  const changeMode = useCallback((next: 'network' | 'map' | 'list') => {
    setMode(next);
    if (next !== 'network') worker.current?.postMessage({ kind: 'layout', running: false });
    setAnnouncement(next === 'list'
      ? 'Tabellenansicht geöffnet. Jeder Knoten ist als Zeile mit Aktionen verfügbar.'
      : next === 'map' ? 'Kartenansicht geöffnet.' : 'Netzwerkansicht geöffnet. Pfeiltasten bewegen den Fokus.');
  }, []);
  const clearSelected = useCallback(() => { selectedId.current = null; setSelected(null); setEvidence([]); loadingDetail.current?.abort(); setDetailError(''); }, []);
  const send = useCallback((request: WorkerRequest) => worker.current?.postMessage(request), []);
  const requestView = useCallback((kind: 'filter' | 'reset' = 'filter') => { setBusy(true); setError(''); clearSelected(); setPathResult(null); setPathError(''); send({ kind, version: ++version.current, filters: filtersRef.current }); }, [send, clearSelected]);
  useEffect(() => {
    const instance = new Worker(new URL('./graph.worker.ts', import.meta.url)); worker.current = instance;
    instance.onmessage = ({ data }: MessageEvent<WorkerResponse>) => {
      if (data.kind === 'view' && data.view.version === version.current) { setView(data.view); setBusy(false); setSourceLabel(pendingSource.current); committedSource.current = pendingSource.current; setListPage(0); }
      if (data.kind === 'positions') network.current?.positions(data.version, data.positions);
      if (data.kind === 'layout') setLayoutRunning(data.running);
      if (data.kind === 'detail' && data.node?.id === selectedId.current) { setSelected(data.node); setEvidence(old => [...new Map([...old, ...data.evidence].map(d => [d.id, d])).values()]); }
      if (data.kind === 'path') {
        if (!data.nodes || !data.edges) {
          setPathResult(null);
          setPathError('Kein Pfad zwischen den gewählten Entitäten im aktuellen Ausschnitt (maximal 6 Hops). Das ist eine ehrliche Auskunft über den veröffentlichten Ausschnitt, kein Beweis, dass keine Verbindung existiert.');
          setAnnouncement('Kein Pfad im aktuellen Ausschnitt gefunden.');
          return;
        }
        const labelOf = (id: string) => viewLatest.current?.nodes.find(n => n.id === id)?.label ?? id;
        setPathResult({ ids: data.nodes, labels: data.nodes.map(labelOf), types: data.edges.map(e => e.type), edgeIds: data.edges.map(e => e.id), hops: data.hops });
        setPathError('');
        setAnnouncement(`Pfad gefunden: ${data.hops} Verbindungen von ${labelOf(data.nodes[0])} nach ${labelOf(data.nodes[data.nodes.length - 1])}.`);
        return;
      }
      if (data.kind === 'error' && data.version === version.current) { setError(data.message); setBusy(false); pendingSource.current = committedSource.current; }
    };
    instance.onerror = event => { setError(`Daten-Worker: ${event.message || 'konnte nicht gestartet werden'}`); setBusy(false); };
    const loadPublished = () => {
      staticRequest.current?.abort();
      const controller = new AbortController(); staticRequest.current = controller;
      const bootVersion = ++version.current;
      setBusy(true); setError(''); clearSelected(); setStandby(null); setPathResult(null); setPathError('');
      loadStaticGraph({ signal: controller.signal }).then(data => {
        if (controller.signal.aborted || version.current !== bootVersion || worker.current !== instance) return;
        if (data.source === 'static') {
          pendingSource.current = data.message; setNotice('Hochleistungs-CDN-Modus: veröffentlichter Datensatz, keine Live-Datenbankverbindung.');
          instance.postMessage({ kind: 'import', text: JSON.stringify(data.graph), version: bootVersion, filters: filtersRef.current } satisfies WorkerRequest);
        } else {
          // No usable publication (missing/offline/invalid or an empty dataset
          // after a full withdrawal): stay in standby instead of silently
          // swapping in fictional demo data. The user decides explicitly.
          setStandby({ reason: data.reason, message: data.message });
          setBusy(false); setNotice('');
          setSourceLabel('Bereitschaftsmodus'); pendingSource.current = committedSource.current = 'Bereitschaftsmodus';
          setAnnouncement('Bereitschaftsmodus: kein auswertbarer Datensatz geladen. Demonstrationsdaten können explizit geladen werden.');
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
    const node = view?.nodes.find(n => n.id === id);
    setSelected(node ?? null);
    setAnnouncement(node ? `Details geöffnet: ${node.label}.` : 'Details geöffnet.');
    if (group) return;
    send({ kind: 'detail', id });

  }, [view, send]);
  function demo(count: number) {
    staticRequest.current?.abort(); pendingSource.current = count > 900 ? `Fiktiver Lasttest · ${number(count * 4)} Elemente` : 'Fiktive Demo';
    clearSelected(); setBusy(true); setError(''); setNotice(''); setStandby(null); setPathResult(null); setPathError(''); send({ kind: 'demo', count, version: ++version.current, filters: filtersRef.current });
  }
  function importFile(file: File) {
    if (file.size > LIMITS.bytes) { setError('Importlimit: 64 MiB JSON.'); return; }
    staticRequest.current?.abort(); pendingSource.current = file.name; setBusy(true); setError(''); clearSelected(); setPathResult(null); setPathError('');
    send({ kind: 'import', file, version: ++version.current, filters: filtersRef.current });
  }
  async function startResearch() {
    if (researchBusy) return;
    setError(''); loadingApi.current?.abort(); const controller = new AbortController(); loadingApi.current = controller;
    setResearchBusy(true);
    try {
      await requestResearch({ workerUrl: base, token, query: researchQuery, signal: controller.signal });
      setNotice('Recherche angenommen. GitHub prüft das Tageskontingent. Neue Daten erscheinen erst nach erfolgreicher Verarbeitung, Freigabe und Veröffentlichung. Du kannst die CDN-Daten später neu laden.');
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Recherche nicht angenommen.');
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
    if (viewLatest.current?.stats.grouped) { setPathError('Pfadfinder benötigt die Einzelknoten-Ansicht: LoD deaktivieren oder näher zoomen.'); return; }
    const source = resolveNodeRef(nodes, pathSource), target = resolveNodeRef(nodes, pathTarget);
    if (!source || !target) { setPathError('Start oder Ziel im aktuellen Ausschnitt nicht gefunden.'); return; }
    if (source.id === target.id) { setPathError('Start und Ziel sind dieselbe Entität.'); return; }
    send({ kind: 'path', source: source.id, target: target.id });
  }, [pathSource, pathTarget, send]);
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
  useEffect(() => {
    const wanted = initialFocus.current;
    if (!wanted || !view) return;
    initialFocus.current = null;
    if (view.nodes.some(n => n.id === wanted)) {
      select(wanted);
      requestAnimationFrame(() => network.current?.focus(wanted));
    }
  }, [view, select]);
  return <main id="main-content" tabIndex={-1} className="dashboard-shell" aria-busy={busy}>
    <p className="visually-hidden" role="status" aria-live="polite" aria-atomic="true">{announcement}</p>
    <header className="dashboard-header"><a className="brand" href="/"><img className="brand-art" src="/media/logo.webp" width="52" height="52" alt="" /><div>PUPPET<span>NET</span><small>INTELLIGENCE WORKSPACE</small></div></a><div className="header-path">Workspace <span>/</span> <strong>Netzwerkanalyse</strong></div><div className="header-status"><span className="status-dot" /> LOKALE VISUALISIERUNG <span className="version-tag">2.0</span></div></header>
    <section className="workspace-intro"><div><span className="eyebrow">VERBINDUNGEN SICHTBAR MACHEN</span><h1>Hinter den Verbindungen.</h1><p>Entitäten erkunden. Muster hinterfragen. Quellen überprüfen.</p></div><button onClick={() => setSourceForm(s => !s)} className="outline-button">On-Demand-Recherche ↗</button></section>
    <div className="source-strip"><span className="source-pill">{sourceLabel.includes('Fiktiv') ? 'DEMO' : 'DATEN'}</span><strong>{sourceLabel}</strong><span>Keine erfundenen Daten als Live-Ergebnisse.</span><button onClick={() => demo(900)}>Demo</button><button disabled={busy} onClick={() => demo(30_000)}>120.000 Elemente testen</button><label className="import-button">JSON importieren<input type="file" accept="application/json,.json" onChange={e => { const file = e.target.files?.[0]; if (file) importFile(file); e.target.value = ''; }} /></label></div>
    <p className="cdn-mode" role="note">Hochleistungs-CDN-Modus · Dashboard, Profile und Analystenkonsole verwenden den veröffentlichten statischen Datensatz. Zum Lesen ist kein Cloudflare-Worker nötig. Standardmäßig wird die sparsame Tabelle geöffnet; GPU-Netzwerk und Layout sind optional. <button type="button" disabled={busy} onClick={() => reloadStatic.current?.()}>CDN-Daten neu laden</button></p>
    {sourceForm && <form className="connection-form" onSubmit={e => { e.preventDefault(); startResearch(); }}>
      <p>Optional: Eine On-Demand-Recherche durchsucht die konfigurierten Quellen im Hintergrund. Das ist keine sofortige Live-Suche und keine Graph-API.</p>
      <label>Suchbegriff<input required maxLength={120} value={researchQuery} onChange={e => setResearchQuery(e.target.value)} /></label>
      <label>Worker-URL für On-Demand-Recherche<input required type="url" value={base} onChange={e => setBase(e.target.value)} placeholder="https://dein-suchrelay.workers.dev" /></label>
      <label>Suchzugangstoken<input required type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} /></label>
      <button className="primary-button" disabled={researchBusy} type="submit">{researchBusy ? 'Wird angefragt …' : 'Recherche anfordern'}</button>
      <p>Eine Worker-URL ist ausschließlich für Rechercheaufträge erforderlich. Token bleiben im Arbeitsspeicher; niemals einen GitHub-Token hier eingeben. Der Worker muss diesen Website-Ursprung per CORS erlauben.</p>
    </form>}

    {(error || notice || view?.stats.truncated) && <div className={`dashboard-alert ${error ? 'error' : ''}`} role={error ? 'alert' : 'status'}>{error || notice || 'API-Antwort ist begrenzt; dargestellt wird nur der geladene Ausschnitt.'}</div>}
    {standby
      ? <StandbyPanel standby={standby} busy={busy} onDemo={() => demo(900)} onResearch={() => setSourceForm(true)} onReload={() => reloadStatic.current?.()} />
      : !view ? <LoadingStage />
      : <div className="workspace-grid">
      <aside className="filter-panel"><span className="eyebrow">ANSICHT EINGRENZEN / 01</span><div className="section-heading"><h2>Filter</h2><button className="text-button" onClick={() => { filtersRef.current = initialFilters; setFilters(initialFilters); requestView('reset'); }}>Zurücksetzen</button></div>
        <label className="slider-label" htmlFor="confidence"><span>Confidence <strong>{filters.confidence.toFixed(2)}</strong></span><input id="confidence" disabled={busy} type="range" min="0" max="1" step="0.01" value={filters.confidence} onChange={e => setFilters(f => ({ ...f, confidence: Number(e.target.value) }))} /><small>Mindestens erforderliches Vertrauen</small></label>
        <label className="slider-label" htmlFor="weight"><span>Beziehungsgewicht <strong>{filters.weight.toFixed(2)}</strong></span><input id="weight" disabled={busy} type="range" min="0" max="1" step="0.01" value={filters.weight} onChange={e => setFilters(f => ({ ...f, weight: Number(e.target.value) }))} /><small>Fachliche Stärke einer Verbindung</small></label>
        <h3>Entitätstypen</h3><div className="type-filters">{TYPES.map(type => <label key={type}><input type="checkbox" disabled={busy} checked={filters.types.includes(type)} onChange={e => setFilters(f => ({ ...f, types: e.target.checked ? [...f.types, type] : f.types.filter(t => t !== type) }))} /><span className="type-dot" style={{ background: COLORS[type] }} />{type}</label>)}</div>
        {communities.length > 0 && <div className="community-filter"><h3>Community</h3>
          <label className="visually-hidden" htmlFor="community-select">Nach Community filtern</label>
          <select id="community-select" disabled={busy} value={filters.community ?? ''} onChange={e => setFilters(f => ({ ...f, community: e.target.value || undefined }))}>
            <option value="">Alle Communities</option>
            {communities.map(name => <option key={name} value={name}>{name}</option>)}
          </select>
          <p>Analysierte Communities aus dem Export. Farbmodus „Community" in der Netzwerkansicht.</p></div>}
        <div className="timeline-filter"><h3>Zeitraum (Erwähnungen)</h3>
          <div className="timeline-row">
            <label htmlFor="time-from">Von<input id="time-from" type="date" disabled={busy} value={filters.from ?? ''} max={filters.to} onChange={e => setFilters(f => ({ ...f, from: e.target.value || undefined }))} /></label>
            <label htmlFor="time-to">Bis<input id="time-to" type="date" disabled={busy} value={filters.to ?? ''} min={filters.from} onChange={e => setFilters(f => ({ ...f, to: e.target.value || undefined }))} /></label>
          </div>
          {(filters.from || filters.to) && <button type="button" className="text-button" onClick={() => setFilters(f => ({ ...f, from: undefined, to: undefined }))}>Zeitfilter löschen</button>}
          <p>Nur Entitäten mit datierten Quellnachweisen (erste/letzte Erwähnung aus dem Export). Undatierte fallen bei aktivem Filter heraus.</p></div>
        <div className="lod-setting"><label><input type="checkbox" disabled={busy} checked={filters.lod} onChange={e => { if (!e.target.checked && (view?.stats.filteredNodes ?? 0) > 20_000 && !window.confirm('Alle Knoten einzeln auf der GPU darstellen? Dies kann auf schwachen Geräten langsam sein.')) return; setFilters(f => ({ ...f, lod: e.target.checked })); }} /> Adaptives Detailniveau</label><p>Über 5.000 Knoten: zoomabhängige Darstellungsgruppen. Ausschalten zeigt einzelne Knoten.</p></div>
        {view?.analytics && <div className="analytics-panel"><div className="section-heading"><h3>Analytik (Export)</h3></div>
          <dl className="analytics-facts"><div><dt>Communities</dt><dd>{number(view.analytics.communities)}</dd></div><div><dt>Dichte</dt><dd>{(view.analytics.density * 100).toFixed(2)} %</dd></div>{view.analytics.temporalRange && <div><dt>Zeitraum</dt><dd>{view.analytics.temporalRange.first} → {view.analytics.temporalRange.last}</dd></div>}</dl>
          {view.analytics.hubs.length > 0 && <><h4 className="hub-heading">Top-Hubs (PageRank)</h4><ol className="hub-list">{view.analytics.hubs.map(hub => <li key={hub.id}><button type="button" className="text-button" onClick={() => { if (mode !== 'network') changeMode('network'); select(hub.id); requestAnimationFrame(() => network.current?.focus(hub.id)); }}>{hub.name}</button><span className="hub-rank">{(hub.rank * 100).toFixed(1)} %</span></li>)}</ol></>}
        </div>}
        <div className="pathfinder"><div className="section-heading"><h3>Pfadfinder</h3></div>
          <p className="muted">Kürzeste Verbindung zwischen zwei Entitäten im sichtbaren Ausschnitt (maximal 6 Hops, deterministische Breitensuche).</p>
          <label htmlFor="path-source">Von<input id="path-source" list="pathfinder-options" autoComplete="off" value={pathSource} onChange={e => setPathSource(e.target.value)} placeholder="Name oder ID …" /></label>
          <label htmlFor="path-target">Nach<input id="path-target" list="pathfinder-options" autoComplete="off" value={pathTarget} onChange={e => setPathTarget(e.target.value)} placeholder="Name oder ID …" /></label>
          <datalist id="pathfinder-options">{(view?.nodes ?? []).slice(0, 200).map(n => <option key={n.id} value={n.label}>{n.id}</option>)}</datalist>
          <button type="button" className="primary-button" disabled={busy} onClick={requestPath}>Pfad berechnen</button>
          {pathError && <p className="inline-warning" role="status">{pathError}</p>}
          {pathResult && <div className="path-result" role="status" aria-label="Gefundener Pfad">
            <strong>{pathResult.hops} Hop{pathResult.hops === 1 ? '' : 's'}</strong>
            <ol className="path-hops">{pathResult.labels.map((label, index) => <li key={`${pathResult.ids[index]}:${index}`}>
              <button type="button" className="text-button" onClick={() => { select(pathResult.ids[index]); requestAnimationFrame(() => network.current?.focus(pathResult.ids[index])); }}>{label}</button>
              {index < pathResult.types.length && <span className="hop-edge" aria-label={`über ${pathResult.types[index]}`}> —{pathResult.types[index]}→</span>}
            </li>)}</ol>
            {view?.stats.grouped && <p className="muted">Graph-Hervorhebung erst in der Einzelknoten-Ansicht (LoD-Gruppen aktiv).</p>}
            <button type="button" className="text-button" onClick={() => { setPathResult(null); setPathError(''); }}>Pfad löschen</button>
          </div>}
        </div>
        <div className="filter-foot"><span>○</span><p>Gemeinsame Erwähnung ist kein Beweis für Kontrolle oder Fehlverhalten.</p></div>
      </aside>
      <section className="visualization-panel" aria-label="Visualisierung"><div className="visualization-toolbar"><div className="view-tabs" role="group" aria-label="Darstellung"><button aria-pressed={mode === 'network'} aria-controls="visualization-stage" onClick={() => changeMode('network')}>◈ Netzwerk</button><button aria-pressed={mode === 'map'} aria-controls="visualization-stage" onClick={() => changeMode('map')}>◎ Karte</button><button aria-pressed={mode === 'list'} aria-controls="visualization-stage" onClick={() => changeMode('list')}>☷ Tabelle</button></div><button type="button" className="outline-button color-toggle" aria-pressed={colorMode === 'community'} disabled={mode !== 'network'} onClick={() => setColorMode(c => c === 'type' ? 'community' : 'type')}>Farbe: {colorMode === 'community' ? 'Community' : 'Typ'}</button><span className="live-indicator" role="status">{busy ? 'Verarbeitung …' : layoutRunning ? 'Layout aktiv' : 'Bereit'}</span></div>
        <div className="stats-bar"><div><strong>{number(view?.stats.filteredNodes)}</strong><span>Knoten im Filter</span></div><div><strong>{number(view?.stats.filteredEdges)}</strong><span>Beziehungen</span></div><div><strong>{number(view?.nodes.length)}</strong><span>GPU-Knoten / Gruppen</span></div><span className="lod-pill">{view?.stats.level ?? 'Laden'}</span></div>
        <details className="keyboard-help"><summary>Tastaturbedienung</summary><ul><li><kbd>Tab</kbd> springt in den Graphen, die Tabelle und den Inspector.</li><li><kbd>Pfeiltasten</kbd> bewegen den Fokus zwischen Knoten; <kbd>Pos1</kbd>/<kbd>Ende</kbd> springen an den Anfang oder das Ende.</li><li><kbd>Eingabe</kbd> oder <kbd>Leertaste</kbd> öffnet die Details, <kbd>Escape</kbd> schließt sie.</li><li><kbd>+</kbd>/<kbd>−</kbd> zoomen, <kbd>0</kbd> setzt die Ansicht zurück.</li><li>Die Registerkarte <strong>Tabelle</strong> ist die vollständige Textalternative zum WebGL-Canvas.</li></ul></details>
        <div id="visualization-stage" className={mode === 'network' ? 'graph-pane' : 'graph-pane hidden-pane'}>{mode === 'network' && <NetworkGraph ref={network} view={view} onSelect={select} onRatio={onRatio} onClear={clearSelected} colorMode={colorMode} path={pathHighlight} />}<div className="graph-tools"><button aria-label="Vergrößern" onClick={() => network.current?.zoom(.65)}>+</button><button aria-label="Verkleinern" onClick={() => network.current?.zoom(1.5)}>−</button><button aria-label="Ansicht einpassen" onClick={() => network.current?.reset()}>⌖</button><button onClick={() => send({ kind: 'layout', running: !layoutRunning })}>{layoutRunning ? 'Pause' : 'Layout'}</button></div></div>
        {mode === 'map' && <><MapModule points={view?.mapPoints ?? []} onSelect={id => select(id)} /><div className="data-footnote">{number(view?.stats.locatedNodes)} Knoten mit gültigen Koordinaten. Räumlich aggregiert, maximal 1.000 Marker.</div></>}
        {mode === 'list' && <EntityTable rows={tableRows} total={list.length} page={listPage} pageSize={pageSize} query={search} onQuery={value => { setSearch(value); setListPage(0); }} onPage={setListPage} onSelect={select} onFocusGraph={id => { changeMode('network'); select(id); requestAnimationFrame(() => network.current?.focus(id)); }} />}
        <div className="visualization-footer"><span><span className="status-dot" /> {number(view?.stats.totalNodes)} Knoten geladen</span><span className="view-export"><button type="button" onClick={() => downloadText('puppetnet-knoten.csv', nodesToCSV(view?.nodes ?? []), 'text/csv;charset=utf-8')}>Knoten CSV ↓</button><button type="button" onClick={() => downloadText('puppetnet-kanten.csv', edgesToCSV(view?.edges ?? []), 'text/csv;charset=utf-8')}>Kanten CSV ↓</button><button type="button" onClick={() => downloadText('puppetnet-ansicht.json', viewToJSON({ nodes: view?.nodes ?? [], edges: view?.edges ?? [], analytics: view?.analytics }), 'application/json')}>JSON ↓</button></span><span>{view?.stats.focused ? <button onClick={() => requestView('reset')}>← Gesamten Ausschnitt anzeigen</button> : 'Klick: untersuchen · Scroll: zoomen'}</span></div>
      </section>
      <SidePanel node={selected} evidence={evidence} detailError={detailError} onClose={() => { clearSelected(); setAnnouncement('Details geschlossen.'); }} onDrill={group => { clearSelected(); setBusy(true); send({ kind: 'drill', group, version: ++version.current, filters: filtersRef.current }); }} />
    </div>}
    <footer className="dashboard-footer"><span>PUPPETNET / OPEN-SOURCE INTELLIGENCE</span><span>Sigma.js 4 · Graphology · Leaflet · OpenStreetMap</span></footer>
  </main>;
}
