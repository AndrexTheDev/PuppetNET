import type { Metadata } from 'next';
import Link from 'next/link';
import { buildWebsiteJsonLd, englishDescription, entityPath, serializeJsonLd, siteOrigin } from '../../lib/entity-seo';
import { listPublicEntities, loadEntityDataset } from '../../lib/entity-store';
import { translate } from '../../lib/i18n-strings';

/**
 * English landing (Wave 6). Static export has no built-in i18n routing, so the
 * English publication is an explicit route: hreflang-linked from the German
 * landing, listed in every sitemap generator, and language-tagged on the
 * subtree (`<main lang="en">`) because nested routes cannot re-render <html>.
 *
 * Chrome is English; published entity data (names, descriptions) stays in its
 * source language — translations of data are never fabricated.
 */

export const metadata: Metadata = {
  title: 'PuppetNET — Follow the connection.',
  description: 'A name is only the beginning. Discover relationships, explore networks and find your next research lead. Start the interactive demo.',
  alternates: {
    canonical: '/en/',
    languages: { de: '/', en: '/en/', 'x-default': '/' },
  },
  openGraph: { url: '/en/', title: 'One name. One network. A new perspective.', description: 'What connects them? Find out with PuppetNET. Interactive network analysis, sources and new research paths.' },
};

export default function HomeEn() {
  const { dataset } = loadEntityDataset();
  const entities = listPublicEntities().slice(0, 6);
  const websiteJsonLd = serializeJsonLd({
    '@context': 'https://schema.org',
    ...buildWebsiteJsonLd(siteOrigin()),
  });
  return <main id="main-content" className="landing" tabIndex={-1} lang="en">
    <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: websiteJsonLd }} />
    <nav className="landing-nav" aria-label="Main navigation"><Link className="landing-brand" href="/en/"><img className="brand-art" src="/media/logo.webp" width="56" height="56" alt="" /><strong>PUPPETNET</strong></Link><div><a href="#discover">Discover</a><a href="#how-it-works">How it works</a><Link className="nav-launch" href="/dashboard/">Open workspace ↗</Link></div></nav>
    <section className="hero" aria-labelledby="hero-title">
      <div className="hero-copy"><span className="eyebrow">FOR EVERYONE WHO KEEPS ASKING.</span><h1 id="hero-title">A name is<br />only the <em>beginning.</em></h1><p>The interesting story is often in between. Make connections visible, follow new leads and see the network behind the names.</p><div className="hero-actions"><Link className="cta-primary" href="/dashboard/">Discover the network <span>↗</span></Link><a className="cta-secondary" href="#discover">Look around first ↓</a></div><span className="hero-note">Right in the browser. No registration for the demo.</span></div>
      <div className="hero-network" role="img" aria-label="Illustration of a network with connected nodes; no analysis data"><div className="orbit orbit-one" /><div className="orbit orbit-two" /><svg viewBox="0 0 500 460" aria-hidden="true"><g stroke="currentColor" strokeWidth="1"><path d="M250 220 100 105 75 285 250 220 390 90 425 290 250 220 315 390 75 285M100 105 390 90M425 290 315 390M100 105 315 390M390 90 75 285" /></g>{[[250,220,17],[100,105,9],[75,285,11],[390,90,8],[425,290,12],[315,390,8],[160,360,5],[330,170,5]].map(([x,y,r],i)=><circle key={i} cx={x} cy={y} r={r} className={i%3===0?'node-gold':'node-green'} />)}</svg><span className="network-label label-one">THE PERSON.</span><span className="network-label label-two">THE CONNECTION.</span><span className="network-label label-three">YOUR NEXT QUESTION.</span><div className="network-caption"><span className="pulse-dot" /> CHANGE PERSPECTIVE. DISCOVER PATTERNS.</div></div>
    </section>
    <div className="value-strip"><span>◉ Interactive networks</span><span>↗ New research paths</span><span>◎ Sources in view</span><span>⌘ Your data. Your perspective.</span></div>
    <section id="how-it-works" className="story-section"><div className="section-intro"><span className="eyebrow">FROM NAME TO CONTEXT</span><h2>Fewer lists.<br />More overview.</h2><p>Individual hits only tell part of the story. PuppetNET brings people, organizations and places into a shared context.</p></div><div className="feature-grid">{[
      ['01','See what belongs together.','Switch between network, map and table. Discover relationships from a different perspective.'],
      ['02','Follow your curiosity.','Open details, narrow the slice and find the next starting point for your research.'],
      ['03','Ask for the evidence.','Check available sources and references. A connection is a hint — not proof yet.'],
    ].map(([n,title,text])=><article key={n}><span className="feature-number">{n} /</span><h3>{title}</h3><p>{text}</p></article>)}</div></section>
    <section id="discover" className="profile-index"><div className="section-intro"><span className="eyebrow">YOUR ENTRY INTO THE NETWORK</span><h2>Every lead starts with a question.</h2><p>Choose an entry point and discover the relationships behind it.</p></div>
      {dataset.demo && <p className="demo-disclosure" role="note"><strong>Interactive demo:</strong> These profiles and their relationships are fictional sample data, not research results.</p>}
      <ul className="profile-grid">{entities.map(entity=><li className="profile-card" key={entity.id}><span className="profile-kind">{translate('en', `type.${entity.type}`)}</span><h3>{entity.name}</h3><p>{englishDescription(entity)}</p><Link href={entityPath(entity.slug)}>Discover the lead<span className="visually-hidden">: {entity.name}</span> ↗</Link></li>)}</ul>
    </section>
    <section className="closing-cta"><span className="eyebrow">WHAT CONNECTS THEM?</span><h2>The next connection<br />is waiting for your eyes.</h2><p>Open the demo. Change the perspective. Ask the next question.</p><Link className="cta-primary" href="/dashboard/">Discover it yourself ↗</Link></section>
    <footer className="landing-footer"><Link className="landing-brand" href="/en/"><img className="brand-art" src="/media/logo.webp" width="56" height="56" alt="" /><strong>PUPPETNET</strong></Link><p>See connections. Question claims.</p><div><Link href="/dashboard/">Workspace</Link><Link href="/legal/">Legal &amp; erasure</Link><a href="/sitemap.xml">Sitemap</a><a href="/" lang="de" hrefLang="de">Deutsch</a></div></footer>
  </main>;
}
