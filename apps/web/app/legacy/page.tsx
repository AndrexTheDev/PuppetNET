import type { Metadata } from 'next';
import Link from 'next/link';

/**
 * Static redirect for the archived v1 analyst console (formerly
 * apps/web/public/legacy). `next.config.mjs` `redirects()` are NOT applied with
 * `output: 'export'` — Next only warns and ignores them — and `redirect()`
 * from next/navigation is baked into the RSC payload only, which needs
 * JavaScript to navigate. This route therefore renders the redirect itself:
 * a zero-delay permanent meta refresh (React 19 hoists <meta> into <head> at
 * build time) plus a visible fallback link, so /legacy forwards on any static
 * host, without scripts, for crawlers and humans alike.
 * Cloudflare Pages additionally serves a real HTTP 301 from public/_redirects.
 */
export const dynamic = 'force-static';

export const metadata: Metadata = {
  title: 'Konsole archiviert — Weiterleitung',
  description: 'Die bisherige Analystenkonsole wurde archiviert. Weiterleitung zur Netzwerkanalyse.',
  robots: { index: false, follow: false },
  alternates: { canonical: '/dashboard/' },
};

export default function LegacyConsoleRedirect() {
  return (
    <main id="main-content" tabIndex={-1} className="legacy-redirect">
      <meta httpEquiv="refresh" content="0; url=/dashboard/" />
      <section className="legacy-redirect-card">
        <h1>Die bisherige Analystenkonsole wurde archiviert.</h1>
        <p>Du wirst zur Netzwerkanalyse weitergeleitet …</p>
        <p><Link href="/dashboard/">Weiter zur Netzwerkanalyse</Link>, falls die automatische Weiterleitung nicht funktioniert.</p>
      </section>
    </main>
  );
}
