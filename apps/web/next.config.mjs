import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Config is evaluated by direct next build as well as npm/Cloudflare builds.
if (process.argv.includes('build')) {
  execFileSync(process.execPath, [fileURLToPath(new URL('../../scripts/generate-seo.js', import.meta.url))], { stdio: 'inherit' });
}

/**
 * Static export: no paid SSR/functions, image optimizer or runtime server.
 *
 * Redirects: `redirects()` in this config are silently ignored for
 * `output: 'export'` (Next only prints the "export-no-custom-routes" warning),
 * so the archived `/legacy` console path is redirected by a real route —
 * `app/legacy/page.tsx` exports a permanent meta-refresh page to
 * `/dashboard/`, and `public/_redirects` adds the matching HTTP 301 for
 * Cloudflare Pages. `trailingSlash: true` keeps `/dashboard/` canonical.
 */
const config = {
  output: 'export',
  trailingSlash: true,
  images: { unoptimized: true },
  poweredByHeader: false,
  experimental: { cpus: 2 },
};
export default config;
