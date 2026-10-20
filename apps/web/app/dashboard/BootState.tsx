'use client';
// Explicit import: the component test runs under tsx's classic JSX transform.
import React from 'react';
import type { Standby } from '../../src/lib/dataFetcher';

/**
 * Dashboard boot states. The visualization grid only exists once a dataset is
 * actually present, so the two states a first visit can produce — "still
 * loading /data/graph_latest.json" and "no usable publication" — get their own
 * calm surface instead of an error alert or an empty table.
 */

export function LoadingStage() {
  return (
    <section className="boot-stage" aria-busy="true" aria-live="polite" role="status">
      <div className="boot-card">
        <span className="boot-icon boot-spinner" aria-hidden="true">◎</span>
        <p className="eyebrow">WIRD GELADEN</p>
        <h2>Veröffentlichter Datensatz wird geprüft.</h2>
        <p>Die statische Graph-Datei <code>/data/graph_latest.json</code> wird abgerufen. Das dauert normalerweise nur einen Moment.</p>
      </div>
    </section>
  );
}

const REASON_LABEL: Record<Standby['reason'], string> = {
  unavailable: 'Kein Datensatz gefunden',
  empty: 'Leere Veröffentlichung',
};

export interface StandbyPanelProps {
  standby: Standby;
  busy: boolean;
  onDemo: () => void;
  onResearch: () => void;
  onReload: () => void;
}

export default function StandbyPanel({ standby, busy, onDemo, onResearch, onReload }: StandbyPanelProps) {
  return (
    <section className="boot-stage standby" aria-labelledby="standby-title">
      <div className="boot-card">
        <span className="boot-icon" aria-hidden="true">⏾</span>
        <p className="eyebrow">BEREITSCHAFTSMODUS · {REASON_LABEL[standby.reason]}</p>
        <h2 id="standby-title">System im Bereitschaftsmodus.</h2>
        <p>Zeige Demonstrationsdaten oder starte eine On-Demand-Recherche.</p>
        <p className="boot-reason" role="note">{standby.message}</p>
        <div className="boot-actions">
          <button type="button" className="primary-button" onClick={onDemo} disabled={busy}>Demo-Datensatz laden</button>
          <button type="button" className="outline-button" onClick={onResearch}>On-Demand-Recherche ↗</button>
        </div>
        <button type="button" className="text-button boot-reload" onClick={onReload} disabled={busy}>Veröffentlichte Daten erneut prüfen</button>
        <p className="boot-note">Der integrierte Demo-Datensatz besteht aus klar gekennzeichneten fiktiven Beispieldaten — keine Rechercheergebnisse und keine veröffentlichten Angaben über reale Personen.</p>
      </div>
    </section>
  );
}
