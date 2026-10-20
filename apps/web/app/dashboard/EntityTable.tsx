'use client';
// Explicit import: the component test runs under tsx's classic JSX transform.
import React from 'react';

import { ENTITY_TYPE_LABELS, entityPath, entitySlug, isPublicEntityType } from '../../lib/entity-seo';
import type { GraphNode } from './graph-data';

export type TableRow = GraphNode & { degree: number };

interface Props {
  rows: TableRow[];
  total: number;
  page: number;
  pageSize: number;
  query: string;
  onQuery: (value: string) => void;
  onPage: (page: number) => void;
  onSelect: (id: string, group: boolean) => void;
  onFocusGraph: (id: string) => void;
}

/**
 * The text alternative to the WebGL canvas (WCAG 1.1.1, 2.1.1, 4.1.2).
 *
 * A real table — not a list of divs — so screen readers can announce row and
 * column headers, and every graph action (inspect, focus, open the profile)
 * exists here as an ordinary button or link. The scroll container is focusable
 * so a keyboard-only user can scroll long tables.
 */
export default function EntityTable({
  rows,
  total,
  page,
  pageSize,
  query,
  onQuery,
  onPage,
  onSelect,
  onFocusGraph,
}: Props) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const first = total === 0 ? 0 : page * pageSize + 1;
  const last = Math.min(total, (page + 1) * pageSize);
  return (
    <section className="data-list" aria-labelledby="table-heading">
      <div className="table-heading">
        <h2 id="table-heading">Tabellenansicht</h2>
        <p id="table-description">
          Textalternative zum WebGL-Graphen: jeder Knoten der geladenen Ansicht als Zeile, mit Tastatur und
          Screenreader bedienbar. Die Spalten zeigen Typ, Confidence, Verbindungsgrad, Rank und Community/Cluster.
        </p>
      </div>

      <label className="search-label" htmlFor="entity-table-search">Geladene Ansicht durchsuchen</label>
      <input
        id="entity-table-search"
        type="search"
        placeholder="Name oder ID …"
        autoComplete="off"
        value={query}
        onChange={(event) => onQuery(event.target.value)}
      />

      <div className="table-scroll" role="region" aria-label="Tabelle mit Bildlauf" tabIndex={0}>
        <table aria-describedby="table-description">
          <caption>
            Knoten der geladenen Ansicht. Seite {page + 1} von {pageCount}, Einträge {first} bis {last} von {total}.
          </caption>
          <thead>
            <tr>
              <th scope="col">Entität</th>
              <th scope="col">Typ</th>
              <th scope="col">Confidence</th>
              <th scope="col">Verbindungen</th>
              <th scope="col">Rank</th>
              <th scope="col">Community/Cluster</th>
              <th scope="col">Aktionen</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((node) => {
              const profile = !node.group && isPublicEntityType(node.type)
                ? entityPath(entitySlug(node.id, node.label))
                : '';
              return (
                <tr key={node.id}>
                  <th scope="row">
                    <span className="table-entity-label">{node.label}</span>
                    <code className="table-entity-key">{node.id}</code>
                  </th>
                  <td>{ENTITY_TYPE_LABELS[node.type]}</td>
                  <td>{(node.confidence * 100).toFixed(0)} %</td>
                  <td>{node.degree.toLocaleString('de-DE')}</td>
                  <td>{node.rank !== undefined ? `${(node.rank * 100).toFixed(1)} %` : '–'}</td>
                  <td>{node.group ? `${node.count?.toLocaleString('de-DE') ?? '?'} Knoten` : node.community || node.cluster || '–'}</td>
                  <td className="table-actions">
                    <button type="button" onClick={() => onSelect(node.id, Boolean(node.group))}>
                      Details<span className="visually-hidden"> zu {node.label}</span>
                    </button>
                    <button type="button" onClick={() => onFocusGraph(node.id)}>
                      Im Graph<span className="visually-hidden"> auf {node.label} fokussieren</span>
                    </button>
                    {profile && (
                      <a href={profile}>
                        Profil<span className="visually-hidden"> von {node.label}</span>
                      </a>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {rows.length === 0 && <p role="status">Keine passenden Einträge.</p>}
      </div>

      <nav className="pagination" aria-label="Tabellenseiten">
        <button type="button" disabled={page === 0} onClick={() => onPage(page - 1)} aria-label="Vorherige Seite">
          ←
        </button>
        <span aria-live="polite">Seite {page + 1} von {pageCount}</span>
        <button
          type="button"
          disabled={page + 1 >= pageCount}
          onClick={() => onPage(page + 1)}
          aria-label="Nächste Seite"
        >
          →
        </button>
      </nav>
    </section>
  );
}
