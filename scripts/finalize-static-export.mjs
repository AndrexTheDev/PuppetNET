#!/usr/bin/env node
/** Remove Next 15's reserved empty-parameter 404 stub, never a real profile. */
import { existsSync, readFileSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const stub = fileURLToPath(new URL('../apps/web/out/entity/__empty-publication/', import.meta.url));
const html = `${stub}/index.html`;
if (existsSync(html)) {
  const contents = readFileSync(html, 'utf8');
  if (!/<meta name="robots" content="noindex/.test(contents) || contents.includes('application/ld+json')) {
    throw new Error('Refusing to remove a non-404 entity export');
  }
  rmSync(stub, { recursive: true });
  console.log('finalize-static-export: removed reserved empty-publication 404 stub');
}
