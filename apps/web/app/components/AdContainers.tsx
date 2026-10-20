'use client';

import { useEffect, useState } from 'react';
import { useT } from '../dashboard/i18n';

/** Inventory only: no external requests, cookies or ad-network code. */
export default function AdContainers({ children }: { children: React.ReactNode }) {
  const [mobileDismissed, setMobileDismissed] = useState(false);
  const t = useT();
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
    <div className="desktop-ad-container" aria-label={t('ad.desktopAreas')}>
      <div className="ad-slot ad-header" data-ad-slot="desktop-header"><span>{t('ad.header')}</span><small>728 × 90 / 970 × 90</small></div>
      <div className="ad-slot ad-skyscraper ad-left" data-ad-slot="desktop-left"><span>{t('ad.placement')}</span><small>160 × 600</small></div>
      <div className="ad-slot ad-skyscraper ad-right" data-ad-slot="desktop-right"><span>{t('ad.placement')}</span><small>160 × 600</small></div>
    </div>
    <div className="site-content">{children}</div>
    <div className="desktop-ad-container ad-footer-wrap" aria-label={t('ad.footerArea')}><div className="ad-slot ad-footer" data-ad-slot="desktop-footer"><span>{t('ad.footer')}</span><small>728 × 90 / 970 × 90</small></div></div>
    {!mobileDismissed && <aside className="mobile-ad-container" aria-label={t('ad.mobileArea')}><div className="ad-slot" data-ad-slot="mobile-footer"><span>{t('ad.banner')}</span><small>320 × 50</small></div><button type="button" aria-label={t('ad.close')} onClick={dismissMobile}>×</button></aside>}
  </>;
}
