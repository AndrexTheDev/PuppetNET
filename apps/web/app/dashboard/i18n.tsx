'use client';

import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { usePathname } from 'next/navigation';

import { translate, type Language, type StringKey } from '../../lib/i18n-strings';

/**
 * UI language context (Wave 6).
 *
 * Priority: the /en/ route is always English (it is the explicit English
 * publication and must stay stable for crawlers and hreflang); everywhere
 * else the stored preference wins, defaulting to German. The toggle persists
 * to localStorage. Without a provider (static rendering, component tests) the
 * context falls back to German, so pinned markup stays byte-identical.
 *
 * The <html lang> attribute is NOT flipped by the toggle (the published
 * document language is German); the dashboard subtree carries lang="en" when
 * the toggle is active.
 */

const STORAGE_KEY = 'puppetnet-lang';

interface LanguageContextValue {
  lang: Language;
  setLang: (next: Language) => void;
}

const LanguageContext = createContext<LanguageContextValue>({
  lang: 'de',
  setLang: () => { /* no provider: language is fixed to the document language */ },
});

function isStoredLanguage(value: string | null): value is Language {
  return value === 'de' || value === 'en';
}

/**
 * Fixed-language provider for tests and previews: no router, no storage — the
 * language is exactly what the caller passes. (LanguageProvider depends on
 * usePathname, which only exists inside the Next.js app router.)
 */
export function FixedLanguageProvider({ lang, children }: { lang: Language; children: React.ReactNode }) {
  const value = useMemo<LanguageContextValue>(() => ({ lang, setLang: () => { /* fixed */ } }), [lang]);
  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
}

export function LanguageProvider({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const onEnRoute = pathname === '/en' || pathname.startsWith('/en/');
  const [stored, setStored] = useState<Language | null>(null);

  // Reading storage in an effect keeps server and first client render equal
  // (no hydration mismatch); the German default matches the document language.
  useEffect(() => {
    try {
      const value = window.localStorage.getItem(STORAGE_KEY);
      if (isStoredLanguage(value)) setStored(value);
    } catch { /* storage may be blocked; the default stays */ }
  }, []);

  const setLang = useCallback((next: Language) => {
    setStored(next);
    try { window.localStorage.setItem(STORAGE_KEY, next); } catch { /* non-persistent switch */ }
  }, []);

  const value = useMemo<LanguageContextValue>(
    () => ({ lang: onEnRoute ? 'en' : (stored ?? 'de'), setLang }),
    [onEnRoute, stored, setLang],
  );
  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
}

export function useLanguage(): LanguageContextValue {
  return useContext(LanguageContext);
}

/** Translator bound to the active language. Accepts computed keys (e.g. `type.${node.type}`). */
export function useT(): (key: StringKey | string, params?: Record<string, string | number>) => string {
  const { lang } = useLanguage();
  return useCallback((key: StringKey | string, params?: Record<string, string | number>) => translate(lang, key, params), [lang]);
}
