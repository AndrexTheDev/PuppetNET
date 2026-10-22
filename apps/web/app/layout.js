import '../src/styles/globals.css';
import './styles.css';
import AdContainers from './components/AdContainers';
import Intro from './components/Intro';
import OfflineService from './components/OfflineService';
import SkipLink from './components/SkipLink';
import { LanguageProvider } from './dashboard/i18n';

const origin = process.env.SITE_ORIGIN || process.env.NEXT_PUBLIC_SITE_ORIGIN || 'https://puppetnet-web.pages.dev';
const metadataBase = (() => {
  try {
    const url = new URL(origin);
    return ['https:', 'http:'].includes(url.protocol) ? url : new URL('https://puppetnet-web.pages.dev');
  } catch {
    return new URL('https://puppetnet-web.pages.dev');
  }
})();

export const metadata = {
  metadataBase,
  icons: { icon: "/media/logo-icon.webp", apple: "/media/logo.png" },
  manifest: '/manifest.webmanifest',
  title: { default: 'PuppetNET 2.0 — OSINT-Netzwerkanalyse', template: '%s · PuppetNET' },
  description:
    'Sieh das Netzwerk hinter der Geschichte. Entdecke Verbindungen, erkunde Quellen und entwickle neue Rechercheansätze mit PuppetNET.',
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

export const viewport = { width: 'device-width', initialScale: 1, colorScheme: 'dark', themeColor: '#0b100d' };

export default function Layout({ children }) {
  return (
    <html lang="de">
      <body>
        <OfflineService />
        <LanguageProvider>
          <SkipLink />
          <AdContainers>{children}</AdContainers>
          <Intro />
        </LanguageProvider>
      </body>
    </html>
  );
}
