import type { Metadata } from 'next';
import Link from 'next/link';
import { siteOrigin } from '../../lib/entity-seo';
import './legal.css';

export const metadata: Metadata = {
  title: 'Recht, Löschung & Quellen-Policy',
  description:
    'Dokumentierter Lösch- und Widerspruchsablauf (DSGVO Art. 17 & 21), Quellen-Policy nach EuGH C-37/20 und C-601/20 sowie Kanten-Provenienz — öffentlich erklärt. Erasure workflow, source policy and edge provenance, documented in the open.',
  alternates: { canonical: '/legal/' },
  openGraph: {
    url: '/legal/',
    title: 'Recht, Löschung & Quellen-Policy — PuppetNET',
    description: 'Löschung in ≤ 24 Stunden, nur frei zugängliche öffentliche Quellen, jede Kante mit Quellnachweis.',
  },
};

const REPO = 'https://github.com/AndrexTheDev/PuppetNET';

export default function LegalPage() {
  return <main id="main-content" className="landing legal-doc" tabIndex={-1}>
    <nav className="landing-nav" aria-label="Hauptnavigation"><Link className="landing-brand" href="/"><img className="brand-art" src="/media/logo.webp" width="56" height="56" alt="" /><strong>PUPPETNET</strong></Link><div><Link href="/dashboard/">Workspace</Link><Link href="/">Startseite</Link></div></nav>
    <span className="eyebrow">TRANSPARENZ &amp; COMPLIANCE</span>
    <h1>Recht, Löschung &amp; Quellen-Policy.</h1>
    <p>PuppetNET veröffentlicht <strong>berechnete Verbindungshinweise</strong> aus öffentlichen Quellen — jede Kante trägt Confidence und Quellnachweise. Diese Seite dokumentiert offen, welche Rechte betroffene Personen haben, welche Quellen wir verwenden (und welche nicht) und wie jede Aussage im Graphen überprüfbar bleibt. This page is fully mirrored in English below.</p>

    <section aria-labelledby="loeschung">
      <h2 id="loeschung">1 · Löschung &amp; Widerspruch (DSGVO Art. 17 &amp; Art. 21)</h2>
      <p>Jede Person und jede Organisation, die im öffentlichen Graphen erscheint, kann <strong>Löschung</strong> (Art. 17) oder <strong>Widerspruch gegen die Verarbeitung</strong> (Art. 21) verlangen — ohne Anmeldung, ohne Kosten, ohne Begründungszwang.</p>
      <h3>So läuft eine Anfrage</h3>
      <ul>
        <li><strong>Kanal:</strong> ein Issue im öffentlichen Repository (<a href={REPO} rel="noopener noreferrer">{REPO}</a>) oder jeder andere dokumentierte Kontaktweg des Betreibers. Nenne Entitätsnamen oder Entitäts-ID (z. B. <code>person:anna-berg</code>) — eine Begründung ist optional.</li>
        <li><strong>Technische Kette:</strong> der Betreiber setzt die Entität auf <code>public_released: false</code> (bzw. entzieht die Freigabe über das <code>approved()</code>-Veto). Bereits beim nächsten Export werden die Entitäts-Slices über das Manifest-Pruning entfernt, alle Kanten zu der Entität verschwinden aus <code>graph_latest.json</code>, und veraltete Profilseiten löscht der Export physisch.</li>
        <li><strong>Frist:</strong> die Publikation läuft zweimal täglich (03:00 und 15:00 UTC). Eine Löschung ist damit regelmäßig <strong>innerhalb von 24 Stunden</strong> öffentlich wirksam, zuzüglich kurzer CDN-Nachvalidierung.</li>
        <li><strong>Suchmaschinen:</strong> für das Delisting bei Suchmaschinen verweisen wir auf die EDPB-Leitlinien 5/2019; Anfragen dort sind getrennt zu stellen. Wir stellen auf Wunsch die Dokumentation des Löschablaufs als Nachweis bereit.</li>
      </ul>
      <p className="legal-note"><strong>Ehrliche Grenze:</strong> Caches, Archive oder Kopien Dritter liegen außerhalb unserer Kontrolle. Wir löschen, was wir veröffentlichen — und dokumentieren, was wir nicht löschen können.</p>
    </section>

    <section aria-labelledby="quellen">
      <h2 id="quellen">2 · Quellen-Policy (EuGH C-37/20 &amp; C-601/20)</h2>
      <p>Der Europäische Gerichtshof hat mit den Urteilen vom 22. November 2022 (C-37/20 und C-601/20) den <strong>allgemeinen öffentlichen Zugang zu Registern wirtschaftlichen Eigentums</strong> für ungültig erklärt; das deutsche Transparenzregister erfordert seitdem ein berechtigtes Interesse. Konsequenzen für diese Installation:</p>
      <ul>
        <li>PuppetNET ingestiert <strong>ausschließlich frei zugängliche öffentliche Quellen</strong>: Nachrichten/RSS (Gewicht 0.4), Wikidata/OpenCorporates (0.9) und offizielle offene Register sowie öffentlich freigegebene Leaks-Datensätze (1.0).</li>
        <li><strong>Zugangsbeschränkte Register werden nicht gescrapt</strong> — weder das Transparenzregister noch vergleichbare Quellen hinter Berechtigungsinteressen.</li>
        <li>Betreiber, die eigene Quellen ergänzen, sind für deren Rechtsgrundlage selbst verantwortlich; die Gewichtungs- und Freigabekette (<code>public_released</code>, <code>approved()</code>, Review-Veto für sensible Prädikate) ist in <code>docs/source-policy.md</code> und <code>docs/daily-ingestion.md</code> dokumentiert.</li>
      </ul>
    </section>

    <section aria-labelledby="provenienz">
      <h2 id="provenienz">3 · Kanten-Provenienz &amp; Audit</h2>
      <ul>
        <li><strong>Jede Kante trägt ihre Quelle:</strong> im Workspace öffnet ein Klick auf eine Kante den Inspector mit genau den Dokumenten, aus denen diese Verbindung extrahiert wurde (Titel, Quelle, Datum, Original-Link).</li>
        <li><strong>Korrelations-Insights zitieren Kanten-IDs:</strong> jede Regel-Erkenntnis (Strohmann-Ketten, Sternstrukturen, Community-Brücken, Personenverflechtungen) verweist auf die tragenden Kanten und ist damit bis zum Dokument rückverfolgbar.</li>
        <li><strong>Rechnung ≠ Behauptung:</strong> Confidence-Werte, Community-Zugehörigkeiten und Zentralitätsmaße sind Strukturhinweise auf dem veröffentlichten Ausschnitt — keine Tatsachenbehauptungen und keine Schuldzuweisungen. Fiktive Demonstrationsdaten sind immer als solche gekennzeichnet.</li>
      </ul>
    </section>

    <section className="legal-lang" aria-labelledby="english" lang="en">
      <h2 id="english">English summary</h2>
      <h3>1 · Erasure &amp; objection (GDPR Art. 17 &amp; Art. 21)</h3>
      <p>Any person or organisation appearing in the public graph can request <strong>erasure</strong> or <strong>objection</strong> — no account, no fee, no justification required. File an issue in the public repository (<a href={REPO} rel="noopener noreferrer">{REPO}</a>) naming the entity (or its id). The operator sets <code>public_released: false</code>; the very next export prunes the entity slices via the manifest, removes every edge to that entity from <code>graph_latest.json</code> and deletes stale profile pages. Publication runs twice daily (03:00 and 15:00 UTC), so erasure normally takes effect <strong>within 24 hours</strong> plus short CDN revalidation. For search-engine delisting see EDPB Guidelines 5/2019 — requests there are separate, and we provide documentation of this workflow as supporting evidence. Honest limitation: caches, archives or copies held by third parties are beyond our control.</p>
      <h3>2 · Source policy (CJEU C-37/20 &amp; C-601/20)</h3>
      <p>Following the CJEU judgments of 22 November 2022, general public access to beneficial-ownership registers is invalid (in Germany, the Transparenzregister now requires a legitimate interest). PuppetNET therefore ingests <strong>freely accessible public sources only</strong> — news/RSS (weight 0.4), Wikidata/OpenCorporates (0.9), official open registers and publicly released leak datasets (1.0) — and <strong>never scrapes access-restricted registers</strong>. Operators adding their own sources are responsible for the legal basis; the weighting and release chain is documented in <code>docs/source-policy.md</code> and <code>docs/daily-ingestion.md</code>.</p>
      <h3>3 · Edge provenance &amp; audit</h3>
      <p>Every published edge carries confidence and its source documents: clicking an edge in the workspace opens an inspector listing exactly the documents that produced this connection. Correlation insights cite the edge ids they rest on, so every pattern is traceable to evidence. Computed connections are structural hints, never accusations; fictional demo data is always disclosed.</p>
    </section>

    <footer className="landing-footer"><Link className="landing-brand" href="/"><img className="brand-art" src="/media/logo.webp" width="56" height="56" alt="" /><strong>PUPPETNET</strong></Link><p>Verbindungen sehen. Behauptungen hinterfragen.</p><div><Link href="/dashboard/">Workspace</Link><Link href="/">Startseite</Link><a href="/sitemap.xml">Sitemap</a></div></footer>
    <p className="legal-meta"><span>Stand / Last updated: 2026-10-20</span><span>Origin: {siteOrigin()}</span><a href={REPO} rel="noopener noreferrer">Quellcode / Source code ↗</a></p>
  </main>;
}
