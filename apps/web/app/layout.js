import './styles.css';

const origin = process.env.NEXT_PUBLIC_SITE_ORIGIN || process.env.SITE_ORIGIN || 'https://puppetnet-console.pages.dev';
const metadataBase = (() => {
  try {
    const url = new URL(origin);
    return ['https:', 'http:'].includes(url.protocol) ? url : new URL('https://puppetnet-console.pages.dev');
  } catch {
    return new URL('https://puppetnet-console.pages.dev');
  }
})();

export const metadata = {
  metadataBase,
  title: { default: 'PuppetNET 2.0 — OSINT-Netzwerkanalyse', template: '%s · PuppetNET' },
  description:
    'Open-Source-Konsole für OSINT-Netzwerkanalyse: Entitäten, Beziehungen und Quellen nachvollziehbar prüfen.',
  applicationName: 'PuppetNET',
  authors: [{ name: 'PuppetNET' }],
  category: 'technology',
  robots: { index: true, follow: true, 'max-image-preview': 'large', 'max-snippet': -1 },
  openGraph: {
    type: 'website',
    locale: 'de_DE',
    siteName: 'PuppetNET',
    title: 'PuppetNET 2.0 — OSINT-Netzwerkanalyse',
    description:
      'Entitäten, Beziehungen und Quellen nachvollziehbar prüfen. Berechnete Verbindungen sind keine Tatsachenbehauptungen.',
    images: [{ url: '/og/site.png', width: 1200, height: 630, alt: 'PuppetNET — OSINT-Netzwerkanalyse' }],
  },
  twitter: {
    card: 'summary_large_image',
    title: 'PuppetNET 2.0 — OSINT-Netzwerkanalyse',
    description: 'Entitäten, Beziehungen und Quellen nachvollziehbar prüfen.',
    images: ['/og/site.png'],
  },
};

export const viewport = { width: 'device-width', initialScale: 1, colorScheme: 'dark', themeColor: '#0b111c' };

export default function Layout({ children }) {
  return (
    <html lang="de">
      <body>
        <a className="skip-link" href="#main-content">Zum Inhalt springen</a>
        {children}
      </body>
    </html>
  );
}
