import type { Metadata } from 'next';
import Link from 'next/link';
import { ENTITY_TYPE_LABELS, entityPath } from '../lib/entity-seo';
import { listPublicEntities, loadEntityDataset } from '../lib/entity-store';

export const metadata: Metadata = {
  title: 'PuppetNET — Folge der Verbindung.',
  description: 'Ein Name ist erst der Anfang. Entdecke Beziehungen, erkunde Netzwerke und finde deinen nächsten Rechercheansatz. Starte die interaktive Demo.',
  alternates: { canonical: '/' },
  openGraph: { url: '/', title: 'Ein Name. Ein Netzwerk. Eine neue Perspektive.', description: 'Was verbindet sie? Entdecke es mit PuppetNET. Interaktive Netzwerkanalyse, Quellen und neue Recherchewege.' },
};

export default function Home() {
  const { dataset } = loadEntityDataset();
  const entities = listPublicEntities().slice(0, 6);
  return <main id="main-content" className="landing" tabIndex={-1}>
    <nav className="landing-nav" aria-label="Hauptnavigation"><Link className="landing-brand" href="/"><img className="brand-art" src="/media/logo.webp" width="56" height="56" alt="" /><strong>PUPPETNET</strong></Link><div><a href="#entdecken">Entdecken</a><a href="#so-gehts">So funktioniert’s</a><Link className="nav-launch" href="/dashboard/">Workspace öffnen ↗</Link></div></nav>
    <section className="hero" aria-labelledby="hero-title">
      <div className="hero-copy"><span className="eyebrow">FÜR ALLE, DIE WEITERFRAGEN.</span><h1 id="hero-title">Ein Name ist<br />erst der <em>Anfang.</em></h1><p>Die spannende Geschichte liegt oft dazwischen. Mach Verbindungen sichtbar, folge neuen Spuren und sieh das Netzwerk hinter den Namen.</p><div className="hero-actions"><Link className="cta-primary" href="/dashboard/">Netzwerk entdecken <span>↗</span></Link><a className="cta-secondary" href="#entdecken">Erst umsehen ↓</a></div><span className="hero-note">Direkt im Browser. Keine Anmeldung für die Demo.</span></div>
      <div className="hero-network" role="img" aria-label="Illustration eines Netzwerks mit verbundenen Knoten; keine Analysedaten"><div className="orbit orbit-one" /><div className="orbit orbit-two" /><svg viewBox="0 0 500 460" aria-hidden="true"><g stroke="currentColor" strokeWidth="1"><path d="M250 220 100 105 75 285 250 220 390 90 425 290 250 220 315 390 75 285M100 105 390 90M425 290 315 390M100 105 315 390M390 90 75 285" /></g>{[[250,220,17],[100,105,9],[75,285,11],[390,90,8],[425,290,12],[315,390,8],[160,360,5],[330,170,5]].map(([x,y,r],i)=><circle key={i} cx={x} cy={y} r={r} className={i%3===0?'node-gold':'node-green'} />)}</svg><span className="network-label label-one">DIE PERSON.</span><span className="network-label label-two">DIE VERBINDUNG.</span><span className="network-label label-three">DEINE NÄCHSTE FRAGE.</span><div className="network-caption"><span className="pulse-dot" /> PERSPEKTIVE WECHSELN. MUSTER ENTDECKEN.</div></div>
    </section>
    <div className="value-strip"><span>◉ Interaktive Netzwerke</span><span>↗ Neue Recherchewege</span><span>◎ Quellen im Blick</span><span>⌘ Deine Daten. Deine Perspektive.</span></div>
    <section id="so-gehts" className="story-section"><div className="section-intro"><span className="eyebrow">VOM NAMEN ZUM ZUSAMMENHANG</span><h2>Weniger Listen.<br />Mehr Überblick.</h2><p>Einzelne Treffer erzählen nur einen Teil. PuppetNET bringt Personen, Organisationen und Orte in einen gemeinsamen Kontext.</p></div><div className="feature-grid">{[
      ['01','Sieh, was zusammenhängt.','Wechsle zwischen Netzwerk, Karte und Tabelle. Entdecke Beziehungen aus einer anderen Perspektive.'],
      ['02','Folge deiner Neugier.','Öffne Details, grenze den Ausschnitt ein und finde den nächsten Ansatzpunkt für deine Recherche.'],
      ['03','Frag nach dem Beleg.','Prüfe verfügbare Quellen und Referenzen. Eine Verbindung ist ein Hinweis – noch kein Beweis.'],
    ].map(([n,title,text])=><article key={n}><span className="feature-number">{n} /</span><h3>{title}</h3><p>{text}</p></article>)}</div></section>
    <section id="entdecken" className="profile-index"><div className="section-intro"><span className="eyebrow">DEIN EINSTIEG INS NETZWERK</span><h2>Jede Spur beginnt mit einer Frage.</h2><p>Wähle einen Einstiegspunkt und entdecke die Beziehungen dahinter.</p></div>
      {dataset.demo && <p className="demo-disclosure" role="note"><strong>Interaktive Demo:</strong> Diese Profile und ihre Beziehungen sind fiktive Beispieldaten, keine Rechercheergebnisse.</p>}
      <ul className="profile-grid">{entities.map(entity=><li className="profile-card" key={entity.id}><span className="profile-kind">{ENTITY_TYPE_LABELS[entity.type]}</span><h3>{entity.name}</h3><p>{entity.description}</p><Link href={entityPath(entity.slug)}>Spur entdecken<span className="visually-hidden">: {entity.name}</span> ↗</Link></li>)}</ul>
    </section>
    <section className="closing-cta"><span className="eyebrow">WAS VERBINDET SIE?</span><h2>Die nächste Verbindung<br />wartet auf deinen Blick.</h2><p>Öffne die Demo. Wechsle die Perspektive. Stell die nächste Frage.</p><Link className="cta-primary" href="/dashboard/">Jetzt selbst entdecken ↗</Link></section>
    <footer className="landing-footer"><Link className="landing-brand" href="/"><img className="brand-art" src="/media/logo.webp" width="56" height="56" alt="" /><strong>PUPPETNET</strong></Link><p>Verbindungen sehen. Behauptungen hinterfragen.</p><div><Link href="/dashboard/">Workspace</Link><a href="/legacy/">Analystenkonsole & Rechtliches ↗</a><a href="/sitemap.xml">Sitemap</a></div></footer>
  </main>;
}
