'use client';
// Explicit import: the component test runs under tsx's classic JSX transform.
import React from 'react';

import { entityPath, entitySlug, isPublicEntityType } from '../../lib/entity-seo';
import { NUMBER_LOCALE } from '../../lib/i18n-strings';
import type { GraphNode } from './graph-data';
import { useLanguage, useT } from './i18n';

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
 *
 * Wave 6: chrome strings come from the dictionary; without a language provider
 * (static rendering, component tests) the German markup stays byte-identical.
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
  const { lang } = useLanguage();
  const t = useT();
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const first = total === 0 ? 0 : page * pageSize + 1;
  const last = Math.min(total, (page + 1) * pageSize);
  return (
    <section className="data-list" aria-labelledby="table-heading">
      <div className="table-heading">
        <h2 id="table-heading">{t('table.heading')}</h2>
        <p id="table-description">{t('table.description')}</p>
      </div>

      <label className="search-label" htmlFor="entity-table-search">{t('table.searchLabel')}</label>
      <input
        id="entity-table-search"
        type="search"
        placeholder={t('table.searchPlaceholder')}
        autoComplete="off"
        value={query}
        onChange={(event) => onQuery(event.target.value)}
      />

      <div className="table-scroll" role="region" aria-label={t('table.scrollRegion')} tabIndex={0}>
        <table aria-describedby="table-description">
          <caption>{t('table.caption', { page: page + 1, pageCount, first, last, total })}</caption>
          <thead>
            <tr>
              <th scope="col">{t('table.colEntity')}</th>
              <th scope="col">{t('table.colType')}</th>
              <th scope="col">{t('table.colConfidence')}</th>
              <th scope="col">{t('table.colDegree')}</th>
              <th scope="col">{t('table.colRank')}</th>
              <th scope="col">{t('table.colCommunity')}</th>
              <th scope="col">{t('table.colActions')}</th>
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
                  <td>{t(`type.${node.type}`)}</td>
                  <td>{(node.confidence * 100).toFixed(0)} %</td>
                  <td>{node.degree.toLocaleString(NUMBER_LOCALE[lang])}</td>
                  <td>{node.rank !== undefined ? `${(node.rank * 100).toFixed(1)} %` : '–'}</td>
                  <td>{node.group ? t('table.groupCount', { count: node.count?.toLocaleString(NUMBER_LOCALE[lang]) ?? '?' }) : node.community || node.cluster || '–'}</td>
                  <td className="table-actions">
                    <button type="button" onClick={() => onSelect(node.id, Boolean(node.group))}>
                      {t('table.details')}<span className="visually-hidden">{t('table.detailsFor', { label: node.label })}</span>
                    </button>
                    <button type="button" onClick={() => onFocusGraph(node.id)}>
                      {t('table.inGraph')}<span className="visually-hidden">{t('table.inGraphFor', { label: node.label })}</span>
                    </button>
                    {profile && (
                      <a href={profile}>
                        {t('table.profile')}<span className="visually-hidden">{t('table.profileOf', { label: node.label })}</span>
                      </a>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {rows.length === 0 && <p role="status">{t('table.empty')}</p>}
      </div>

      <nav className="pagination" aria-label={t('table.pages')}>
        <button type="button" disabled={page === 0} onClick={() => onPage(page - 1)} aria-label={t('table.prev')}>
          ←
        </button>
        <span aria-live="polite">{t('table.pageOf', { page: page + 1, pageCount })}</span>
        <button
          type="button"
          disabled={page + 1 >= pageCount}
          onClick={() => onPage(page + 1)}
          aria-label={t('table.next')}
        >
          →
        </button>
      </nav>
    </section>
  );
}
