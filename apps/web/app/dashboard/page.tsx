import Dashboard from './Dashboard';
import './dashboard.css';
export const metadata = {
  title: 'Netzwerkanalyse',
  description: 'WebGL-gestützte OSINT-Netzwerkanalyse mit überprüfbaren Quellen, geographischem Kontext und barrierearmer Tabellenansicht.',
  alternates: { canonical: '/dashboard/' },
  openGraph: {
    url: '/dashboard/',
    title: 'Netzwerkanalyse — PuppetNET',
    description: 'Entitäten, Beziehungen und Quellen im interaktiven Graphen prüfen — mit vollständiger Tastaturbedienung und Tabellenalternative.',
    images: [{ url: '/og/site.png', width: 1200, height: 630, alt: 'PuppetNET Netzwerkanalyse' }],
  },
};
export default function DashboardPage() { return <Dashboard />; }
