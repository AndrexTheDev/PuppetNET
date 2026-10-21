'use client';
// Explicit import: the component test runs under tsx's classic JSX transform.
import React from 'react';
import type { Standby } from '../../src/lib/dataFetcher';
import { useT } from './i18n';

/**
 * Dashboard boot states. The visualization grid only exists once a dataset is
 * actually present, so the two states a first visit can produce — "still
 * loading /data/graph_latest.json" and "no usable publication" — get their own
 * calm surface instead of an error alert or an empty table.
 *
 * Wave 6: chrome strings come from the dictionary; without a language provider
 * (static rendering, component tests) everything stays byte-identical German.
 */

export function LoadingStage() {
  const t = useT();
  return (
    <section className="boot-stage" aria-busy="true" aria-live="polite" role="status">
      <div className="boot-card">
        <span className="boot-icon boot-spinner" aria-hidden="true">◎</span>
        <p className="eyebrow">{t('boot.loading')}</p>
        <h2>{t('boot.loadingTitle')}</h2>
        <p>{t('boot.loadingPre')}<code>/data/graph_latest.json</code>{t('boot.loadingPost')}</p>
      </div>
    </section>
  );
}

export interface StandbyPanelProps {
  standby: Standby;
  busy: boolean;
  onDemo: () => void;
  onResearch: () => void;
  onReload: () => void;
}

export default function StandbyPanel({ standby, busy, onDemo, onResearch, onReload }: StandbyPanelProps) {
  const t = useT();
  const reason = standby.reason === 'empty' ? t('boot.reasonEmpty') : t('boot.reasonUnavailable');
  return (
    <section className="boot-stage standby" aria-labelledby="standby-title">
      <div className="boot-card">
        <span className="boot-icon" aria-hidden="true">⏾</span>
        <p className="eyebrow">{t('boot.standbyEyebrow', { reason })}</p>
        <h2 id="standby-title">{t('boot.standbyTitle')}</h2>
        <p>{t('boot.standbyText')}</p>
        <p className="boot-reason" role="note">{standby.message}</p>
        <div className="boot-actions">
          <button type="button" className="primary-button" onClick={onDemo} disabled={busy}>{t('boot.demoButton')}</button>
          <button type="button" className="outline-button" onClick={onResearch}>{t('boot.researchButton')}</button>
        </div>
        <button type="button" className="text-button boot-reload" onClick={onReload} disabled={busy}>{t('boot.reload')}</button>
        <p className="boot-note">{t('boot.demoNote')}</p>
      </div>
    </section>
  );
}
