export default function Home() {
  return <main>
    <header><div><span className="badge">PuppetNET 2.0 · Scaffold</span>
      <h1>Netzwerke verstehen. Quellen prüfen.</h1>
      <p>OSINT-Konsole mit nachvollziehbaren Belegen. Berechnete Verbindungen sind keine Tatsachenbehauptungen.</p></div>
      <a href="/dashboard/">WebGL-Dashboard öffnen →</a></header>
    <p className="notice">Die bestehende Analystenkonsole ist unverändert eingebettet. Ohne konfigurierte API verwendet sie ausdrücklich fiktive Demodaten. Livezugriff: Einstellungen → Cloudflare Worker.</p>
    <iframe title="PuppetNET Analystenkonsole" src="/legacy/index.html" />
  </main>;
}
