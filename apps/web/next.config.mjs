import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Config is evaluated by direct next build as well as npm/Cloudflare builds.
if (process.argv.includes('build')) {
  execFileSync(process.execPath, [fileURLToPath(new URL('../../scripts/generate-seo.js', import.meta.url))], { stdio: 'inherit' });
}

/** Static export: no paid SSR/functions, image optimizer or runtime server. */
const config = {
  output: 'export',
  trailingSlash: true,
  images: { unoptimized: true },
  poweredByHeader: false,
  experimental: { cpus: 2 },
};
export default config;
