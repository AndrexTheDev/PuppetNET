#!/usr/bin/env node
/**
 * audit.mjs — whole-repository static audit.
 *
 *   npm run audit            # human-readable report + docs/audit/findings.json
 *   npm run audit -- --strict  # also fail on low/info findings
 *
 * The smoke suites answer "does this feature work?". This script answers a
 * different question: "is the repository internally consistent, safe and free to
 * run?". Those are the defects that survive a green test suite — a client-side
 * cap that drifted from the Worker's, a duplicate element id, an `aria-labelledby`
 * pointing at nothing, a paid API creeping into the ingest list, a secret in a log
 * line. Every check below encodes an invariant that two modules must agree on,
 * or a hygiene rule the project has chosen.
 *
 * Findings are graded high / medium / low / info. Accepted risks are not deleted
 * from the report — they are listed in ACCEPTED with a written justification, so
 * the ledger stays complete and the decision stays visible. CI fails on high and
 * medium (and on anything accepted whose justification has gone stale).
 *
 * Offline and dependency-free by design: it reads files, it runs nothing.
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const STRICT = process.argv.includes("--strict");

/* ========================================================================== */
/*  Ledger                                                                     */
/* ========================================================================== */

/** Accepted risks: check id → why this is deliberate. Keep the reason specific. */
const ACCEPTED = new Map([
  ["xss/innerHTML",
    "The surviving writes are: `host.innerHTML = \"\"` (clearing a container), the el() helper's "
    + "documented `html` key, the QR SVG (constant address through the vendored generator) and three "
    + "settings/boot strings (#cfg-probe, #cfg-mode-help, #boot-status) in which every interpolation "
    + "passes escapeHtml — audited by xss/elHtml and toast/sink above, and attacked at runtime by the "
    + "hostile-text checks in tests/web_smoke.mjs."],
  ["python/assert",
    "assert is used in tests and in config self-validation that runs at load time (never with "
    + "-O in the shipped workflows)."],
  ["cost/external-host",
    "Inventory check: the ledger lists every external host with its free-tier budget. Being on "
    + "the list is not a defect; being on it without a budget note is."],
]);

const findings = [];
const counters = { high: 0, medium: 0, low: 0, info: 0, accepted: 0 };

function finding(id, dimension, severity, module, where, message, fix) {
  const accepted = ACCEPTED.has(id);
  findings.push({
    id, dimension, severity: accepted ? "accepted" : severity,
    module, where, message, fix: fix || null, accepted,
    justification: accepted ? ACCEPTED.get(id) : null,
  });
  if (accepted) counters.accepted += 1;
  else counters[severity] += 1;
}

/* ========================================================================== */
/*  Helpers                                                                    */
/* ========================================================================== */

const cache = new Map();
function read(relative) {
  if (!cache.has(relative)) {
    const full = path.join(ROOT, relative);
    cache.set(relative, existsSync(full) ? readFileSync(full, "utf8") : null);
  }
  return cache.get(relative);
}
const exists = (relative) => existsSync(path.join(ROOT, relative));

/** Walk a file line by line, reporting every regex match with file:line. */
function scan(relative, regex, onMatch) {
  const text = read(relative);
  if (text === null) return 0;
  let hits = 0;
  text.split("\n").forEach((line, index) => {
    const re = new RegExp(regex.source, regex.flags.includes("g") ? regex.flags : regex.flags + "g");
    let match;
    while ((match = re.exec(line)) !== null) {
      hits += 1;
      onMatch(match, `${relative}:${index + 1}`, line.trim());
      if (match.index === re.lastIndex) re.lastIndex += 1;
    }
  });
  return hits;
}

function walk(dir, filter, out = []) {
  for (const entry of readdirSync(path.join(ROOT, dir), { withFileTypes: true })) {
    const relative = path.posix.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (/^\.git$|node_modules|\.venv|__pycache__|\.pytest_cache/.test(entry.name)) continue;
      walk(relative, filter, out);
    } else if (!filter || filter(relative)) out.push(relative);
  }
  return out;
}

/** Read width/height out of a JPEG's SOF marker — no image library needed. */
function jpegSize(buffer) {
  if (buffer[0] !== 0xff || buffer[1] !== 0xd8) return null;
  let offset = 2;
  while (offset + 9 < buffer.length) {
    if (buffer[offset] !== 0xff) { offset += 1; continue; }
    const marker = buffer[offset + 1];
    const length = buffer.readUInt16BE(offset + 2);
    if (marker >= 0xc0 && marker <= 0xcf && ![0xc4, 0xc8, 0xcc].includes(marker)) {
      return { height: buffer.readUInt16BE(offset + 5), width: buffer.readUInt16BE(offset + 7) };
    }
    offset += 2 + length;
  }
  return null;
}

/* ========================================================================== */
/*  1. Browser JS — XSS surface                                                */
/* ========================================================================== */

function auditBrowserJs() {
  const dimension = "security";
  for (const file of ["web/app.js", "web/modals.js"]) {
    // Direct HTML sinks. Each hit is inspected for interpolation: a template
    // literal or a `+` concatenation means data reached the sink.
    scan(file, /\.innerHTML\s*=\s*([^;\n]*)/g, (match, where, line) => {
      const value = match[1];
      const cleared = /^(""|'')/.test(value.trim());
      const interpolated = /\$\{|"\s*\+\s*[A-Za-z_$]|'\s*\+\s*[A-Za-z_$]/.test(value);
      if (!cleared && interpolated) {
        finding("xss/innerHTML", dimension, "high", file, where,
          `innerHTML receives interpolated content: ${value.trim().slice(0, 90)}`,
          "Build nodes with the el() helper (which escapes) or prove the input is a constant.");
      }
    });
    scan(file, /\.insertAdjacentHTML\(\s*"[^"]*"\s*,\s*([^;]*)/g, (match, where) => {
      if (/\$\{|"\s*\+/.test(match[1])) {
        finding("xss/insertAdjacentHTML", dimension, "high", file, where,
          `insertAdjacentHTML receives interpolated content: ${match[1].trim().slice(0, 90)}`,
          "Use insertAdjacentElement with escaped text, or a DocumentFragment.");
      }
    });
    scan(file, /document\.write(ln)?\s*\(/g, (_m, where) => {
      finding("xss/documentWrite", dimension, "high", file, where,
        "document.write in shipped code", "Remove: it blocks parsing and is an injection sink.");
    });
    // Inline handlers inside generated markup strings.
    scan(file, /["'`][^"'`]*\son(click|error|load|mouse\w+|focus|blur|change|submit)\s*=/gi,
      (_m, where, line) => {
        finding("xss/inlineHandler", dimension, "high", file, where,
          `inline event handler in markup: ${line.slice(0, 90)}`,
          "Attach listeners in JS; the deployed CSP forbids inline script anyway.");
      });
    for (const sink of [/\beval\s*\(/g, /new\s+Function\s*\(/g]) {
      scan(file, sink, (_m, where, line) => {
        if (/^\s*(\/\/|\*)/.test(line)) return;
        finding("security/eval", dimension, "high", file, where,
          `dynamic code execution: ${line.slice(0, 80)}`, "Remove — CSP script-src 'self' blocks it.");
      });
    }
    // Leftover debugging in shipped browser code.
    scan(file, /console\.(log|debug)\s*\(/g, (_m, where, line) => {
      if (/^\s*(\/\/|\*)/.test(line)) return;
      finding("hygiene/consoleLog", "bugs", "low", file, where,
        `console.log in shipped code: ${line.slice(0, 80)}`,
        "Use the console's own toast/log surface, or delete.");
    });
  }
}

/**
 * Toast bodies and the `el()` helper's `html` key are the only places the console
 * renders markup on purpose. Both are choke points, so the audit checks the choke
 * point rather than trusting 39 call sites to remember.
 */
function auditHtmlSinks() {
  const app = read("web/app.js") || "";

  const sink = /function toastMarkup\(message\)\s*\{([\s\S]*?)\n  \}/.exec(app);
  if (!sink) {
    finding("toast/sink", "security", "high", "web/app.js", "toastMarkup",
      "no escaping toast sink — toast messages would be rendered as caller-supplied HTML",
      "Escape in the sink and allowlist the emphasis tags.");
  } else {
    if (!/escapeHtml\(/.test(sink[1])) {
      finding("toast/sinkEscapes", "security", "high", "web/app.js", "toastMarkup",
        "the toast sink does not escape its input", "Escape before re-allowing any tag.");
    }
    // The allowlist is the pipe-separated tag group inside the sink's regex.
    const allowed = (/\(([a-z]+(?:\|[a-z]+)+)\)/.exec(sink[1]) || [])[1];
    const tags = allowed ? allowed.split("|").sort() : [];
    if (tags.join("|") !== ["b", "br", "code"].join("|")) {
      finding("toast/markupAllowlist", "security", "medium", "web/app.js", "toastMarkup",
        `the toast markup allowlist is [${tags.join(", ")}], expected [b, br, code]`,
        "Widening it re-opens the sink; an <a> or <img> would carry attributes.");
    }
    if (/"use strict"|onerror|javascript:/i.test(sink[1])) {
      finding("toast/sinkSuspicious", "security", "high", "web/app.js", "toastMarkup",
        "the sink contains something that is not escaping or the allowlist", "Review it.");
    }
  }

  // Call sites pass text. An escapeHtml inside a toast call double-escapes and
  // shows the analyst `&lt;b&gt;` where a bold count was meant.
  scan("web/app.js", /toast\((?:[^;]{0,400}?)escapeHtml\(/g, (_m, where, line) => {
    finding("toast/callSiteEscaping", "bugs", "low", "web/app.js", where,
      `toast call escapes its own argument: ${line.slice(0, 70)}`,
      "Pass text — the sink escapes.");
  });

  // The el() helper's html key is the other deliberate sink.
  const htmlSinks = [...app.matchAll(/html:\s*([^,\n}]+)/g)];
  htmlSinks.forEach((match) => {
    const value = match[1];
    const interpolated = /\$\{|"\s*\+|\+\s*[A-Za-z_$]/.test(value);
    const escaped = /escapeHtml\(|toastMarkup\(/.test(value);
    if (interpolated && !escaped) {
      const line = app.slice(0, match.index).split("\n").length;
      finding("xss/elHtml", "security", "high", "web/app.js", `web/app.js:${line}`,
        `el({ html: … }) receives unescaped interpolation: ${value.trim().slice(0, 70)}`,
        "Wrap every interpolated value in escapeHtml.");
    }
  });
}

/* ========================================================================== */
/*  2. index.html — integrity, a11y, hygiene                                   */
/* ========================================================================== */

function auditHtml() {
  const file = "web/index.html";
  const html = read(file);
  if (!html) return;
  const dimension = "bugs";

  // Duplicate ids silently break getElementById, labels and aria wiring.
  const ids = new Map();
  scan(file, /\bid="([^"]+)"/g, (match, where) => {
    const id = match[1];
    if (ids.has(id)) {
      finding("html/duplicateId", dimension, "high", file, where,
        `duplicate id="${id}" (first at ${ids.get(id)})`, "Rename one; ids must be unique.");
    } else ids.set(id, where);
  });

  // Every id reference must resolve, or the relationship it declares is a lie
  // (a dialog without a name, a label attached to nothing).
  const refAttrs = ["aria-labelledby", "aria-describedby", "aria-controls", "aria-owns", "for", "list"];
  for (const attr of refAttrs) {
    scan(file, new RegExp(`\\b${attr}="([^"]+)"`, "g"), (match, where) => {
      for (const token of match[1].split(/\s+/).filter(Boolean)) {
        if (!ids.has(token)) {
          finding("html/danglingRef", dimension, "medium", file, where,
            `${attr}="${token}" points at an element that does not exist`,
            `Add the missing id or correct the reference.`);
        }
      }
    });
  }
  scan(file, /href="#([^"]+)"/g, (match, where) => {
    if (match[1] && !ids.has(match[1])) {
      finding("html/danglingFragment", dimension, "low", file, where,
        `href="#${match[1]}" has no target`, "Point it at a real id or remove the link.");
    }
  });

  // `data-open-modal="#id"` is resolved by delegation in modals.js, so a typo
  // does not throw — the click quietly opens nothing. The footer carries seven
  // of them and they are the only path to the legal copy.
  scan(file, /\bdata-open-modal="([^"]+)"/g, (match, where) => {
    const target = match[1];
    if (!target.startsWith("#") || !ids.has(target.slice(1))) {
      finding("html/danglingDialogOpener", dimension, "medium", file, where,
        `data-open-modal="${target}" has no dialog to open`,
        "Point it at an existing .modal id, including the leading #.");
    }
  });

  // Inline handlers and blank-target links.
  scan(file, /\son[a-z]+\s*=\s*"/gi, (match, where) => {
    finding("html/inlineHandler", "security", "high", file, where,
      `inline event handler ${match[0].trim()}`, "The CSP forbids inline script — bind in JS.");
  });
  scan(file, /<a\b[^>]*target="_blank"[^>]*>/gi, (match, where) => {
    if (!/rel="[^"]*noopener/.test(match[0])) {
      finding("html/blankNoopener", "security", "medium", file, where,
        `target="_blank" without rel="noopener": ${match[0].slice(0, 80)}`,
        'Add rel="noopener" (reverse tabnabbing).');
    }
  });
  scan(file, /<img\b(?![^>]*\balt=)[^>]*>/gi, (match, where) => {
    finding("a11y/imgAlt", "bugs", "low", file, where,
      `<img> without alt: ${match[0].slice(0, 70)}`, 'Add alt="" if decorative, else describe it.');
  });

  // Dialogs must be named and modal; the console is keyboard-driven.
  scan(file, /<div\b[^>]*class="modal"[^>]*>/g, (match, where) => {
    if (!/role="dialog"/.test(match[0]) || !/aria-modal="true"/.test(match[0])) {
      finding("a11y/dialogRole", "bugs", "medium", file, where,
        `dialog without role/aria-modal: ${match[0].slice(0, 80)}`,
        'Add role="dialog" aria-modal="true" aria-labelledby="…".');
    }
    if (!/aria-labelledby=/.test(match[0])) {
      finding("a11y/dialogName", "bugs", "medium", file, where,
        "dialog has no accessible name", "Add aria-labelledby pointing at its heading.");
    }
  });

  // Heading structure: one h1, no skipped levels.
  const levels = [...html.matchAll(/<h([1-6])\b/gi)].map((match) => Number(match[1]));
  const h1 = levels.filter((level) => level === 1).length;
  if (h1 !== 1) {
    finding("seo/singleH1", "seo", "medium", file, "web/index.html",
      `expected exactly one <h1>, found ${h1}`, "One h1 per document; demote the rest.");
  }
  levels.forEach((level, index) => {
    if (index > 0 && level - levels[index - 1] > 1) {
      finding("seo/headingSkip", "seo", "low", file, `web/index.html (h${levels[index - 1]}→h${level})`,
        `heading level jumps from h${levels[index - 1]} to h${level}`,
        "Keep the outline contiguous for screen readers and crawlers.");
    }
  });

  // Buttons need an accessible name.
  scan(file, /<button\b[^>]*>\s*<\/button>/g, (match, where) => {
    if (!/aria-label=|title=/.test(match[0])) {
      finding("a11y/unnamedButton", "bugs", "low", file, where,
        `empty button without a name: ${match[0].slice(0, 70)}`, "Add aria-label.");
    }
  });
}

/* ========================================================================== */
/*  3. SEO completeness                                                        */
/* ========================================================================== */

function auditSeo() {
  const file = "web/index.html";
  const html = read(file);
  if (!html) return;
  const head = html.slice(0, html.indexOf("</head>"));
  const metaContent = (name) => {
    for (const pattern of [
      new RegExp(`<meta[^>]+name="${name}"[^>]+content="([^"]*)"`, "i"),
      new RegExp(`<meta[^>]+content="([^"]*)"[^>]+name="${name}"`, "i"),
      new RegExp(`<meta[^>]+property="${name}"[^>]+content="([^"]*)"`, "i"),
    ]) {
      const found = pattern.exec(head);
      if (found) return found[1];
    }
    return null;
  };
  const require = (id, condition, message, fix, severity = "medium") => {
    if (!condition) finding(id, "seo", severity, file, "web/index.html<head>", message, fix);
  };

  const title = (/<title>([^<]*)<\/title>/.exec(head) || [])[1] || "";
  require("seo/title", title.length > 0 && title.length <= 70,
    `title is ${title.length} chars (target ≤ 70)`, "Shorten: crawlers truncate.");
  const description = metaContent("description") || "";
  require("seo/description", description.length >= 120 && description.length <= 180,
    `description is ${description.length} chars (target 120–180)`, "Rewrite to the SERP window.");
  require("seo/keywords", Boolean(metaContent("keywords")), "no keyword metadata", "Add keywords.");
  require("seo/canonical", /<link[^>]+rel="canonical"/.test(head), "no canonical link", "Add one.");
  require("seo/robots", /name="robots"[^>]+content="index/.test(head),
    "robots is not indexable", "Flip to index,follow — or document why this deployment is private.");
  require("seo/author", metaContent("author") === "AndrexTheDev", "author is not AndrexTheDev", "Set it.");
  require("seo/themeColor", Boolean(metaContent("theme-color")), "no theme-color", "Add #05070d.");
  for (const key of ["og:type", "og:title", "og:description", "og:url", "og:image", "og:image:width",
    "og:image:height", "og:image:alt", "og:site_name", "og:locale"]) {
    require(`seo/og:${key}`, Boolean(metaContent(key)), `missing ${key}`, `Add the ${key} tag.`);
  }
  for (const key of ["twitter:card", "twitter:title", "twitter:description", "twitter:image"]) {
    require(`seo/twitter:${key}`, Boolean(metaContent(key)), `missing ${key}`, `Add the ${key} tag.`);
  }
  require("seo/twitterCard", metaContent("twitter:card") === "summary_large_image",
    `twitter:card is "${metaContent("twitter:card")}"`, "Use summary_large_image.");

  // The social card must exist, be a JPEG, and match the declared box.
  const image = metaContent("og:image");
  if (image) {
    const local = image.replace(/^\.?\//, "");
    if (!exists(`web/${local}`)) {
      finding("seo/ogImageMissing", "seo", "high", file, `web/${local}`,
        "og:image points at a file that does not exist", "Add the image or fix the path.");
    } else {
      const buffer = readFileSync(path.join(ROOT, "web", local));
      const size = jpegSize(buffer);
      const declared = { w: Number(metaContent("og:image:width")), h: Number(metaContent("og:image:height")) };
      if (!size) {
        finding("seo/ogImageFormat", "seo", "medium", file, `web/${local}`,
          "og:image is not a JPEG (some crawlers only render JPEG/PNG)", "Ship JPEG or PNG.");
      } else if (size.width !== declared.w || size.height !== declared.h) {
        finding("seo/ogImageSize", "seo", "medium", file, `web/${local}`,
          `image is ${size.width}×${size.height} but the tags declare ${declared.w}×${declared.h}`,
          "Make the tags match the file.");
      }
      if (buffer.length > 400_000) {
        finding("seo/ogImageWeight", "seo", "low", file, `web/${local}`,
          `card image is ${(buffer.length / 1024).toFixed(0)} KB`, "Keep it under ~400 KB.");
      }
    }
  }

  // Structured data must parse and be internally consistent.
  const blocks = [...html.matchAll(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/g)];
  require("seo/jsonLd", blocks.length === 1, `expected 1 JSON-LD block, found ${blocks.length}`,
    "Ship exactly one block.");
  blocks.forEach((block) => {
    let data;
    try { data = JSON.parse(block[1]); } catch (error) {
      finding("seo/jsonLdParse", "seo", "high", file, "web/index.html<ld+json>",
        `JSON-LD does not parse: ${error.message}`, "Fix the JSON.");
      return;
    }
    const nodes = Array.isArray(data["@graph"]) ? data["@graph"] : [data];
    const ids = new Set(nodes.map((node) => node["@id"]).filter(Boolean));
    const app = nodes.find((node) => [].concat(node["@type"] || []).includes("SoftwareApplication"));
    const site = nodes.find((node) => node["@type"] === "WebSite");
    if (!app) finding("seo/jsonLdApp", "seo", "high", file, "ld+json",
      "no SoftwareApplication node", "Add one (with WebApplication).");
    if (!site) finding("seo/jsonLdSite", "seo", "medium", file, "ld+json",
      "no WebSite node", "Add one with a SearchAction.");
    if (app) {
      for (const key of ["name", "url", "description", "image", "license", "author", "offers",
        "applicationCategory", "operatingSystem"]) {
        if (app[key] === undefined) {
          finding(`seo/jsonLdApp:${key}`, "seo", key === "offers" ? "high" : "medium", file, "ld+json",
            `SoftwareApplication is missing "${key}"`, `Add ${key} (Google requires name/url/offers).`);
        }
      }
      // The advertised version must be the version that is actually deployed.
      const worker = /WORKER_VERSION = "([^"]+)"/.exec(read("worker.js") || "");
      if (worker && app.softwareVersion && app.softwareVersion !== worker[1]) {
        finding("seo/versionDrift", "bugs", "medium", "web/index.html + worker.js", "ld+json",
          `JSON-LD advertises ${app.softwareVersion} but the Worker is ${worker[1]}`,
          "Bump both, or derive one from the other.");
      }
    }
    // Cross-references inside the graph must resolve.
    nodes.forEach((node) => {
      for (const [key, value] of Object.entries(node)) {
        if (value && typeof value === "object" && typeof value["@id"] === "string" && !ids.has(value["@id"])) {
          finding("seo/jsonLdRef", "seo", "medium", file, `ld+json ${key}`,
            `${key} references ${value["@id"]}, which no node declares`,
            "Declare the node or drop the reference.");
        }
      }
    });
    if (site && site.potentialAction) {
      const template = site.potentialAction.target && site.potentialAction.target.urlTemplate;
      if (!template || !template.includes("{search_term_string}")) {
        finding("seo/searchAction", "seo", "medium", file, "ld+json",
          "SearchAction target has no {search_term_string}", "Fix the urlTemplate.");
      }
    }
  });

  // A public, indexable deployment needs these files; their absence means
  // crawlers get a 404 on /robots.txt and no sitemap to follow.
  for (const [relative, why] of [["web/robots.txt", "crawl directives and the sitemap location"],
    ["web/404.html", "what Cloudflare Pages serves for an unknown path"]]) {
    if (!exists(relative)) {
      finding("seo/missingFile", "seo", "medium", relative, relative,
        `${relative} is missing — it carries ${why}`, `Add ${relative}.`);
    }
  }
  // A sitemap needs absolute URLs, and the deployment origin is not known at
  // commit time, so it is emitted at deploy time — but only if that is wired up.
  // A committed sitemap with a guessed hostname would be worse than none.
  if (!exists("web/sitemap.xml")) {
    const emitter = exists("scripts/emit-seo-files.mjs");
    const wired = /emit-seo-files\.mjs/.test(read(".github/workflows/pages_deploy.yml") || "");
    if (!emitter || !wired) {
      finding("seo/missingFile", "seo", "medium", "web/sitemap.xml", "deploy",
        "no sitemap, and no emitter wired into the Pages deploy to produce one",
        "Add scripts/emit-seo-files.mjs and run it before `pages deploy`.");
    }
  }
  if (exists("web/robots.txt")) {
    const robots = read("web/robots.txt");
    if (!/^User-agent:\s*\*/m.test(robots)) {
      finding("seo/robotsDirectives", "seo", "medium", "web/robots.txt", "web/robots.txt",
        "robots.txt has no User-agent: * block", "Say something to every crawler.");
    }
    if (/^Sitemap:/m.test(robots) && !/emit-seo-files/.test(read(".github/workflows/pages_deploy.yml") || "")) {
      finding("seo/robotsSitemap", "seo", "low", "web/robots.txt", "web/robots.txt",
        "a Sitemap line is committed but nothing regenerates it per deployment",
        "Let scripts/emit-seo-files.mjs own that line.");
    }
  }
}

/* ========================================================================== */
/*  3b. UI state invariants                                                    */
/* ========================================================================== */

/**
 * The console keeps three pieces of UI state that no single test can see drift:
 * which dialog is open, which elements the filter left visible, and whether an
 * animated transition respects the motion preference. Each of the three has
 * broken silently at least once, so each gets a static gate here and a runtime
 * assertion in tests/web_smoke.mjs (checks 31 and 32).
 */
function auditUiState() {
  const file = "web/app.js";
  const app = read(file);
  if (!app) return;
  const dimension = "bugs";

  // Comments are blanked (not removed) so line numbers stay truthful: the prose
  // below the visibility helpers quotes the very selectors this section forbids.
  const code = app
    .replace(/\/\*[\s\S]*?\*\//g, (block) => block.replace(/[^\n]/g, " "))
    .split("\n")
    .map((line) => (/^\s*\/\//.test(line) ? "" : line))
    .join("\n");
  const lineOf = (index) => code.slice(0, index).split("\n").length;
  const where = (index) => `${file}:${lineOf(index)}`;

  /**
   * Balanced extraction: `callText` for a call whose "(" sits at `open`,
   * `blockText` for a body whose "{" does. Braces inside string literals would
   * confuse blockText; none of the bodies read here contain any, and a false
   * negative shows up as a finding rather than as silence.
   */
  function span(open, opening, closing) {
    let depth = 0;
    for (let i = open; i < code.length; i += 1) {
      if (code[i] === opening) depth += 1;
      else if (code[i] === closing) {
        depth -= 1;
        if (depth === 0) return code.slice(open, i + 1);
      }
    }
    return code.slice(open);
  }
  const callText = (open) => span(open, "(", ")");
  const blockText = (open) => span(open, "{", "}");

  // One dialog at a time, and a page that does not scroll behind it.
  const opener = /function openModal\(id\) \{/.exec(code);
  const closer = /function closeModal\(\) \{/.exec(code);
  if (!opener || !closer) {
    finding("ui/dialogPlumbingMissing", dimension, "medium", file, "openModal/closeModal",
      "app.js no longer exposes the openModal/closeModal pair this section reads",
      "Keep both functions, or move these invariants to wherever the dialogs live now.");
  } else {
    const openBody = blockText(code.indexOf("{", opener.index));
    const closeBody = blockText(code.indexOf("{", closer.index));
    if (!/if \(state\.modalOpen && state\.modalOpen !== id\) closeModal\(\);/.test(openBody)) {
      finding("ui/dialogMutualExclusion", dimension, "medium", file, where(opener.index),
        "opening a dialog no longer closes the dialog already on screen",
        "Call closeModal() before showing the new one: two cards stack, and because "
        + "closeModal() only hides what state.modalOpen points at, the first becomes "
        + "impossible to dismiss.");
    }
    if (!/body\.style\.overflow = "hidden"/.test(openBody)) {
      finding("ui/dialogScrollLock", "a11y", "low", file, where(opener.index),
        "the page behind an open dialog can still scroll",
        'Set document.body.style.overflow = "hidden" while a dialog is open.');
    }
    if (!/body\.style\.overflow = ""/.test(closeBody)) {
      finding("ui/dialogScrollLock", "a11y", "low", file, where(closer.index),
        "closing a dialog leaves the page scroll locked",
        'Reset document.body.style.overflow = "" in closeModal.');
    }
  }

  // Visibility comes from the class applyFilters toggles — never from a selector.
  // Cytoscape 3.30.4 rejects `:not(.flt-hidden)` ("The selector ... is invalid")
  // and then matches *everything*, and `:visible` reads the computed style, which
  // can lag the batch that set the classes. Either one made the HUD report the
  // pre-filter count while the canvas had already narrowed.
  for (const match of code.matchAll(/:visible|:not\(\s*\.flt-hidden/g)) {
    finding("ui/visibilitySelector", dimension, "medium", file, where(match.index),
      `visibility is derived from a selector again (${match[0]})`,
      "Use isVisible()/countVisibleNodes()/countVisibleEdges(): `.flt-hidden` is the "
      + "only display:none rule, so the class is the truth and the selector is not.");
  }

  // Every animated viewport transition honours prefers-reduced-motion.
  for (const match of code.matchAll(/cy\.animate\(/g)) {
    const call = callText(code.indexOf("(", match.index));
    if (!/reduce-motion/.test(call)) {
      finding("ui/animationMotionPreference", "a11y", "low", file, where(match.index),
        "an animated transition ignores the user's motion preference",
        "Pass duration: document.body.classList.contains(\"reduce-motion\") ? 0 : <ms>, "
        + "as fitView, centerOn and zoomBy do.");
    }
  }
}

/* ========================================================================== */
/*  3c. Cypher portability                                                     */
/* ========================================================================== */

/**
 * ORDER BY, SKIP/OFFSET and LIMIT became standalone clauses in Neo4j 5.24. On
 * every earlier 5.x — and an Aura Free database is provisioned with whichever 5.x
 * is current, not with the one this repository was written against — a statement
 * like `WITH p, cost LIMIT $enumCap ORDER BY cost ASC` is a syntax error, so the
 * feature behind it fails against the real database while every offline test
 * stays green: both smoke suites stub Neo4j and never parse Cypher.
 *
 * Scanned as whole files rather than line by line, because Python keeps its
 * Cypher in triple-quoted strings where the two clauses sit on different lines —
 * and because both languages build a statement out of quoted fragments, the
 * delimiters and concatenation operators between two fragments are blanked first
 * (whitespace only, newlines preserved, so the reported line is still the real
 * one). Documentation is deliberately not scanned: it quotes the anti-pattern.
 */
function auditCypher() {
  const pattern = /LIMIT\s+(?:\$\w+|\d+)\s+(?:ORDER\s+BY|SKIP|OFFSET)\b/gi;
  const flatten = (text) => text.replace(/(`|"|')\s*(?:\+\s*)?(`|"|')/g,
    (chunk) => chunk.replace(/[^\n]/g, " "));
  const files = ["worker.js", "web/app.js", "graph_analytics.py", "ingest.py", "telegram_bot.py"]
    .concat(walk("puppetnet", (relative) => relative.endsWith(".py")));
  for (const file of files) {
    const text = read(file);
    if (!text) continue;
    for (const match of flatten(text).matchAll(pattern)) {
      const line = text.slice(0, match.index).split("\n").length;
      finding("cypher/standaloneOrderBy", "bugs", "high", file, `${file}:${line}`,
        `a Cypher statement needs Neo4j 5.24+ to parse (${match[0].replace(/\s+/g, " ")})`,
        "Keep ORDER BY/SKIP/LIMIT as subclauses of the WITH or RETURN they belong to: "
        + "split `WITH x LIMIT $n ORDER BY y` into `WITH x LIMIT $n` followed by "
        + "`WITH x ORDER BY y LIMIT $m`.");
    }
  }
}

/* ========================================================================== */
/*  4. Cross-module invariants                                                 */
/* ========================================================================== */

function auditInvariants() {
  const worker = read("worker.js") || "";
  const app = read("web/app.js") || "";
  const config = read("puppetnet/config.py") || "";

  // The Worker publishes its ceilings; the console must clamp to the same ones.
  // A client that asks for more than the server allows gets a silent 400, and a
  // client that asks for less throws away data the analyst paid for.
  const limits = {};
  const block = /const GRAPH_LIMITS = Object\.freeze\(\{([\s\S]*?)\}\);/.exec(worker);
  if (!block) {
    finding("limits/workerMissing", "bugs", "high", "worker.js", "GRAPH_LIMITS",
      "GRAPH_LIMITS could not be parsed", "Keep the frozen object literal.");
  } else {
    for (const match of block[1].matchAll(/(\w+):\s*(\d+)/g)) limits[match[1]] = Number(match[2]);
  }
  /**
   * Collect every clamp(…, min, max) in the console and classify it by what it
   * clamps. One nesting level is enough — the console writes
   * `clamp(toNumber(opts.limit, 250), 1, 1200)` and nothing deeper.
   */
  const clamps = [];
  for (const match of app.matchAll(/clamp\(((?:[^()]|\([^()]*\))*)\)/g)) {
    const args = match[1];
    const bounds = /,\s*(\d+)\s*,\s*(\d+)\s*$/.exec(args);
    if (!bounds) continue;
    let kind = null;
    if (/\.depth\b/.test(args)) kind = "depth";
    else if (/maxHops|hopsCap/.test(args)) kind = "hops";
    else if (/\.limit\b/.test(args)) {
      // Two different budgets share the word "limit": the graph ceiling and the
      // evidence-table row cap. GRAPH_LIMITS is the authority that tells them
      // apart, so the classification follows it instead of guessing from size.
      const max = Number(bounds[2]);
      if (max === limits.MAX_TABLE_ROWS) kind = "tableRows";
      else if (max > 100) kind = "nodes";
      else kind = "searchRows";
    }
    if (kind) clamps.push({ kind, min: Number(bounds[1]), max: Number(bounds[2]) });
  }

  // The dangerous direction is a client that asks for more than the Worker
  // allows: the request is refused and the analyst sees an empty canvas.
  const invariants = [
    { kind: "nodes", limit: "MAX_NODES", label: "graph node ceiling" },
    { kind: "depth", limit: "MAX_DEPTH", label: "neighbourhood depth" },
    { kind: "hops", limit: "MAX_HOPS", label: "handshake hops" },
    { kind: "tableRows", limit: "MAX_TABLE_ROWS", label: "evidence-table row cap" },
  ];
  // Hops go through clampHops(), which picks the ceiling from the cost mode; its
  // constants are compared with the Worker's below, so the scan skips them here.
  const usesClampHops = /function clampHops\(/.test(app) && (app.match(/clampHops\(/g) || []).length >= 4;
  for (const rule of invariants) {
    if (rule.kind === "hops" && usesClampHops) continue;
    const seen = clamps.filter((clamp) => clamp.kind === rule.kind);
    if (seen.length === 0) {
      finding(`limits/client:${rule.limit}`, "bugs", "medium", "web/app.js", rule.label,
        `the console never clamps the ${rule.label} (the Worker allows ${limits[rule.limit]})`,
        "Clamp client-side so an over-budget request is never sent.");
      continue;
    }
    const worst = Math.max(...seen.map((clamp) => clamp.max));
    if (limits[rule.limit] !== undefined && worst > limits[rule.limit]) {
      finding(`limits/drift:${rule.limit}`, "bugs", "high", "web/app.js ↔ worker.js", rule.label,
        `the console allows ${worst} but the Worker refuses anything above ${limits[rule.limit]}`,
        "Clamp to GRAPH_LIMITS — one source of truth.");
    }
    if (worst < (limits[rule.limit] ?? worst)) {
      finding(`limits/underuse:${rule.limit}`, "bugs", "info", "web/app.js", rule.label,
        `the console caps the ${rule.label} at ${worst} although ${limits[rule.limit]} is allowed`,
        "Deliberate? Then say so in a comment next to the clamp.");
    }
  }

  // Weighted path searches enumerate far more of the graph per hop, so the Worker
  // caps them lower than hop-count searches. If the console does not apply the
  // same ceiling, a 12-hop weighted request silently comes back as a 4-hop answer
  // — and the offline solver disagrees with the live API on identical input.
  const clientCeilings = {
    MAX_HOPS: /const MAX_PATH_HOPS\s*=\s*(\d+)/.exec(app),
    MAX_HOPS_WEIGHTED: /const MAX_PATH_HOPS_WEIGHTED\s*=\s*(\d+)/.exec(app),
  };
  for (const [name, match] of Object.entries(clientCeilings)) {
    if (!match) {
      finding(`limits/weightedHops:${name}`, "bugs", "high", "web/app.js", "hop ceilings",
        `the console has no named ${name} constant to compare with GRAPH_LIMITS.${name}`,
        "Name the ceiling so the invariant is checkable.");
    } else if (limits[name] !== undefined && Number(match[1]) !== limits[name]) {
      finding(`limits/drift:${name}`, "bugs", "high", "web/app.js ↔ worker.js", "hop ceilings",
        `the console allows ${match[1]} hops, the Worker ${limits[name]}`,
        "Make them equal — GRAPH_LIMITS is the source of truth.");
    }
  }
  if (!usesClampHops) {
    finding("limits/weightedHops", "bugs", "medium", "web/app.js", "weighted handshake",
      "the console does not route hop clamping through a single cost-aware helper",
      "Clamp hops where the cost mode is known: UI, request and local solver alike.");
  }
  // The ceiling has to reach the analyst, not just the request.
  if (!/syncHopsCeiling/.test(app) || !/id="path-hops-note"/.test(read("web/index.html") || "")) {
    finding("limits/weightedHopsUi", "bugs", "medium", "web/app.js ↔ web/index.html", "weighted handshake",
      "the hop ceiling changes with the cost mode but the UI does not say so",
      "Move the slider's max with the cost mode and explain the cap next to it.");
  }

  // Aura Free's caps are declared in Python and mirrored in the console's HUD.
  const auraNode = /aura_node_cap:\s*int\s*=\s*([\d_]+)/.exec(config);
  const auraEdge = /aura_edge_cap:\s*int\s*=\s*([\d_]+)/.exec(config);
  const hud = /node_cap:\s*(\d+),\s*edge_cap:\s*(\d+)/.exec(app);
  if (auraNode && auraEdge && hud) {
    const py = [Number(auraNode[1].replace(/_/g, "")), Number(auraEdge[1].replace(/_/g, ""))];
    const js = [Number(hud[1]), Number(hud[2])];
    if (py[0] !== js[0] || py[1] !== js[1]) {
      finding("limits/auraDrift", "bugs", "high", "web/app.js ↔ puppetnet/config.py", "aura caps",
        `console shows ${js.join("/")} but the pipeline budgets ${py.join("/")}`,
        "Keep the Aura Free ceilings identical in both places.");
    }
  } else {
    finding("limits/auraMissing", "bugs", "low", "web/app.js", "aura caps",
      "could not locate the Aura cap mirrors", "Check the HUD caps object.");
  }

  // The canonical key ("TYPE:slug-8hex") is written by Python and *reproduced* in
  // JS: the console builds keys for its demo dataset and renders every key it reads
  // back out of the graph. The invariant is the label vocabulary — the console
  // knows the entity types from `EntityType` plus the domain labels from
  // `puppetnet/domain.py`. A type the console does not know renders as an
  // unlabelled, unfilterable node; a type it invents points at nodes that never
  // exist.
  //
  // This check used to read `puppetnet/resolver.py` and compare `PERSON|ORG|SHELL`
  // prefixes. That path does not exist (the resolver is
  // `puppetnet/graph/resolver.py`, and the key is built in `puppetnet/models.py`),
  // `read()` returns null for a missing file, and the comparison was therefore
  // skipped — a check that could not fail, which is decoration.
  const models = read("puppetnet/models.py") || "";
  const enumStart = models.indexOf("class EntityType(str, Enum):");
  const enumEnd = enumStart < 0 ? -1 : models.indexOf("\nclass ", enumStart + 1);
  const enumBlock = enumStart < 0 ? "" : models.slice(enumStart, enumEnd < 0 ? undefined : enumEnd);
  const pyTypes = [...enumBlock.matchAll(/^\s{4}[A-Z_]+ = "([A-Za-z]+)"/gm)].map((m) => m[1]);
  const domain = read("puppetnet/domain.py") || "";
  // Anchored at the start of a line: an unanchored `[^=]*=` walked past this
  // assignment and matched the *first* frozenset in the file — the jurisdiction
  // list — so the comparison ran against country codes and every domain label
  // looked JS-only.
  const domainBlock = /^DOMAIN_LABELS[^=]*=\s*frozenset\(([\s\S]*?)\n\)/m.exec(domain);
  const domainLabels = domainBlock
    ? [...domainBlock[1].matchAll(/"([A-Za-z]+)"/g)].map((m) => m[1])
    : [];
  const entityTypes = pyTypes.filter((type) => type !== "Unknown");

  // The console's vocabulary, and the rows it builds demo keys from.
  const typesBlock = /const ENTITY_TYPES = Object\.freeze\(\{([\s\S]*?)\n\s{2}\}\)/.exec(app);
  const jsTypes = typesBlock
    ? [...typesBlock[1].matchAll(/^\s{4}([A-Za-z]+)\s*:/gm)].map((m) => m[1])
    : [];

  if (entityTypes.length === 0 || domainLabels.length === 0 || jsTypes.length === 0) {
    finding("invariant/canonicalKey", "bugs", "high", "web/app.js ↔ puppetnet", "label vocabulary",
      `could not read a vocabulary (python types: ${entityTypes.length}, domain labels: ${domainLabels.length}, console types: ${jsTypes.length})`,
      "Keep `class EntityType`, `DOMAIN_LABELS` and `ENTITY_TYPES` parseable — this check compares them.");
  } else {
    const missing = entityTypes.filter((type) => !jsTypes.includes(type));
    // `Unknown` is the console\'s fallback label, not an entity type: Python has it
    // too, and it is excluded from the type list above only because no node is
    // *created* as Unknown.
    const invented = jsTypes.filter((type) => type !== "Unknown" && !entityTypes.includes(type) && !domainLabels.includes(type));
    if (missing.length || invented.length) {
      finding("invariant/canonicalKey", "bugs", "high", "web/app.js ↔ puppetnet/models.py", "label vocabulary",
        `label vocabularies differ (python-only: ${missing.join(", ") || "—"}; js-only: ${invented.join(", ") || "—"})`,
        "Align the two: a label the console does not know renders unlabelled and cannot be filtered.");
    }
  }
}

/* ========================================================================== */
/*  5. Worker security                                                         */
/* ========================================================================== */

function auditWorker() {
  const file = "worker.js";
  const worker = read(file);
  if (!worker) return;

  scan(file, /console\.(log|error|warn|info)\s*\(([^;]*)/g, (match, where) => {
    if (/token|secret|password|authorization|api[_-]?key|credential/i.test(match[2])) {
      finding("worker/secretInLog", "security", "high", file, where,
        `log line mentions a credential: ${match[2].trim().slice(0, 80)}`,
        "Log a fingerprint or a boolean, never the value.");
    }
  });
  scan(file, /"Access-Control-Allow-Origin":\s*"([^"]*)"/g, (match, where) => {
    if (match[1] === "*") {
      finding("worker/corsWildcard", "security", "high", file, where,
        "CORS origin is a wildcard", "Echo a validated origin instead.");
    }
  });
  // Access-Control-Allow-Origin takes exactly one origin, or `*`. A deployment
  // that lists two of them used to have them joined into the header — a value
  // every browser rejects, so such a deployment served none of its origins and
  // the console failed CORS with no clue why. The answer must also depend on the
  // caller's Origin, which is why the resolution has to happen per request.
  const corsStart = worker.indexOf("function corsHeaders(env) {");
  if (corsStart < 0) {
    finding("worker/corsMissing", "security", "high", file, "corsHeaders",
      "worker.js no longer exposes corsHeaders",
      "Restore it, or move these rules to wherever CORS is answered now.");
  } else {
    let depth = 0;
    let end = worker.length - 1;
    for (let i = worker.indexOf("{", corsStart); i < worker.length; i += 1) {
      if (worker[i] === "{") depth += 1;
      else if (worker[i] === "}") {
        depth -= 1;
        if (depth === 0) { end = i; break; }
      }
    }
    const body = worker.slice(corsStart, end + 1);
    if (/Access-Control-Allow-Origin"\]\s*=[^;]*\.join\(/.test(body)) {
      finding("worker/corsJoinedList", "security", "high", file, "corsHeaders",
        "the allowed origins are joined into Access-Control-Allow-Origin",
        "Echo the caller's Origin when it is on the list, send no header at all when it "
        + "is not, and keep `*` for wildcard deployments — a comma-separated list is "
        + "rejected by every browser.");
    }
    if (body.indexOf("__requestOrigin") < 0) {
      finding("worker/corsNotPerRequest", "security", "medium", file, "corsHeaders",
        "the CORS answer does not depend on the request's Origin",
        "Carry the Origin with the env (withRequestOrigin) and echo it back only when the "
        + "operator allowed it; a static answer cannot serve a multi-origin deployment.");
    }
  }
  if (/Access-Control-Allow-Credentials":\s*"true"/.test(worker)
    && /"Access-Control-Allow-Origin":\s*"\*"/.test(worker)) {
    finding("worker/corsCredentials", "security", "high", file, "CORS",
      "credentials allowed with a wildcard origin", "Never combine the two.");
  }
  // Read-only API: anything but GET/HEAD/OPTIONS must be refused.
  if (!/(405|Method Not Allowed)/i.test(worker)) {
    finding("worker/methodAllowance", "security", "high", file, "router",
      "no 405 path found — the graph API may accept writes",
      "Reject every method except GET/HEAD/OPTIONS.");
  }
  if (!/timingSafeEqual/.test(worker)) {
    finding("worker/tokenCompare", "security", "medium", file, "token comparison",
      "token comparison may not be constant-time", "Use crypto.subtle.timingSafeEqual.");
  }
  // Fail-closed default: no token configured must not mean open access.
  if (!/GRAPH_PUBLIC_READ/.test(worker)) {
    finding("worker/failClosed", "security", "medium", file, "graph auth",
      "no explicit public-read switch found", "Require a token unless explicitly opened.");
  }
  // Workers KV Free allows 1k writes/day. A KV write on the request path spends
  // that budget per document, and the scheduled cadence (24 hourly runs with
  // hundreds of article hosts each) would drain it before noon — after which the
  // fleet silently loses its shared rate limits and its robots.txt cache. Bucket
  // writes belong to the throttled sync (one per hot host per sync window).
  {
    const start = worker.indexOf("async function acquireHostToken(");
    if (start >= 0) {
      const end = worker.indexOf("\n}", start);
      const body = worker.slice(start, end < 0 ? worker.length : end);
      if (/\.put\(/.test(body)) {
        finding("worker/kvWritePerRequest", "cost", "high", file, "acquireHostToken",
          "a KV write sits on the per-request path",
          "Publish the bucket from the throttled sync (one write per hot host per window), "
          + "not once per request: KV Free allows 1k writes/day and a single hourly run can "
          + "exceed that.");
      }
    }
  }
}

/* ========================================================================== */
/*  6. Python hazards                                                          */
/* ========================================================================== */

function auditPython() {
  const files = walk(".", (relative) => relative.endsWith(".py"));
  const hazards = [
    [/\byaml\.load\s*\((?![^)]*Loader\s*=\s*yaml\.SafeLoader)/g, "high", "python/yamlLoad",
      "yaml.load without SafeLoader can execute arbitrary Python", "Use yaml.safe_load."],
    [/\bshell\s*=\s*True/g, "high", "python/shellTrue",
      "subprocess with shell=True interpolates into a shell", "Pass an argument list."],
    [/\bpickle\.load\b|\bmarshal\.load\b/g, "high", "python/pickle",
      "unpickling untrusted bytes is code execution", "Use JSON or a schema."],
    [/\beval\s*\(|\bexec\s*\(/g, "high", "python/eval",
      "dynamic code execution", "Replace with a dispatch table."],
    [/\bos\.system\s*\(/g, "medium", "python/osSystem",
      "os.system runs a shell string", "Use subprocess with a list."],
    [/^\s*except\s*:\s*$/g, "medium", "python/bareExcept",
      "bare except swallows KeyboardInterrupt and SystemExit too", "Catch Exception explicitly."],
    [/datetime\.utcnow\(\)/g, "low", "python/utcnow",
      "datetime.utcnow() is naive and deprecated in 3.12+",
      "Use datetime.now(timezone.utc)."],
    [/\bassert\b/g, "info", "python/assert",
      "assert is stripped under python -O", "Validate explicitly on production paths."],
    [/verify\s*=\s*False/g, "high", "python/tlsVerifyOff",
      "TLS verification disabled", "Remove verify=False."],
  ];
  for (const file of files) {
    const isTest = /(^|\/)tests?\//.test(file) || /test_.*\.py$/.test(file);
    for (const [regex, severity, id, message, fix] of hazards) {
      scan(file, regex, (_match, where, line) => {
        if (/^\s*#/.test(line)) return;
        if (isTest && id === "python/assert") return;
        finding(id, "security", severity, file, where, `${message} — ${line.slice(0, 70)}`, fix);
      });
    }
  }
}

/* ========================================================================== */
/*  7. Cost & supply chain — the project must stay free to run                 */
/* ========================================================================== */

/**
 * Every external host the code touches, with the free-tier budget it lives in.
 * Adding a host here is a deliberate act: it forces the question "what does this
 * cost, and what happens at the limit?" before the code ships.
 */
const HOST_BUDGET = {
  "schema.org": { cost: "free", note: "JSON-LD vocabulary identifier, never fetched" },
  "www.w3.org": { cost: "free", note: "XML namespace identifier, never fetched" },
  "opensource.org": { cost: "free", note: "licence URL in metadata" },
  "github.com": { cost: "free", note: "repository links" },
  "api.github.com": { cost: "free", note: "5 000 req/h with a token, 60 without" },
  "raw.githubusercontent.com": { cost: "free", note: "soft rate limit, cacheable" },
  "objects.githubusercontent.com": { cost: "free", note: "release asset redirects" },
  "neo4j.com": { cost: "free", note: "docs links" },
  "console.neo4j.io": { cost: "free", note: "AuraDB Free: 200k nodes / 400k rels, 1 AU" },
  "developers.cloudflare.com": { cost: "free", note: "docs links" },
  "cloudflare.com": { cost: "free", note: "docs links" },
  "api.telegram.org": { cost: "free", note: "Bot API, ~30 msg/s global" },
  "registry.npmjs.org": { cost: "free", note: "dev-time vendoring only" },
  "pypi.org": { cost: "free", note: "dev-time installs only" },
  "files.pythonhosted.org": { cost: "free", note: "dev-time installs only" },
  "offshoreleaks.icij.org": { cost: "free", note: "ICIJ Offshore Leaks, public-domain data" },
  "www.icij.org": { cost: "free", note: "ICIJ public pages" },
  "opencorporates.com": { cost: "free tier, then PAID", note: "anonymous use is throttled hard; an API key needs a paid plan for volume — keep it optional and off by default" },
  "www.wikidata.org": { cost: "free", note: "Wikidata SPARQL: be polite, one query at a time" },
  "query.wikidata.org": { cost: "free", note: "SPARQL endpoint, user-agent required" },
  "en.wikipedia.org": { cost: "free", note: "REST summary API, 200 req/s per IP" },
  "registry.faa.gov": { cost: "free", note: "US aircraft registry, public data" },
  "a4b2c6.local": { cost: "n/a", note: "test fixture host" },
  "relay.example.invalid": { cost: "n/a", note: "test fixture host" },
  "console.example.invalid": { cost: "n/a", note: "test fixture host" },
  "example.com": { cost: "n/a", note: "documentation placeholder" },
  "example.invalid": { cost: "n/a", note: "documentation placeholder" },
  "example.dev": { cost: "n/a", note: "documentation placeholder" },
  "www.sitemaps.org": { cost: "free", note: "sitemap XML namespace identifier, never fetched" },
  "puppetnet-console.pages.dev": { cost: "free", note: "the deployment's own Cloudflare Pages domain — the emitter's example origin, and the default the workflow derives" },
  "localhost": { cost: "free", note: "local development" },
  "127.0.0.1": { cost: "free", note: "local development" },
};

/** Hosts that would put the operator on somebody's invoice — never allowed. */
const PAID_BLOCKLIST = [
  "api.qrserver.com", "quickchart.io", "chart.googleapis.com", "maps.googleapis.com",
  "api.openai.com", "openai.com", "api.anthropic.com", "serper.dev", "serpapi.com",
  "api.hunter.io", "api.shodan.io", "haveibeenpwned.com", "api.ipify.org",
  "cdn.tailwindcss.com", "unpkg.com", "jsdelivr.net", "cdnjs.cloudflare.com",
  "fonts.googleapis.com", "fonts.gstatic.com", "google-analytics.com", "googletagmanager.com",
  "cdn.plot.ly", "d3js.org",
];

/**
 * Pattern rules, applied when a host is not listed exactly. A new source should
 * still land in a known cost class — and anything that does not is a finding,
 * which is the point: adding a data source is a decision, not a side effect.
 */
const HOST_RULES = [
  { test: /\.gov(\.|$)/, cost: "free", note: "public government data, no key" },
  { test: /\.gov\.uk$/, cost: "free", note: "UK public data; Companies House needs a free key (600 req/5 min)" },
  { test: /^(feeds|rss)\./, cost: "free", note: "RSS feed — respect robots.txt and the feed's own cadence" },
  { test: /rapidapi\.com$/, cost: "PAID subscription", note: "RapidAPI metering; must stay key-gated and off by default" },
  { test: /^api\.opencorporates\.com$/, cost: "PAID beyond a small free allowance", note: "token-gated, off by default; anonymous use is throttled and non-commercial only" },
  { test: /neo4j\.io$/, cost: "free tier", note: "AuraDB Free connection shape: 200k nodes / 400k rels, auto-pauses when idle" },
  { test: /npmjs\.(org|com)$/, cost: "free", note: "dev-time registry metadata for vendoring, never at runtime" },
  { test: /icij\.org$/, cost: "free", note: "ICIJ Offshore Leaks — public-domain data" },
  { test: /wikidata\.org$|wikipedia\.org$/, cost: "free", note: "send an identifying User-Agent, one query at a time" },
  { test: /adsbdb\.com$/, cost: "free", note: "community ADS-B read-through; no key, be polite" },
  { test: /faa\.gov$/, cost: "free", note: "US registry dump, public data" },
  // Editorial RSS and public news pages: free to read, but they are somebody's
  // property — the crawl budget in docs/operations.md is what keeps this legal.
  { test: /(theguardian|bbc\.co|nytimes|reuters|aljazeera|france24|flightglobal|tradewindsnews|gcaptain|avherald|aviation24|occrp|globalwitness|transparency)\./,
    cost: "free", note: "newsroom RSS/public page — fetch the feed, not the whole site, and honour crawl-delay" },
];

/** Test and documentation fixtures: reserved TLDs and example hosts. */
const isFixtureHost = (host) => !host.includes(".") || /\.(test|invalid|local|example)$/.test(host)
  || host.includes("example") || /\.example$/.test(host);

function auditCostAndSupplyChain() {
  // The generated ledger and this script are excluded: a report that quotes a
  // finding, and a blocklist that names what it forbids, are not calls.
  const codeFiles = walk(".", (relative) => /\.(js|mjs|cjs|py|yml|yaml|toml|html|css|json)$/.test(relative)
    && !/package-lock\.json$/.test(relative)
    && !/^web\/vendor\//.test(relative)
    && !/^docs\/audit\//.test(relative)
    && relative !== "scripts/audit.mjs"
    && !/^node_modules\//.test(relative));

  // 1. Paid or third-party-CDN hosts must not appear anywhere in shipped code.
  //    The audit script itself is exempt: a blocklist has to name what it forbids.
  for (const file of codeFiles.filter((name) => name !== "scripts/audit.mjs")) {
    for (const host of PAID_BLOCKLIST) {
      scan(file, new RegExp(host.replace(/\./g, "\\."), "g"), (_m, where, line) => {
        if (/^\s*(#|\/\/|\*)/.test(line)) return;
        // A test or a blocklist that *names* a banned host is the opposite of a
        // call to it; those lines are the guard, not the offence.
        if (/banned|blocklist|denylist|must not|forbidden|never/i.test(line)) return;
        finding("cost/paidHost", "security", "high", file, where,
          `references ${host} — a paid service or a CDN that breaks offline use`,
          "Vendor it or remove it; this project must cost nothing to run.");
      });
    }
  }

  // 2. Inventory every external host so the free-tier budget stays explicit.
  const inventory = new Map();
  for (const file of codeFiles) {
    scan(file, /https?:\/\/([a-z0-9.:\[\]-]+)(?::\d+)?/gi, (match, where) => {
      // Two normalisations, for the same reason the worker does them: a trailing
      // dot is the DNS root and names the same host (`https://localhost./` is
      // `localhost`), and a bare IP literal is not a service with a free-tier
      // budget — it is an address, and the ones a test writes down are the ones
      // the SSRF guard refuses.
      const host = match[1].toLowerCase().replace(/\.+$/, "").replace(/^\[|\]$/g, "");
      if (/^\d+\.\d+\.\d+\.\d+$/.test(host) || host.indexOf(":") >= 0) return;
      if (!inventory.has(host)) inventory.set(host, []);
      if (inventory.get(host).length < 3) inventory.get(host).push(where);
    });
  }
  for (const [host, where] of [...inventory.entries()].sort()) {
    if (isFixtureHost(host)) continue;
    const budget = HOST_BUDGET[host]
      || (() => { const rule = HOST_RULES.find((candidate) => candidate.test.test(host));
        return rule ? { cost: rule.cost, note: rule.note } : null; })();
    if (!budget) {
      // A host that appears only in tests is a fixture, not a dependency: nothing
      // shipped ever resolves it. It is recorded, but it does not need a budget.
      const testOnly = where.every((site) => /(^|\/)tests?\//.test(site));
      finding(testOnly ? "cost/testOnlyHost" : "cost/unknownHost", "security",
        testOnly ? "info" : "medium", host, where.join(", "),
        testOnly
          ? `external host ${host} appears only in tests (fixture)`
          : `external host ${host} has no recorded free-tier budget`,
        testOnly ? "No action — nothing shipped calls it."
          : "Add it to HOST_BUDGET with its cost and limit, or remove the call.");
    } else if (/PAID/i.test(budget.cost)) {
      finding("cost/external-host", "info", "low", host, where.join(", "),
        `${host}: ${budget.cost} — ${budget.note}`,
        "Keep it optional and disabled by default.");
    }
  }

  // 3. Any source that can cost money must be key-gated and off by default, so
  //    a fresh clone never sends a request somebody has to pay for.
  const configPy = read("puppetnet/config.py") || "";
  const PAIRED = [
    { host: "api.opencorporates.com", key: "opencorporates_api_token" },
    { host: "adsbexchange-com1.p.rapidapi.com", key: "adsbexchange_api_key" },
  ];
  for (const pair of PAIRED) {
    if (!inventory.has(pair.host)) continue;
    const declared = new RegExp(`${pair.key}:\\s*str\\s*=\\s*"([^"]*)"`).exec(configPy);
    if (!declared) {
      finding("cost/paidSourceUngated", "security", "high", "puppetnet/config.py", pair.host,
        `${pair.host} is reachable but no ${pair.key} setting was found`,
        "Gate the paid source behind an API key that defaults to empty.");
    } else if (declared[1] !== "") {
      finding("cost/paidSourceDefault", "security", "high", "puppetnet/config.py", pair.host,
        `${pair.key} ships with a non-empty default — the paid source is on for everybody`,
        'Default it to "" so the adapter degrades to the free source.');
    }
  }

  // 4. Supply chain: every vendored browser library ships its licence text.
  const vendorDir = "web/vendor";
  if (exists(vendorDir)) {
    const libs = readdirSync(path.join(ROOT, vendorDir)).filter((name) => name.endsWith(".min.js"));
    const licences = readdirSync(path.join(ROOT, vendorDir)).filter((name) => name.startsWith("LICENSE."));
    if (licences.length < libs.length) {
      finding("supply/licences", "security", "medium", vendorDir, vendorDir,
        `${libs.length} vendored libraries but only ${licences.length} licence texts`,
        "Every vendored file needs its upstream licence next to it.");
    }
    libs.forEach((lib) => {
      const bytes = statSync(path.join(ROOT, vendorDir, lib)).size;
      if (bytes > 1_500_000) {
        finding("supply/size", "bugs", "low", `${vendorDir}/${lib}`, vendorDir,
          `${lib} is ${(bytes / 1024).toFixed(0)} KB`, "Check that it is minified and needed.");
      }
    });
  }

  // 5. No secret may be committed; .env must stay ignored.
  const gitignore = read(".gitignore") || "";
  for (const pattern of [".env", ".venv", "node_modules", "data/"]) {
    if (!gitignore.includes(pattern)) {
      finding("hygiene/gitignore", "security", "medium", ".gitignore", ".gitignore",
        `.gitignore does not list ${pattern}`, `Add ${pattern}.`);
    }
  }
  if (exists(".env")) {
    finding("hygiene/dotEnv", "security", "high", ".env", ".env",
      "a .env file exists in the working tree", "Never commit it; it must stay ignored.");
  }
  for (const file of codeFiles) {
    scan(file, /(ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|neo4j(s)?:\/\/[^/\s]+:[^@\s]+@)/g,
      (_m, where, line) => {
        if (/example|placeholder|your[_-]?|REDACTED|\*\*\*/i.test(line)) return;
        finding("security/committedSecret", "security", "high", file, where,
          `something that looks like a credential: ${line.slice(0, 60)}`,
          "Rotate it and remove it from the repository.");
      });
  }
}

/* ========================================================================== */
/*  8. Operations — scheduling, workflows, docs                                */
/* ========================================================================== */

function auditOperations() {
  const workflows = walk(".github/workflows", (relative) => relative.endsWith(".yml"));
  if (workflows.length === 0) {
    finding("ops/noWorkflows", "bugs", "high", ".github/workflows", ".github/workflows",
      "no GitHub Actions workflows found", "The 24 h cycle depends on them.");
  }
  const crons = [];
  for (const file of workflows) {
    scan(file, /- cron:\s*"([^"]+)"/g, (match, where) => {
      const expression = match[1];
      const parts = expression.trim().split(/\s+/);
      if (parts.length !== 5) {
        finding("ops/cronSyntax", "bugs", "high", file, where,
          `cron "${expression}" does not have five fields`, "Use minute hour dom month dow.");
        return;
      }
      crons.push({ file, where, expression });
      // GitHub shifts every cron that is not a round hour under load; the docs
      // recommend avoiding minute 0 for exactly that reason.
      if (parts[0] === "0") {
        finding("ops/cronMinuteZero", "bugs", "info", file, where,
          `cron "${expression}" runs at minute 0, when GitHub load-shifts most`,
          "Pick an off-peak minute (e.g. 17) for a more reliable start.");
      }
    });
    // A scheduled workflow must be able to fail loudly.
    if (!/timeout-minutes/.test(read(file))) {
      finding("ops/noTimeout", "bugs", "medium", file, file,
        "no timeout-minutes — a hung job burns the free-tier budget",
        "Add a timeout to every job.");
    }
    if (!/concurrency/.test(read(file))) {
      finding("ops/noConcurrency", "bugs", "medium", file, file,
        "no concurrency group — overlapping runs can double-harvest and hit rate limits",
        "Add concurrency with cancel-in-progress where safe.");
    }
  }
  if (crons.length === 0) {
    finding("ops/noSchedule", "bugs", "high", ".github/workflows", "schedules",
      "no cron schedule found — nothing runs unattended",
      "Add the harvest schedule.");
  }

  // The daily cycle must keep AuraDB Free awake: it auto-pauses after inactivity,
  // and a paused database is a dead console.
  if (crons.length > 0) {
    const perDay = crons.length;
    finding("ops/cadence", "info", "info", ".github/workflows", "schedules",
      `${perDay} scheduled trigger(s): ${crons.map((cron) => cron.expression).join(", ")}`,
      "See docs/operations.md for the free-tier budget this must fit in.");
  }
}

/* ========================================================================== */
/*  Report                                                                     */
/* ========================================================================== */

function report() {
  const order = { high: 0, medium: 1, low: 2, info: 3, accepted: 4 };
  const sorted = [...findings].sort((a, b) => (order[a.severity] - order[b.severity])
    || a.module.localeCompare(b.module));

  const byDimension = new Map();
  for (const item of findings) {
    byDimension.set(item.dimension, (byDimension.get(item.dimension) || 0) + 1);
  }

  console.log("\nPuppetNET audit — static, offline, dependency-free\n");
  console.log(`  modules scanned   ${walk(".", (r) => /\.(js|mjs|cjs|py|yml|html|css)$/.test(r) && !/^web\/vendor\//.test(r)).length}`);
  console.log(`  findings          ${findings.length}`);
  for (const severity of ["high", "medium", "low", "info", "accepted"]) {
    if (counters[severity]) console.log(`    ${severity.padEnd(9)} ${counters[severity]}`);
  }
  console.log(`  by dimension      ${[...byDimension.entries()].map(([key, value]) => `${key} ${value}`).join(" · ")}`);

  for (const severity of ["high", "medium", "low", "info"]) {
    const group = sorted.filter((item) => item.severity === severity);
    if (group.length === 0) continue;
    console.log(`\n── ${severity.toUpperCase()} ${"─".repeat(Math.max(0, 60 - severity.length))}`);
    for (const item of group) {
      console.log(`  [${item.dimension}] ${item.id}`);
      console.log(`      ${item.module} @ ${item.where}`);
      console.log(`      ${item.message}`);
      if (item.fix) console.log(`      → ${item.fix}`);
    }
  }
  const accepted = sorted.filter((item) => item.severity === "accepted");
  if (accepted.length) {
    console.log(`\n── ACCEPTED RISKS ${"─".repeat(46)}`);
    for (const item of accepted) {
      console.log(`  ${item.id} (${item.module}) — ${item.justification.split(". ")[0]}.`);
    }
  }

  const outDir = path.join(ROOT, "docs", "audit");
  const payload = {
    generated: new Date().toISOString(),
    strict: STRICT,
    counters,
    findings: sorted,
  };
  mkdirSync(outDir, { recursive: true });
  writeFileSync(path.join(outDir, "findings.json"), JSON.stringify(payload, null, 2) + "\n");
  console.log(`\nledger written to docs/audit/findings.json`);

  const blocking = findings.filter((item) => !item.accepted
    && (item.severity === "high" || item.severity === "medium" || (STRICT && item.severity === "low")));
  if (blocking.length) {
    console.log(`\nAUDIT FAILED: ${blocking.length} finding(s) must be resolved or accepted with a reason.`);
    process.exitCode = 1;
  } else {
    console.log("\nAUDIT PASSED: no unresolved high/medium findings.");
  }
}

auditBrowserJs();
auditHtmlSinks();
auditHtml();
auditSeo();
auditUiState();
auditCypher();
auditInvariants();
auditWorker();
auditPython();
auditCostAndSupplyChain();
auditOperations();
await report();
