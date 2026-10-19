#!/usr/bin/env node
/**
 * scripts/vendor-libs.mjs — reproduce web/vendor/ from node_modules.
 *
 *   npm install && npm run vendor:libs
 *
 * The browser libraries the console needs are committed rather than loaded from a
 * CDN (see web/vendor/README.md for why). Committing a copy means the copy has to
 * be reproducible, or it silently drifts from the versions in package.json. This
 * script is the reproduction procedure, and it is deliberately strict:
 *
 *   1. Every expected package must be installed at the version recorded in
 *      package.json's devDependencies — a transitive bump fails the run.
 *   2. `cytoscape` ships a minified dist bundle; it is copied verbatim.
 *   3. The three layout libraries ship unminified UMD bundles; they are minified
 *      with terser (`--compress --mangle`), keeping licence banners.
 *   4. MIT licence texts are copied next to the code they cover.
 *   5. web/vendor/README.md's inventory table is rewritten with the resulting
 *      versions and sizes, so the documentation cannot lag the artefacts.
 *
 * Nothing here runs at deploy time — Cloudflare Pages serves the committed files.
 */

import { createRequire } from "node:module";
import { minify } from "terser";
import { copyFile, readFile, writeFile, mkdir } from "node:fs/promises";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const vendorDir = path.join(root, "web", "vendor");
const modulesDir = path.join(root, "node_modules");

/**
 * Resolve inside node_modules by path rather than through `require.resolve`.
 * Several of these packages ship an `exports` map that deliberately hides
 * `package.json` and `dist/*` from subpath resolution — correct for consumers,
 * but a vendoring script needs the real files, not the public entry point.
 */
function pkgPath(pkg, ...rest) {
  return path.join(modulesDir, pkg, ...rest);
}

/** Packages that must be present, and the file each one contributes. */
const LIBRARIES = [
  { pkg: "layout-base", source: "layout-base.js", target: "layout-base.min.js", minify: true, global: "layoutBase" },
  { pkg: "cose-base", source: "cose-base.js", target: "cose-base.min.js", minify: true, global: "coseBase" },
  { pkg: "cytoscape", source: "dist/cytoscape.min.js", target: "cytoscape.min.js", minify: false, global: "cytoscape" },
  { pkg: "cytoscape-fcose", source: "cytoscape-fcose.js", target: "cytoscape-fcose.min.js", minify: true, global: "cytoscapeFcose" },
  // Donation QR codes. Zero dependencies, ships an unminified UMD bundle that
  // declares `var qrcode` at the top level, so terser's default (non-toplevel)
  // mangling keeps the browser global intact — asserted below.
  { pkg: "qrcode-generator", source: "dist/qrcode.js", target: "qrcode.min.js", minify: true, global: "qrcode" },
];

const LICENSES = ["layout-base", "cose-base", "cytoscape", "cytoscape-fcose", "qrcode-generator"];

function fail(message) {
  console.error("vendor-libs: " + message);
  process.exit(1);
}

function installedVersion(pkg) {
  try {
    return JSON.parse(readFileSync(pkgPath(pkg, "package.json"), "utf8")).version;
  } catch (_) {
    return null;
  }
}

function declaredVersion(pkg) {
  const manifest = require(path.join(root, "package.json"));
  const all = Object.assign({}, manifest.dependencies, manifest.devDependencies);
  const spec = all[pkg];
  if (!spec) fail(`${pkg} is not declared in package.json`);
  return String(spec).replace(/^[\^~]/, "");
}

function formatSize(bytes) {
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
  return Math.round(bytes / 102.4) / 10 + " KB";
}

async function main() {
  if (!existsSync(path.join(root, "node_modules"))) {
    fail("node_modules is missing — run `npm install` first");
  }
  await mkdir(vendorDir, { recursive: true });

  const inventory = [];

  for (const lib of LIBRARIES) {
    const installed = installedVersion(lib.pkg);
    if (!installed) fail(`${lib.pkg} is not installed`);
    const declared = declaredVersion(lib.pkg);
    if (installed !== declared) {
      fail(`${lib.pkg} is installed at ${installed} but package.json declares ${declared}`);
    }

    const sourcePath = pkgPath(lib.pkg, lib.source);
    if (!existsSync(sourcePath)) fail(`${lib.pkg}: expected ${lib.source} at ${sourcePath}`);
    const targetPath = path.join(vendorDir, lib.target);

    if (lib.minify) {
      const code = await readFile(sourcePath, "utf8");
      const result = await minify(code, {
        compress: true,
        mangle: true,
        format: { comments: /^!/ },
      });
      if (result.error) fail(`${lib.pkg}: terser failed — ${result.error.message}`);
      await writeFile(targetPath, result.code, "utf8");
    } else {
      await copyFile(sourcePath, targetPath);
    }

    // A vendored UMD bundle that lost its global would load "successfully" and
    // then break at first use, so assert the attachment is still in the file.
    const written = await readFile(targetPath, "utf8");
    if (!written.includes(lib.global)) {
      fail(`${lib.pkg}: minified output no longer references the global "${lib.global}"`);
    }

    const bytes = Buffer.byteLength(written, "utf8");
    inventory.push({ pkg: lib.pkg, version: installed, target: lib.target, bytes: bytes });
    console.log(`  ✓ ${lib.target.padEnd(26)} ${lib.pkg}@${installed}  ${formatSize(bytes)}`);
  }

  // MIT code does not ship without its licence text. Some packages declare
  // "license": "MIT" in package.json but omit the file from the tarball
  // (qrcode-generator does); for those the text is committed here, taken
  // verbatim from the upstream repository, and this step verifies it is present
  // rather than silently regenerating a paraphrase.
  let copied = 0;
  for (const pkg of LICENSES) {
    const licence = pkgPath(pkg, "LICENSE");
    const target = path.join(vendorDir, "LICENSE." + pkg);
    if (existsSync(licence)) {
      await copyFile(licence, target);
      copied += 1;
      continue;
    }
    if (existsSync(target)) {
      const text = await readFile(target, "utf8");
      if (!/Permission is hereby granted/i.test(text)) {
        fail(`${pkg}: committed LICENSE.${pkg} does not look like a licence text`);
      }
      console.log(`  = ${pkg}: tarball ships no LICENSE — keeping the committed upstream text`);
      copied += 1;
      continue;
    }
    fail(`${pkg}: no LICENSE in the tarball and none committed — cannot ship it`);
  }
  console.log(`  ✓ licences present for ${copied}/${LICENSES.length} packages`);

  // Rewrite the inventory table in web/vendor/README.md so the sizes and
  // versions documented there are generated, not remembered.
  const readmePath = path.join(vendorDir, "README.md");
  if (existsSync(readmePath)) {
    const readme = await readFile(readmePath, "utf8");
    const rows = inventory.map((item) => {
      const line = readme.split("\n").find((l) => l.includes("| `" + item.target + "` |"));
      const url = line ? (/\((https:\/\/www\.npmjs\.com\/package\/[^)]+)\)/.exec(line) || [])[1] : "";
      const label = url ? `[${item.pkg}](${url})` : item.pkg;
      return `| \`${item.target}\` | ${label} | ${item.version} | MIT | ${formatSize(item.bytes)} |`;
    });
    const tailwind = path.join(vendorDir, "tailwind.css");
    if (existsSync(tailwind)) {
      const css = await readFile(tailwind, "utf8");
      rows.push(
        `| \`tailwind.css\` | [tailwindcss](https://www.npmjs.com/package/tailwindcss) | ` +
        `${installedVersion("tailwindcss") || declaredVersion("tailwindcss")} | MIT | ` +
        `${formatSize(Buffer.byteLength(css, "utf8"))} (purged build) |`
      );
    }
    const start = readme.indexOf("| File | Package | Version | Licence | Uncompressed |");
    const separator = readme.indexOf("\n", readme.indexOf("| --- |", start));
    const tableEnd = readme.indexOf("\n\n", separator);
    if (start > 0 && separator > 0 && tableEnd > separator) {
      const head = readme.slice(0, separator + 1);
      const tail = readme.slice(tableEnd);
      await writeFile(readmePath, head + rows.join("\n") + tail, "utf8");
      console.log("  ✓ web/vendor/README.md inventory table refreshed");
    } else {
      console.warn("  ! could not locate the inventory table in web/vendor/README.md — left untouched");
    }
  }

  console.log("\nvendor-libs: done. Commit web/vendor/ together with package.json.");
}

main().catch((err) => fail(err && err.stack ? err.stack : String(err)));
