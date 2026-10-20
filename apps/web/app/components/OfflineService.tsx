'use client';
import { useEffect } from 'react';

/**
 * Registers /sw.js once, feature-guarded (Wave 6 PWA). Offline support is an
 * enhancement: registration failures stay silent, because the dashboard already
 * explains every unusable-publication state on its own (standby, never a
 * silent demo swap). The worker itself caches only same-origin GET requests —
 * see apps/web/public/sw.js.
 */
export default function OfflineService() {
  useEffect(() => {
    if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return;
    navigator.serviceWorker.register('/sw.js').catch(() => { /* enhancement, never a blocker */ });
  }, []);
  return null;
}
