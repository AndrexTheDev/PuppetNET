import type { Metadata } from 'next';
import Link from 'next/link';
import { ENTITY_TYPE_LABELS, entityPath } from '../lib/entity-seo';
import { listPublicEntities, loadEntityDataset } from '../lib/entity-store';

export const metadata: Metadata = {
  alternates: { canonical: '/' },
  openGraph: { url: '/' },
};

export default function Home() {
  const { dataset } = loadEntityDataset();
  const entities = listPublicEntities().slice(0, 12);
  return (
    <main id="main-content" tabIndex={-1}>
      <header>
        <div>
          <span className="badge">PuppetNET 2.0 · Intelligence Workspace</span>
          <h1>Netzwerke verstehen. Quellen prüfen.</h1>
          <p>OSINT-Konsole mit nachvollziehbaren Belegen. Berechnete Verbindungen sind keine Tatsachenbehauptungen.</p>
        </div>
        <a href="/dashboard/">WebGL-Dashboard öffnen →</a>
      </header>

      {dataset.demo && (
        <p className="notice" role="note">
          <strong>Fiktive Demodaten:</strong> Die öffentlichen Profile unten sind Beispielinhalte. Ohne konfigurierte API zeigt die eingebettete Konsole ebenfalls ausdrücklich fiktive Demodaten an.
        </p>
      )}

      {entities.length > 0 && (
        <section className="profile-index" aria-labelledby="profile-index-heading">
          <h2 id="profile-index-heading">Öffentliche Entitätsprofile</h2>
          <p>
            Statisch vorgerenderte Profilseiten mit strukturierten Daten (Schema.org), Quellenreferenzen und
            nachvollziehbaren Beziehungen. Jede Seite ist ohne JavaScript lesbar.
          </p>
          <ul className="profile-grid">
            {entities.map((entity) => (
              <li className="profile-card" key={entity.id}>
                <span className="profile-kind">{ENTITY_TYPE_LABELS[entity.type]}</span>
                <h3>{entity.name}</h3>
                <p>{entity.description}</p>
                <Link href={entityPath(entity.slug)}>
                  Profil ansehen<span className="visually-hidden">: {entity.name}</span> →
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}

      <p className="notice">
        Die bestehende Analystenkonsole ist unverändert eingebettet. Ohne konfigurierte API verwendet sie ausdrücklich fiktive Demodaten. Livezugriff: Einstellungen → Cloudflare Worker.
      </p>
      <iframe title="PuppetNET Analystenkonsole" src="/legacy/index.html" />
    </main>
  );
}
