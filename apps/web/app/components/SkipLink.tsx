'use client';

import React from 'react';
import { useT } from '../dashboard/i18n';

/**
 * First focusable element of every page. It follows the active UI language:
 * /en/ is always English, elsewhere the stored preference applies (German by
 * default, so every German page keeps the byte-identical text).
 */
export default function SkipLink() {
  const t = useT();
  return <a className="skip-link" href="#main-content">{t('a11y.skipToContent')}</a>;
}
