'use client';
import { useState } from 'react';
import { COLORS, communityColor, cypherExport, safeUrl, type Evidence, type GraphEdge, type GraphNode } from './graph-data';
import { downloadText as download } from './download';
export default function SidePanel({ node, evidence, edge, edgeEvidence, edgeLabels, onDrill, onClose, detailError }: { node: GraphNode | null; evidence: Evidence[]; edge: GraphEdge | null; edgeEvidence: Evidence[]; edgeLabels: { source: string; target: string }; onDrill: (id: string) => void; onClose: () => void; detailError: string }) {
  const [copied, setCopied] = useState('');
  if (!node && !edge) return <aside className="detail-panel" role="region" aria-labelledby="inspector-heading"><span className="eyebrow">INSPECTOR / 03</span><div className="empty-inspector"><div className="inspect-icon" aria-hidden="true">◎</div><h2 id="inspector-heading">Jede Verbindung<br />beginnt mit einer Frage.</h2><p>Wähle einen Knoten oder eine Kante im Graphen, auf der Karte oder in der Datenliste. Hier erscheinen Eigenschaften und überprüfbare Quellen.</p></div><div className="inspector-foot">RECHERCHEHINWEISE ≠ TATSACHENBEWEISE</div></aside>;
  if (edge && !node) return <aside className="detail-panel" role="region" aria-labelledby="inspector-heading">
    <div className="panel-row"><span className="eyebrow">INSPECTOR / 03</span><button className="icon-button" onClick={onClose} aria-label="Details schließen">×</button></div>
    <div className="node-badge edge-badge">KANTE · {edge.type}</div>
    <h2 className="node-title" id="inspector-heading">{edgeLabels.source} → {edgeLabels.target}</h2>
    <code className="node-key">{edge.id}</code>
    <dl className="node-facts"><div><dt>Confidence</dt><dd>{(edge.confidence * 100).toFixed(1)} %</dd></div><div><dt>Beziehungsgewicht</dt><dd>{(edge.weight * 100).toFixed(1)} %</dd></div>
      {edge.count !== undefined && <div><dt>Gebündelte Kanten</dt><dd>{edge.count.toLocaleString('de-DE')}</dd></div>}</dl>
    {edge.type === 'AGGREGATED'
      ? <p className="muted">Darstellungsgruppen-Kante (adaptives Detailniveau): gebündelte Verbindungen ohne eigene Quellenliste. LoD deaktivieren oder näher zoomen — in der Einzelknoten-Ansicht trägt jede Kante ihre Quellnachweise.</p>
      : <>
        <div className="section-heading"><h3>Quellen dieser Kante</h3><span>{edgeEvidence.length}</span></div>
        <p className="muted small-text">Jede veröffentlichte Kante nennt die Dokumente, aus denen sie extrahiert wurde — gemeinsame Erwähnung ist kein Beweis für Kontrolle oder Fehlverhalten.</p>
        {detailError && <p className="inline-warning" role="status">{detailError}</p>}
        {!edgeEvidence.length && <p className="muted">Keine Dokumentreferenzen im geladenen Ausschnitt. Das bedeutet nicht, dass die Aussage belegt ist.</p>}
        <ul className="evidence-list">{edgeEvidence.slice(0, 100).map((doc, index) => <li key={`${doc.id}:${index}`}><span className="evidence-index">{String(index + 1).padStart(2, '0')}</span><div><strong>{doc.title}</strong><p>{doc.source || 'Quelle nicht angegeben'} {doc.date && `· ${doc.date}`}</p>{safeUrl(doc.url) && <a href={safeUrl(doc.url)} target="_blank" rel="noopener noreferrer">Originalquelle ↗</a>}</div></li>)}</ul>
        {edgeEvidence.length > 100 && <p className="muted">Anzeige auf 100 Nachweise begrenzt.</p>}
      </>}
  </aside>;
  if (!node) return null;
  const exported = cypherExport(node.id);
  return <aside className="detail-panel" role="region" aria-labelledby="inspector-heading">
    <div className="panel-row"><span className="eyebrow">INSPECTOR / 03</span><button className="icon-button" onClick={onClose} aria-label="Details schließen">×</button></div>
    <div className="node-badge" style={{ color: COLORS[node.type] }}>{node.group ? 'DARSTELLUNGSGRUPPE' : node.type.toUpperCase()}</div>
    <h2 className="node-title" id="inspector-heading">{node.label}</h2><code className="node-key">{node.id}</code>
    <dl className="node-facts"><div><dt>Confidence{node.group ? ' · Mittelwert' : ''}</dt><dd>{(node.confidence * 100).toFixed(1)} %</dd></div><div><dt>{node.group ? 'Enthaltene Knoten' : 'Community/Cluster'}</dt><dd>{node.group ? node.count?.toLocaleString('de-DE') : node.community ? <><span className="community-chip" aria-hidden="true" style={{ background: communityColor(node.community) }} /> {node.community}</> : node.cluster}</dd></div>
      {node.group && <div><dt>Interne Kanten</dt><dd>{node.internalEdges}</dd></div>}
      {!node.group && node.rank !== undefined && <div><dt>PageRank (global)</dt><dd>{(node.rank * 100).toFixed(2)} %</dd></div>}
      {!node.group && node.degree !== undefined && <div><dt>Verbindungen (gesamt)</dt><dd>{node.degree.toLocaleString('de-DE')}</dd></div>}
      {!node.group && node.firstSeen && <div><dt>Erste Erwähnung</dt><dd><time dateTime={node.firstSeen}>{node.firstSeen}</time></dd></div>}
      {!node.group && node.lastSeen && node.lastSeen !== node.firstSeen && <div><dt>Letzte Erwähnung</dt><dd><time dateTime={node.lastSeen}>{node.lastSeen}</time></dd></div>}
      {node.lat !== undefined && <div><dt>Koordinaten</dt><dd>{node.lat.toFixed(4)}, {node.lon?.toFixed(4)}</dd></div>}</dl>
    {node.group ? <><p className="muted">Diese LoD-Gruppe reduziert die Zeichenlast. Sie ist keine analytisch belegte Community. Kanten stehen für aggregierte Verbindungen.</p><button className="primary-button" onClick={() => onDrill(node.id)}>Gruppe untersuchen →</button><h3>Beispiel-IDs</h3><ul className="example-ids">{node.examples?.map(id => <li key={id}>{id}</li>)}</ul></> : <>
      <div className="section-heading"><h3>Quellnachweise</h3><span>{evidence.length}</span></div><p className="muted small-text">EVIDENCED_BY / MENTIONS / Dokumentreferenzen</p>
      {detailError && <p className="inline-warning" role="status">{detailError}</p>}
      {!evidence.length && <p className="muted">Keine Dokumentreferenzen im geladenen Ausschnitt. Das bedeutet nicht, dass die Aussage belegt ist.</p>}
      <ul className="evidence-list">{evidence.slice(0, 100).map((doc, index) => <li key={`${doc.id}:${index}`}><span className="evidence-index">{String(index + 1).padStart(2, '0')}</span><div><strong>{doc.title}</strong><p>{doc.source || 'Quelle nicht angegeben'} {doc.date && `· ${doc.date}`}</p>{safeUrl(doc.url) && <a href={safeUrl(doc.url)} target="_blank" rel="noopener noreferrer">Originalquelle ↗</a>}</div></li>)}</ul>
      {evidence.length > 100 && <p className="muted">Anzeige auf 100 Nachweise begrenzt.</p>}
      <h3>Cypher-Export</h3><p className="muted small-text">Parametrisierte Abfrage. Die Knoten-ID wird nicht in Cypher interpoliert.</p><pre className="cypher-preview">{exported.query}</pre>
      <div className="export-actions"><button onClick={() => download('puppetnet-query.cypher', exported.query, 'text/plain')}>Abfrage ↓</button><button onClick={() => download('puppetnet-parameters.json', JSON.stringify(exported.parameters, null, 2), 'application/json')}>Parameter ↓</button><button onClick={async () => { try { await navigator.clipboard.writeText(JSON.stringify(exported, null, 2)); setCopied('Kopiert'); } catch { setCopied('Zwischenablage blockiert – Export verwenden'); } }}>Kopieren</button></div><p className="muted" role="status">{copied}</p>
    </>}
  </aside>;
}
