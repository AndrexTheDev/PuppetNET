'use client';

import { useEffect, useState } from 'react';

/** Inventory only: no external requests, cookies or ad-network code. */
export default function AdContainers({ children }: { children: React.ReactNode }) {
  const [mobileDismissed, setMobileDismissed] = useState(false);
  useEffect(() => {
    try { setMobileDismissed(localStorage.getItem('puppetnet:mobile-ad-dismissed:v1') === '1'); }
    catch { /* Storage can be blocked; closing still works for this visit. */ }
  }, []);
  function dismissMobile() {
    setMobileDismissed(true);
    try { localStorage.setItem('puppetnet:mobile-ad-dismissed:v1', '1'); }
    catch { /* No persistence available. */ }
  }
  return <>
    <div className="desktop-ad-container" aria-label="Desktop-Werbeflächen">
      <div className="ad-slot ad-header" data-ad-slot="desktop-header"><span>Anzeige · Header</span><small>728 × 90 / 970 × 90</small></div>
      <div className="ad-slot ad-skyscraper ad-left" data-ad-slot="desktop-left"><span>Anzeige</span><small>160 × 600</small></div>
      <div className="ad-slot ad-skyscraper ad-right" data-ad-slot="desktop-right"><span>Anzeige</span><small>160 × 600</small></div>
    </div>
    <div className="site-content">{children}</div>
    <div className="desktop-ad-container ad-footer-wrap" aria-label="Desktop-Footer-Werbefläche"><div className="ad-slot ad-footer" data-ad-slot="desktop-footer"><span>Anzeige · Footer</span><small>728 × 90 / 970 × 90</small></div></div>
    {!mobileDismissed && <aside className="mobile-ad-container" aria-label="Mobile Werbefläche"><div className="ad-slot" data-ad-slot="mobile-footer"><span>Anzeige · Bannerplatz</span><small>320 × 50</small></div><button type="button" aria-label="Werbeplatz schließen" onClick={dismissMobile}>×</button></aside>}
  </>;
}
