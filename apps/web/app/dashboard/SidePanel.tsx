'use client';
import { useState } from 'react';
import { COLORS, cypherExport, safeUrl, type Evidence, type GraphNode } from './graph-data';
function download(name: string, text: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type })), a = document.createElement('a');
  a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export default function SidePanel({ node, evidence, onDrill, onClose, detailError }: { node: GraphNode | null; evidence: Evidence[]; onDrill: (id: string) => void; onClose: () => void; detailError: string }) {
  const [copied, setCopied] = useState('');
  if (!node) return <aside className="detail-panel"><span className="eyebrow">INSPECTOR / 03</span><div className="empty-inspector"><div className="inspect-icon">◎</div><h2>Jede Verbindung<br />beginnt mit einer Frage.</h2><p>Wähle einen Knoten im Graphen, auf der Karte oder in der Datenliste. Hier erscheinen Eigenschaften und überprüfbare Quellen.</p></div><div className="inspector-foot">RECHERCHEHINWEISE ≠ TATSACHENBEWEISE</div></aside>;
  const exported = cypherExport(node.id);
  return <aside className="detail-panel" aria-label="Knotendetails">
    <div className="panel-row"><span className="eyebrow">INSPECTOR / 03</span><button className="icon-button" onClick={onClose} aria-label="Details schließen">×</button></div>
    <div className="node-badge" style={{ color: COLORS[node.type] }}>{node.group ? 'DARSTELLUNGSGRUPPE' : node.type.toUpperCase()}</div>
    <h2 className="node-title">{node.label}</h2><code className="node-key">{node.id}</code>
    <dl className="node-facts"><div><dt>Confidence{node.group ? ' · Mittelwert' : ''}</dt><dd>{(node.confidence * 100).toFixed(1)} %</dd></div><div><dt>{node.group ? 'Enthaltene Knoten' : 'Cluster-ID'}</dt><dd>{node.group ? node.count?.toLocaleString('de-DE') : node.cluster}</dd></div>
      {node.group && <div><dt>Interne Kanten</dt><dd>{node.internalEdges}</dd></div>}
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
