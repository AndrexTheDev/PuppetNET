'use client';
import { useState } from 'react';
import { COLORS, NOTE_LIMIT, communityColor, cypherExport, normalizeNote, safeUrl, type Evidence, type GraphEdge, type GraphNode } from './graph-data';
import { downloadText as download } from './download';
import { useLanguage, useT } from './i18n';
import { NUMBER_LOCALE } from '../../lib/i18n-strings';
export default function SidePanel({ node, evidence, edge, edgeEvidence, edgeLabels, onDrill, onEgo, onClose, detailError, note = '', onNote }: { node: GraphNode | null; evidence: Evidence[]; edge: GraphEdge | null; edgeEvidence: Evidence[]; edgeLabels: { source: string; target: string }; onDrill: (id: string) => void; onEgo?: (id: string) => void; onClose: () => void; detailError: string; note?: string; onNote?: (text: string) => void }) {
  const [copied, setCopied] = useState('');
  // Wave 11: the note draft follows the selected entity, never leaks between nodes.
  const [draft, setDraft] = useState('');
  const [draftNode, setDraftNode] = useState('');
  const [noteStatus, setNoteStatus] = useState('');
  if (node && draftNode !== node.id) { setDraftNode(node.id); setDraft(note); setNoteStatus(''); }
  const { lang } = useLanguage();
  const t = useT();
  if (!node && !edge) return <aside className="detail-panel" role="region" aria-labelledby="inspector-heading"><span className="eyebrow">{t('inspector.eyebrow')}</span><div className="empty-inspector"><div className="inspect-icon" aria-hidden="true">◎</div><h2 id="inspector-heading">{t('inspector.emptyTitleA')}<br />{t('inspector.emptyTitleB')}</h2><p>{t('inspector.emptyText')}</p></div><div className="inspector-foot">{t('inspector.foot')}</div></aside>;
  if (edge && !node) return <aside className="detail-panel" role="region" aria-labelledby="inspector-heading">
    <div className="panel-row"><span className="eyebrow">{t('inspector.eyebrow')}</span><button className="icon-button" onClick={onClose} aria-label={t('inspector.close')}>×</button></div>
    <div className="node-badge edge-badge">{t('inspector.edgeBadge', { type: edge.type })}</div>
    <h2 className="node-title" id="inspector-heading">{edgeLabels.source} → {edgeLabels.target}</h2>
    <code className="node-key">{edge.id}</code>
    <dl className="node-facts"><div><dt>{t('inspector.confidence')}</dt><dd>{(edge.confidence * 100).toFixed(1)} %</dd></div><div><dt>{t('inspector.weight')}</dt><dd>{(edge.weight * 100).toFixed(1)} %</dd></div>
      {edge.count !== undefined && <div><dt>{t('inspector.bundled')}</dt><dd>{edge.count.toLocaleString(NUMBER_LOCALE[lang])}</dd></div>}</dl>
    {edge.type === 'AGGREGATED'
      ? <p className="muted">{t('inspector.aggregatedText')}</p>
      : <>
        <div className="section-heading"><h3>{t('inspector.edgeSources')}</h3><span>{edgeEvidence.length}</span></div>
        <p className="muted small-text">{t('inspector.edgeProvenance')}</p>
        {detailError && <p className="inline-warning" role="status">{detailError}</p>}
        {!edgeEvidence.length && <p className="muted">{t('inspector.noEvidence')}</p>}
        <ul className="evidence-list">{edgeEvidence.slice(0, 100).map((doc, index) => <li key={`${doc.id}:${index}`}><span className="evidence-index">{String(index + 1).padStart(2, '0')}</span><div><strong>{doc.title}</strong><p>{doc.source || t('inspector.sourceMissing')} {doc.date && `· ${doc.date}`}</p>{safeUrl(doc.url) && <a href={safeUrl(doc.url)} target="_blank" rel="noopener noreferrer">{t('inspector.originalSource')}</a>}</div></li>)}</ul>
        {edgeEvidence.length > 100 && <p className="muted">{t('inspector.evidenceCap')}</p>}
      </>}
  </aside>;
  if (!node) return null;
  const exported = cypherExport(node.id);
  return <aside className="detail-panel" role="region" aria-labelledby="inspector-heading">
    <div className="panel-row"><span className="eyebrow">{t('inspector.eyebrow')}</span><button className="icon-button" onClick={onClose} aria-label={t('inspector.close')}>×</button></div>
    <div className="node-badge" style={{ color: COLORS[node.type] }}>{node.group ? t('inspector.groupBadge') : node.type.toUpperCase()}</div>
    <h2 className="node-title" id="inspector-heading">{node.label}</h2><code className="node-key">{node.id}</code>
    <dl className="node-facts"><div><dt>{node.group ? t('inspector.confidenceAvg') : t('inspector.confidence')}</dt><dd>{(node.confidence * 100).toFixed(1)} %</dd></div><div><dt>{node.group ? t('inspector.members') : t('inspector.community')}</dt><dd>{node.group ? node.count?.toLocaleString(NUMBER_LOCALE[lang]) : node.community ? <><span className="community-chip" aria-hidden="true" style={{ background: communityColor(node.community) }} /> {node.community}</> : node.cluster}</dd></div>
      {node.group && <div><dt>{t('inspector.internalEdges')}</dt><dd>{node.internalEdges}</dd></div>}
      {!node.group && node.rank !== undefined && <div><dt>{t('inspector.pagerank')}</dt><dd>{(node.rank * 100).toFixed(2)} %</dd></div>}
      {!node.group && node.degree !== undefined && <div><dt>{t('inspector.degree')}</dt><dd>{node.degree.toLocaleString(NUMBER_LOCALE[lang])}</dd></div>}
      {!node.group && node.firstSeen && <div><dt>{t('inspector.firstSeen')}</dt><dd><time dateTime={node.firstSeen}>{node.firstSeen}</time></dd></div>}
      {!node.group && node.lastSeen && node.lastSeen !== node.firstSeen && <div><dt>{t('inspector.lastSeen')}</dt><dd><time dateTime={node.lastSeen}>{node.lastSeen}</time></dd></div>}
      {node.lat !== undefined && <div><dt>{t('inspector.coords')}</dt><dd>{node.lat.toFixed(4)}, {node.lon?.toFixed(4)}</dd></div>}</dl>
    {onEgo && <button type="button" className="outline-button ego-expand" onClick={() => onEgo(node.id)}>{t('inspector.egoButton')}</button>}
    {!node.group && onNote && <div className="note-block">
      <div className="section-heading"><h3>{t('inspector.noteTitle')}</h3><span className="note-counter">{t('inspector.noteCount', { count: draft.length, max: NOTE_LIMIT })}</span></div>
      <textarea aria-label={t('inspector.noteTitle')} value={draft} maxLength={NOTE_LIMIT} rows={4} placeholder={t('inspector.notePlaceholder')} onChange={e => setDraft(e.target.value)} />
      <div className="note-actions"><button type="button" className="primary-button" onClick={() => { onNote(normalizeNote(draft)); setNoteStatus(t('inspector.noteSaved')); }}>{t('inspector.noteSave')}</button><button type="button" className="text-button" onClick={() => { setDraft(''); onNote(''); setNoteStatus(t('inspector.noteCleared')); }}>{t('inspector.noteClear')}</button></div>
      <p className="muted small-text">{t('inspector.noteHint')}</p>
      <p className="muted" role="status">{noteStatus}</p>
    </div>}
    {node.group ? <><p className="muted">{t('inspector.groupText')}</p><button className="primary-button" onClick={() => onDrill(node.id)}>{t('inspector.drill')}</button><h3>{t('inspector.examples')}</h3><ul className="example-ids">{node.examples?.map(id => <li key={id}>{id}</li>)}</ul></> : <>
      <div className="section-heading"><h3>{t('inspector.nodeSources')}</h3><span>{evidence.length}</span></div><p className="muted small-text">{t('inspector.nodeSourcesSub')}</p>
      {detailError && <p className="inline-warning" role="status">{detailError}</p>}
      {!evidence.length && <p className="muted">{t('inspector.noEvidence')}</p>}
      <ul className="evidence-list">{evidence.slice(0, 100).map((doc, index) => <li key={`${doc.id}:${index}`}><span className="evidence-index">{String(index + 1).padStart(2, '0')}</span><div><strong>{doc.title}</strong><p>{doc.source || t('inspector.sourceMissing')} {doc.date && `· ${doc.date}`}</p>{safeUrl(doc.url) && <a href={safeUrl(doc.url)} target="_blank" rel="noopener noreferrer">{t('inspector.originalSource')}</a>}</div></li>)}</ul>
      {evidence.length > 100 && <p className="muted">{t('inspector.evidenceCap')}</p>}
      <h3>{t('inspector.cypher')}</h3><p className="muted small-text">{t('inspector.cypherText')}</p><pre className="cypher-preview">{exported.query}</pre>
      <div className="export-actions"><button onClick={() => download('puppetnet-query.cypher', exported.query, 'text/plain')}>{t('inspector.queryDownload')}</button><button onClick={() => download('puppetnet-parameters.json', JSON.stringify(exported.parameters, null, 2), 'application/json')}>{t('inspector.paramsDownload')}</button><button onClick={async () => { try { await navigator.clipboard.writeText(JSON.stringify(exported, null, 2)); setCopied(t('inspector.copied')); } catch { setCopied(t('inspector.clipboardBlocked')); } }}>{t('inspector.copy')}</button></div><p className="muted" role="status">{copied}</p>
    </>}
  </aside>;
}
