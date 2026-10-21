'use client';
import { useEffect, useRef, useState } from 'react';
import { usePathname } from 'next/navigation';
import { useT } from '../dashboard/i18n';

/** Nonessential homepage intro. Never blocks content when JS/video is unavailable. */
export default function Intro() {
  const pathname = usePathname();
  const t = useT();
  const [visible, setVisible] = useState(false);
  const [eligible, setEligible] = useState(false);
  const [fallback, setFallback] = useState(false);
  const skip = useRef<HTMLButtonElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    if (pathname !== '/') { setEligible(false); setVisible(false); return; }
    setEligible(true);
    // Safari uses the precomposited MP4: VP9 support does not guarantee alpha.
    setFallback(/Apple/.test(navigator.vendor));
    if (matchMedia('(prefers-reduced-motion: reduce)').matches || (navigator as Navigator & { connection?: { saveData?: boolean } }).connection?.saveData) return;
    try { if (sessionStorage.getItem('puppetnet-intro-seen')) return; sessionStorage.setItem('puppetnet-intro-seen', '1'); } catch { /* storage may be blocked */ }
    setVisible(true);
  }, [pathname]);
  useEffect(() => {
    if (!visible) return;
    const previous = document.activeElement as HTMLElement | null;
    const nodes = [...document.querySelectorAll<HTMLElement>('.site-content, .desktop-ad-container, .mobile-ad-container')];
    const oldInert = nodes.map(node => node.inert);
    nodes.forEach(node => { node.inert = true; });
    const overflow = document.body.style.overflow; document.body.style.overflow = 'hidden';
    skip.current?.focus();
    const timer = setTimeout(() => setVisible(false), 12_000);
    return () => { clearTimeout(timer); nodes.forEach((node, i) => { node.inert = oldInert[i]; }); document.body.style.overflow = overflow; previous?.focus(); };
  }, [visible]);
  if (!eligible) return null;
  return <>
    <button className="intro-replay" type="button" onClick={() => setVisible(true)}>{t('intro.replay')}</button>
    {visible && <div className="intro-overlay" role="dialog" aria-modal="true" aria-label={t('intro.aria')} onKeyDown={event => {
      if (event.key === 'Escape') setVisible(false);
      if (event.key === 'Tab') { event.preventDefault(); skip.current?.focus(); }
    }}>
      <video key={fallback ? 'mp4' : 'webm'} ref={video} className="intro-video" autoPlay muted playsInline preload="auto" tabIndex={-1} aria-hidden="true" poster="/media/logo.webp"
        onEnded={() => setVisible(false)} onError={() => { if (!fallback) setFallback(true); else setVisible(false); }}
        onCanPlay={() => { video.current?.play().catch(() => setVisible(false)); }}>
        <source src={fallback ? '/media/intro-fallback.mp4' : '/media/intro-alpha.webm'} type={fallback ? 'video/mp4' : 'video/webm'} />
      </video>
      <p className="intro-caption">{t('intro.caption')}</p>
      <button ref={skip} className="intro-skip" type="button" onClick={() => setVisible(false)}>{t('intro.skip')} <span aria-hidden="true">↗</span></button>
    </div>}
  </>;
}
