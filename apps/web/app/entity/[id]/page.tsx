import type { Metadata } from 'next';
import Link from 'next/link';
import { notFound } from 'next/navigation';
import {
  ENTITY_TYPE_LABELS,
  buildEntityJsonLd,
  buildEntityMetadata,
  entityPath,
  serializeJsonLd,
  siteOrigin,
} from '../../../lib/entity-seo';
import { findEntityBySlug, listPublicEntities, loadEntityDataset } from '../../../lib/entity-store';
import './entity.css';

/** Static export: no server, no ISR. Every profile is a file in out/entity/. */
export const dynamic = 'force-static';
export const dynamicParams = false;

type Params = { id: string };

export function generateStaticParams(): Params[] {
  const profiles = listPublicEntities().map((entity) => ({ id: entity.slug }));
  // Next 15 static export rejects an empty dynamic-route parameter list.
  // The reserved non-slug hits notFound(), so no fictitious profile is published.
  return profiles.length ? profiles : [{ id: '__empty-publication' }];
}

export async function generateMetadata({ params }: { params: Promise<Params> }): Promise<Metadata> {
  const { id } = await params;
  const entity = findEntityBySlug(id);
  if (!entity) {
    return { title: 'Entität nicht gefunden', robots: { index: false, follow: false } };
  }
  return buildEntityMetadata(entity);
}

function formatConfidence(value: number): string {
  return `${(value * 100).toFixed(0)} %`;
}

export default async function EntityPage({ params }: { params: Promise<Params> }) {
  const { id } = await params;
  const entity = findEntityBySlug(id);
  if (!entity) notFound();
  const { dataset, source } = loadEntityDataset();
  const origin = siteOrigin();
  const jsonLd = serializeJsonLd(buildEntityJsonLd(entity, origin));
  const typeLabel = ENTITY_TYPE_LABELS[entity.type];

  return (
    <main id="main-content" tabIndex={-1} className="entity-profile">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: jsonLd }} />
      <nav className="entity-breadcrumb" aria-label="Brotkrumen">
        <ol>
          <li><Link href="/">Startseite</Link></li>
          <li><Link href="/dashboard/">Netzwerkanalyse</Link></li>
          <li aria-current="page">{entity.name}</li>
        </ol>
      </nav>

      {dataset.demo && (
        <p className="entity-demo" role="note">
          <strong>Fiktive Demodaten.</strong> Alle Entitäten, Beziehungen und Quellen auf dieser Seite sind erfunden und dienen ausschließlich der Demonstration des Graphen.
        </p>
      )}

      <header className="entity-header">
        <p className="entity-kind">{typeLabel}</p>
        <h1>{entity.name}</h1>
        {entity.aliases.length > 0 && (
          <p className="entity-aliases">
            Auch bekannt als {entity.aliases.join(', ')}.
          </p>
        )}
        <p className="entity-lede">{entity.description}</p>
      </header>

      <div className="entity-grid">
        <section className="entity-card" aria-labelledby="facts-heading">
          <h2 id="facts-heading">Erfasste Angaben</h2>
          <dl className="entity-facts">
            <div><dt>Typ</dt><dd>{typeLabel}</dd></div>
            <div><dt>Confidence</dt><dd>{formatConfidence(entity.confidence)}</dd></div>
            <div><dt>Aktualisiert</dt><dd><time dateTime={entity.updatedAt}>{entity.updatedAt}</time></dd></div>
            {entity.jurisdiction && <div><dt>Zuständigkeit</dt><dd>{entity.jurisdiction}</dd></div>}
            {entity.address && <div><dt>Sitz</dt><dd>{entity.address}</dd></div>}
            <div><dt>Kanonischer Schlüssel</dt><dd><code>{entity.id}</code></dd></div>
          </dl>
          {entity.identifiers.length > 0 && (
            <>
              <h3>Identifikatoren</h3>
              <dl className="entity-facts entity-identifiers">
                {entity.identifiers.map((identifier) => (
                  <div key={`${identifier.property}:${identifier.value}`}>
                    <dt>{identifier.property}</dt>
                    <dd><code>{identifier.value}</code></dd>
                  </div>
                ))}
              </dl>
            </>
          )}
        </section>

        <section className="entity-card" aria-labelledby="evidence-heading">
          <h2 id="evidence-heading">Quellenreferenzen</h2>
          <p className="entity-hint">
            EVIDENCED_BY- und MENTIONS-Kanten des geladenen Datensatzes. Eine Referenz belegt die Existenz einer Quelle, nicht die Richtigkeit jeder Aussage.
          </p>
          {entity.evidence.length === 0 ? (
            <p className="entity-empty">Keine Quellenreferenz im geladenen Datensatz. Das bedeutet nicht, dass die Angaben belegt sind.</p>
          ) : (
            <ol className="entity-evidence">
              {entity.evidence.map((item, index) => (
                <li key={item.id}>
                  <span className="entity-evidence-index" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
                  <div>
                    <strong>{item.title}</strong>
                    <p>
                      {item.source || 'Quelle nicht angegeben'}
                      {item.date && <> · <time dateTime={item.date}>{item.date}</time></>}
                    </p>
                    {item.url && (
                      <a href={item.url} target="_blank" rel="noopener noreferrer nofollow">
                        Originalquelle<span className="visually-hidden"> (öffnet in einem neuen Tab)</span> ↗
                      </a>
                    )}
                  </div>
                </li>
              ))}
            </ol>
          )}
        </section>
      </div>

      <section className="entity-card entity-related" aria-labelledby="related-heading">
        <h2 id="related-heading">Beziehungen im Graphen</h2>
        <p className="entity-hint">
          Kanten aus dem geladenen Ausschnitt. Berechnete oder gemeinsame Erwähnungen sind keine Tatsachenbehauptungen über Kontrolle oder Fehlverhalten.
        </p>
        {entity.related.length === 0 ? (
          <p className="entity-empty">Keine öffentliche Beziehung im geladenen Ausschnitt.</p>
        ) : (
          <ul className="entity-relations">
            {entity.related.map((related) => (
              <li key={`${related.id}:${related.predicate}:${related.direction}`}>
                <code className="entity-predicate">{related.predicate}</code>
                <span className="visually-hidden">
                  {related.direction === 'out' ? ' ausgehende Beziehung zu ' : ' eingehende Beziehung von '}
                </span>
                <span aria-hidden="true" className="entity-arrow">{related.direction === 'out' ? '→' : '←'}</span>
                <Link href={entityPath(related.slug)}>{related.name}</Link>
                <span className="entity-related-type">{ENTITY_TYPE_LABELS[related.type]}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <nav className="entity-actions" aria-label="Weiterführende Ansichten">
        <Link className="entity-button" href={`/dashboard/?focus=${encodeURIComponent(entity.id)}`}>
          Im Netzwerkgraphen öffnen
        </Link>
        <Link className="entity-link" href="/">Zur Startseite</Link>
      </nav>

      <footer className="entity-footer">
        <p>
          Profil generiert aus dem <code>{source === 'export' ? 'veröffentlichten' : 'fiktiven Demo-'}Datensatz</code>
          {' '}vom <time dateTime={dataset.generatedAt}>{dataset.generatedAt}</time>.
          Berechnete Beziehungen sind keine Tatsachenbehauptungen.
        </p>
      </footer>
    </main>
  );
}
