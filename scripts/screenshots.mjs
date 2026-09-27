#!/usr/bin/env node
/**
 * screenshots.mjs — the visual beta-test matrix.
 *
 *   npm run screenshots                          # every viewport × every state
 *   npm run screenshots -- --only donate         # one state, all viewports
 *   npm run screenshots -- --viewport mobile     # one viewport, all states
 *   npm run screenshots -- --list                # print the matrix, launch nothing
 *   npm run screenshots -- --out docs/shots      # somewhere else than .screenshots/
 *
 *   npx playwright install --with-deps chromium  # once, before the first run
 *
 * What it does: serves `web/` over a local HTTP server and drives the **shipped**
 * artefacts in a real Chromium — index.html, styles.css, app.js, modals.js and the
 * vendored engine. No mocks, no storybook, no build step: the pictures are of the
 * thing that deploys.
 *
 * Every state carries an assertion as well as a screenshot, because a matrix of
 * pretty pictures is not a test. If the handshake panel renders empty, the run
 * fails — the PNG is the evidence, not the verdict.
 *
 * Two honest caveats:
 *   * Fonts. The console's stack asks for Inter and JetBrains Mono and ships
 *     neither (no webfont, no CDN, no licence to carry), so screenshots render in
 *     the runner's system fonts. Consistent inside CI, different on your machine.
 *   * The canvas is real. Cytoscape renders into a WebGL/canvas layer that a
 *     headless browser rasterises in software, so anti-aliasing differs slightly
 *     from a GPU-backed desktop. Layout, colour and labels do not.
 *
 * Output: <out>/<viewport>/<state>.png plus <out>/matrix.json describing each
 * shot (viewport, state, duration, assertion result). Nothing is committed;
 * CI uploads the directory as a build artefact.
 */

import { createServer } from "node:http";
import { readFileSync, existsSync, mkdirSync, writeFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const WEB = path.join(ROOT, "web");

/* -------------------------------------------------------------------------- */
/*  Flags                                                                      */
/* -------------------------------------------------------------------------- */

function parseFlags(argv) {
  const flags = { out: ".screenshots", only: "", viewport: "", list: false, timeout: 45000 };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--list") flags.list = true;
    else if (arg === "--only") flags.only = String(argv[++i] || "");
    else if (arg === "--viewport") flags.viewport = String(argv[++i] || "");
    else if (arg === "--out") flags.out = String(argv[++i] || ".screenshots");
    else if (arg === "--timeout") flags.timeout = Number(argv[++i] || 45000);
    else if (arg.startsWith("--")) throw new Error(`unknown flag ${arg}`);
  }
  return flags;
}

const FLAGS = parseFlags(process.argv.slice(2));

/* -------------------------------------------------------------------------- */
/*  The matrix                                                                 */
/* -------------------------------------------------------------------------- */

const VIEWPORTS = [
  { id: "mobile-360", width: 360, height: 740, scale: 2, mobile: true, touch: true,
    note: "smallest supported phone: drawers instead of panels, footer scrolls" },
  { id: "tablet-820", width: 820, height: 1180, scale: 2, mobile: true, touch: true,
    note: "portrait tablet: rail and inspector become overlays" },
  { id: "laptop-1280", width: 1280, height: 800, scale: 1, mobile: false, touch: false,
    note: "the reference size: everything docked, nothing hidden" },
  { id: "desktop-1920", width: 1920, height: 1080, scale: 1, mobile: false, touch: false,
    note: "wide screen: canvas takes the room, panels stay put" },
];

/**
 * Each state: how to get there, and what must be true when we arrive. `prepare`
 * and `assert` run in the browser (Playwright serialises them), so they may only
 * touch page globals — no Node imports, no closures over this file.
 */
const STATES = [
  {
    id: "01-graph",
    title: "Graph view, demo dataset",
    prepare: async (page) => { await page.evaluate(() => window.PuppetNET.actions.switchView("graph")); },
    assert: () => {
      const api = window.PuppetNET;
      if (!api.cy || api.cy.nodes().length === 0) return "the canvas has no nodes";
      const shown = Number(document.querySelector("#hud-nodes").textContent);
      if (!(shown > 0)) return `the HUD reports ${shown} nodes although the canvas has some`;
      if (!document.querySelector(".cy-hud").textContent.trim()) return "the HUD is empty";
      return null;
    },
  },
  {
    id: "02-search",
    title: "Search with live suggestions",
    prepare: async (page) => {
      await page.evaluate(() => {
        const api = window.PuppetNET;
        const name = Array.from(api.state.nodes.values())
          .map((node) => node.name || "").filter(Boolean)
          .sort((a, b) => b.length - a.length)[0] || "a";
        const input = document.querySelector("#search");
        input.focus();
        input.value = name.slice(0, Math.min(9, name.length));
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
      await page.waitForFunction(() => !document.querySelector("#search-suggest").hidden, null, { timeout: 10000 });
    },
    assert: () => {
      const list = document.querySelector("#search-suggest");
      if (list.hidden) return "the suggestion list is hidden";
      if (!list.querySelectorAll("li, [role=option]").length) return "the suggestion list is empty";
      return null;
    },
  },
  {
    id: "03-inspector",
    title: "Node selected, inspector populated",
    prepare: async (page) => {
      await page.evaluate(() => {
        const api = window.PuppetNET;
        const nodes = Array.from(api.state.nodes.values());
        // The most central node makes the richest inspector: metrics, ties, sources.
        const best = nodes.slice().sort((a, b) => (b.betweenness || 0) - (a.betweenness || 0))[0];
        api.cy.getElementById(best.key).emit("tap");
      });
      await page.waitForFunction(
        () => !document.querySelector("#inspector-body .inspector-empty"), null, { timeout: 10000 },
      );
    },
    assert: () => {
      const body = document.querySelector("#inspector-body");
      if (body.querySelector(".inspector-empty")) return "the inspector still shows its empty state";
      if (body.textContent.trim().length < 80) return "the inspector rendered almost nothing";
      return null;
    },
  },
  {
    id: "04-handshake",
    title: "Handshake chain between two entities",
    prepare: async (page) => {
      await page.evaluate(async () => {
        const api = window.PuppetNET;
        const nodes = Array.from(api.state.nodes.values());
        const edges = Array.from(api.state.edges.values());
        let pair = null;
        for (const a of nodes) {
          for (const b of nodes) {
            if (a.key === b.key) continue;
            const result = api.localShortestPath(nodes, edges, a.key, b.key, 12, "hops");
            if (result && result.found && result.hops >= 3) { pair = { from: a.key, to: b.key }; break; }
          }
          if (pair) break;
        }
        if (!pair) throw new Error("the demo network has no three-hop pair to photograph");
        api.actions.switchView("path");
        const from = document.querySelector("#path-a");
        const to = document.querySelector("#path-b");
        from.value = pair.from;
        to.value = pair.to;
        from.dispatchEvent(new Event("change", { bubbles: true }));
        to.dispatchEvent(new Event("change", { bubbles: true }));
        await api.actions.findPath();
      });
      await page.waitForFunction(
        () => window.PuppetNET.state.path && window.PuppetNET.state.path.found, null, { timeout: 20000 },
      );
    },
    assert: () => {
      const result = document.querySelector("#path-result");
      if (!result || !result.children.length) return "the chain panel is empty";
      const path = window.PuppetNET.state.path;
      if (!path || !path.found) return "no path in state although the panel rendered";
      if (!result.textContent.includes(String(path.hops))) return "the panel does not state its hop count";
      return null;
    },
  },
  {
    id: "05-table",
    title: "Evidence table, sorted and paged",
    prepare: async (page) => {
      await page.evaluate(() => window.PuppetNET.actions.switchView("table"));
      await page.waitForFunction(
        () => document.querySelectorAll("#data-body tr").length > 0, null, { timeout: 15000 },
      );
    },
    assert: () => {
      const rows = document.querySelectorAll("#data-body tr").length;
      if (rows === 0) return "the table has no rows";
      const heads = document.querySelectorAll("#data-table thead th").length;
      if (heads < 3) return `the table has only ${heads} columns`;
      if (!document.querySelector("#table-filter")) return "the row filter is missing";
      return null;
    },
  },
  {
    id: "06-donate-sol",
    title: "Donation dialog, SOL with a local QR code",
    prepare: async (page) => {
      await page.evaluate(() => document.querySelector("#btn-donate").click());
      await page.waitForFunction(() => !document.querySelector("#modal-donate").hidden, null, { timeout: 8000 });
    },
    assert: () => {
      if (document.querySelector("#modal-donate").hidden) return "the dialog did not open";
      const address = document.querySelector("#donate-address").textContent;
      if (address.length < 30) return `the address looks wrong: ${address}`;
      if (!document.querySelector("#donate-qr svg")) return "no QR code was rendered";
      if (document.querySelector("#donate-qr .qr-missing")) return "the QR generator did not load";
      return null;
    },
  },
  {
    id: "07-donate-btc",
    title: "Donation dialog, BTC tab",
    prepare: async (page) => {
      await page.evaluate(() => {
        if (document.querySelector("#modal-donate").hidden) document.querySelector("#btn-donate").click();
        document.querySelector('.donate-tab[data-coin="btc"]').click();
      });
      await page.waitForFunction(() => !document.querySelector("#modal-donate").hidden, null, { timeout: 8000 });
    },
    assert: () => {
      const active = document.querySelector(".donate-tab.is-active");
      if (!active || active.getAttribute("data-coin") !== "btc") return "the BTC tab is not active";
      if (!document.querySelector("#donate-address").textContent.startsWith("bc1")) return "not a bech32 address";
      if (!document.querySelector("#donate-qr svg")) return "no QR code for BTC";
      return null;
    },
  },
  {
    id: "08-disclaimer",
    title: "Legal disclaimer",
    prepare: async (page) => {
      await page.evaluate(() => document.querySelector('[data-open-modal="#modal-disclaimer"]').click());
      await page.waitForFunction(() => !document.querySelector("#modal-disclaimer").hidden, null, { timeout: 8000 });
    },
    assert: () => {
      const body = document.querySelector("#modal-disclaimer .modal-body");
      if (body.textContent.replace(/\s+/g, " ").length < 1500) return "the disclaimer is too short to be a disclaimer";
      if (!/not an accusation/i.test(body.textContent)) return "the key sentence is missing";
      return null;
    },
  },
  {
    id: "09-terms",
    title: "Terms of Service",
    prepare: async (page) => {
      await page.evaluate(() => document.querySelector('[data-open-modal="#modal-terms"]').click());
      await page.waitForFunction(() => !document.querySelector("#modal-terms").hidden, null, { timeout: 8000 });
    },
    assert: () => {
      const body = document.querySelector("#modal-terms .modal-body");
      if (body.textContent.replace(/\s+/g, " ").length < 1500) return "the terms are too short";
      if (!/MIT licence/.test(body.textContent)) return "the licence is not named";
      return null;
    },
  },
  {
    id: "10-guide",
    title: "Help & OSINT methodology guide",
    prepare: async (page) => {
      await page.evaluate(() => document.querySelector('[data-open-modal="#modal-guide"]').click());
      await page.waitForFunction(() => !document.querySelector("#modal-guide").hidden, null, { timeout: 8000 });
    },
    assert: () => {
      const body = document.querySelector("#modal-guide .modal-body");
      for (const term of ["Betweenness centrality", "Clustering coefficient", "Anomaly score"]) {
        if (!body.textContent.includes(term)) return `the guide does not explain ${term}`;
      }
      if (!document.querySelector("#modal-guide pre code")) return "the Cypher snippet is missing";
      return null;
    },
  },
  {
    id: "11-contact",
    title: "Contact and attribution",
    prepare: async (page) => {
      await page.evaluate(() => document.querySelector('[data-open-modal="#modal-contact"]').click());
      await page.waitForFunction(() => !document.querySelector("#modal-contact").hidden, null, { timeout: 8000 });
    },
    assert: () => {
      const link = document.querySelector('#modal-contact a[href="mailto:hippie.highho@gmail.com"]');
      if (!link) return "the contact mailto is missing";
      if (!document.querySelector("#contact-copy")) return "the copy button is missing";
      return null;
    },
  },
  {
    id: "12-settings",
    title: "Settings and data source",
    prepare: async (page) => {
      await page.evaluate(() => document.querySelector("#btn-settings").click());
      await page.waitForFunction(() => !document.querySelector("#modal-settings").hidden, null, { timeout: 8000 });
    },
    assert: () => {
      if (!document.querySelector("#cfg-mode")) return "the mode selector is missing";
      if (!document.querySelector("#modal-settings .modal-body").textContent.includes("Worker")) {
        return "the settings dialog does not explain the modes";
      }
      return null;
    },
  },
  {
    id: "13-fallback",
    title: "Unreachable API falls back to demo data",
    prepare: async (page, origin) => {
      await page.goto(`${origin}/?api=https://unreachable.invalid`, { waitUntil: "load" });
      await page.waitForFunction(() => window.PuppetNET && window.PuppetNET.state.ready === true, null, { timeout: 45000 });
      await page.waitForFunction(
        () => (document.querySelector("#cy-toast-host") || { textContent: "" }).textContent.length > 0
          || window.PuppetNET.config.mode === "demo",
        null,
        { timeout: 20000 },
      );
    },
    assert: () => {
      const api = window.PuppetNET;
      if (!api.cy || api.cy.nodes().length === 0) return "the fallback loaded no data at all";
      const pill = document.querySelector("#conn-pill");
      const banner = document.querySelector("#cy-toast-host").textContent
        + " " + pill.textContent + " " + pill.getAttribute("data-state");
      if (!/demo|unreachable|fell back|fallback/i.test(banner)) {
        return `nothing on screen says the API was unreachable: ${banner.trim().slice(0, 90)}`;
      }
      return null;
    },
  },
  {
    id: "14-print",
    title: "Print stylesheet (a subgraph pasted into a report)",
    media: "print",
    prepare: async (page) => {
      await page.evaluate(() => window.PuppetNET.actions.switchView("graph"));
      await page.emulateMedia({ media: "print" });
    },
    assert: () => {
      const hidden = (selector) => getComputedStyle(document.querySelector(selector)).display === "none";
      if (!hidden(".site-footer")) return "the site footer still prints";
      if (!hidden(".app-header")) return "the header still prints";
      if (!hidden(".rail")) return "the filter rail still prints";
      return null;
    },
  },
  {
    id: "15-drawer-rail",
    title: "Mobile drawer: filter rail",
    viewports: ["mobile-360", "tablet-820"],
    prepare: async (page) => {
      await page.evaluate(() => {
        window.PuppetNET.actions.switchView("graph");
        document.querySelector("#btn-rail").click();
      });
      await page.waitForFunction(
        () => document.querySelector(".rail").classList.contains("is-open"), null, { timeout: 8000 },
      );
    },
    assert: () => {
      const rail = document.querySelector(".rail");
      if (!rail.classList.contains("is-open")) return "the rail did not open";
      if (getComputedStyle(document.querySelector("#rail-scrim, .scrim")).display === "none") {
        return "no scrim behind the drawer — a tap outside would not close it";
      }
      return null;
    },
  },
  {
    id: "16-drawer-inspector",
    title: "Mobile drawer: inspector",
    viewports: ["mobile-360", "tablet-820"],
    prepare: async (page) => {
      await page.evaluate(() => {
        const api = window.PuppetNET;
        const best = Array.from(api.state.nodes.values())
          .sort((a, b) => (b.betweenness || 0) - (a.betweenness || 0))[0];
        api.cy.getElementById(best.key).emit("tap");
        const button = document.querySelector("#btn-inspector");
        if (button && getComputedStyle(button).display !== "none") button.click();
      });
      await page.waitForTimeout(400);
    },
    assert: () => {
      const body = document.querySelector("#inspector-body");
      if (body.querySelector(".inspector-empty")) return "the inspector is empty after selecting a node";
      return null;
    },
  },
];

/* -------------------------------------------------------------------------- */
/*  Static server                                                              */
/* -------------------------------------------------------------------------- */

const MIME = {
  ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
  ".png": "image/png", ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8",
  ".xml": "application/xml; charset=utf-8", ".ico": "image/x-icon", ".woff2": "font/woff2",
};

function serve(directory) {
  return new Promise((resolve, reject) => {
    const server = createServer((request, response) => {
      const url = new URL(request.url, "http://127.0.0.1");
      const relative = decodeURIComponent(url.pathname).replace(/^\/+/, "") || "index.html";
      const file = path.join(directory, relative);
      // Path traversal would let a screenshot run read outside web/.
      if (!file.startsWith(directory) || !existsSync(file) || !statSync(file).isFile()) {
        response.writeHead(404, { "Content-Type": "text/plain" });
        response.end("not found");
        return;
      }
      response.writeHead(200, {
        "Content-Type": MIME[path.extname(file).toLowerCase()] || "application/octet-stream",
        "Cache-Control": "no-store",
      });
      response.end(readFileSync(file));
    });
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      resolve({ server, origin: `http://127.0.0.1:${server.address().port}` });
    });
  });
}

/* -------------------------------------------------------------------------- */
/*  Run                                                                        */
/* -------------------------------------------------------------------------- */

function printMatrix() {
  console.log(`screenshot matrix: ${VIEWPORTS.length} viewports × ${STATES.length} states\n`);
  for (const viewport of VIEWPORTS) {
    const states = STATES.filter((state) => !state.viewports || state.viewports.includes(viewport.id));
    console.log(`  ${viewport.id.padEnd(14)} ${String(viewport.width).padStart(4)}×${viewport.height}  ${viewport.note}`);
    console.log(`  ${"".padEnd(14)} ${states.length} states: ${states.map((state) => state.id).join(", ")}`);
  }
  const total = VIEWPORTS.reduce((sum, viewport) => sum
    + STATES.filter((state) => !state.viewports || state.viewports.includes(viewport.id)).length, 0);
  console.log(`\n  ${total} screenshots per full run`);
}

/** Return every state to a clean graph view before the next shot. */
async function reset(page, origin) {
  await page.emulateMedia({ media: "screen" });
  if (new URL(page.url()).search) await page.goto(`${origin}/`, { waitUntil: "load" });
  await page.evaluate(() => {
    const api = window.PuppetNET;
    if (!api) return;
    if (api.state.modalOpen) api.actions.closeModal();
    api.actions.switchView("graph");
    const rail = document.querySelector(".rail");
    if (rail && rail.classList.contains("is-open")) {
      const button = document.querySelector("#btn-rail");
      if (button) button.click();
    }
    const search = document.querySelector("#search");
    if (search) { search.value = ""; search.dispatchEvent(new Event("input", { bubbles: true })); search.blur(); }
  });
  await page.waitForFunction(() => window.PuppetNET && window.PuppetNET.state.ready === true, null, { timeout: 30000 });
  await page.waitForTimeout(250);
}

/**
 * Launch Chromium, and if the sandbox is what stopped it, say so and retry
 * without it. Container runners whose user namespace is unprivileged refuse the
 * setuid sandbox, and "Failed to launch" alone does not tell you that — a
 * screenshot job that dies in silence is undebuggable, so it dies loudly.
 */
async function launchChromium(chromium) {
  const args = ["--force-color-profile=srgb", "--font-render-hinting=none",
    "--hide-scrollbars", "--disable-dev-shm-usage"];
  try {
    return await chromium.launch({ args });
  } catch (error) {
    console.error(`screenshots: Chromium refused to launch — ${String(error.message).split("\n")[0]}`);
    console.error("screenshots: retrying without the setuid sandbox (CI runner only).");
    return chromium.launch({ args: args.concat(["--no-sandbox"]) });
  }
}

async function main() {
  if (FLAGS.list) { printMatrix(); return; }

  let chromium;
  try {
    ({ chromium } = await import("playwright"));
  } catch (_) {
    console.error("screenshots: Playwright is not installed.\n"
      + "  npm install -D playwright && npx playwright install --with-deps chromium\n"
      + "It is a dev dependency only — the console itself ships no runtime dependency.");
    process.exit(1);
  }

  const viewports = VIEWPORTS.filter((viewport) => !FLAGS.viewport || viewport.id.includes(FLAGS.viewport));
  const states = STATES.filter((state) => !FLAGS.only || state.id.includes(FLAGS.only) || state.title.toLowerCase().includes(FLAGS.only.toLowerCase()));
  if (!viewports.length || !states.length) {
    console.error("screenshots: the filters matched nothing. Try --list.");
    process.exit(1);
  }

  const out = path.resolve(ROOT, FLAGS.out);
  mkdirSync(out, { recursive: true });

  const shots = [];
  const failures = [];
  const pageErrors = [];
  const began = Date.now();

  // Everything below is staged, and the stage name is part of the failure
  // report: a run dies in seconds when a browser will not launch and in
  // forty-five when a state will not verify, and those need different fixes.
  let stage = "start";
  let origin = null;
  let server = null;
  let browser = null;
  let page = null;
  let fatal = null;

  try {
    stage = "serve";
    ({ server, origin } = await serve(WEB));
    stage = "launch";
    browser = await launchChromium(chromium);

    for (const viewport of viewports) {
      const applicable = states.filter((state) => !state.viewports || state.viewports.includes(viewport.id));
      if (!applicable.length) continue;
      stage = `context:${viewport.id}`;
      const context = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
        deviceScaleFactor: viewport.scale,
        isMobile: viewport.mobile,
        hasTouch: viewport.touch,
        colorScheme: "dark",
        reducedMotion: "no-preference",
      });
      page = await context.newPage();
      page.on("pageerror", (error) => pageErrors.push(`${viewport.id}: ${error.message}`));
      page.on("console", (message) => {
        if (message.type() === "error") pageErrors.push(`${viewport.id} console.error: ${message.text()}`);
      });

      stage = `goto:${viewport.id}`;
      await page.goto(`${origin}/`, { waitUntil: "load" });
      stage = `ready:${viewport.id}`;
      await page.waitForFunction(() => window.PuppetNET && window.PuppetNET.state.ready === true, null, { timeout: FLAGS.timeout });
      stage = `data:${viewport.id}`;
      await page.waitForFunction(() => window.PuppetNET.state.nodes.size > 0, null, { timeout: FLAGS.timeout });
      await page.waitForTimeout(900); // let fcose settle; a mid-layout shot is a lie

      const dir = path.join(out, viewport.id);
      mkdirSync(dir, { recursive: true });

      for (const state of applicable) {
        stage = `state:${viewport.id}/${state.id}`;
        const started = Date.now();
        await reset(page, origin);
        let prepareError = null;
        try {
          await state.prepare(page, origin);
          await page.waitForTimeout(state.settle || 350);
        } catch (error) {
          prepareError = error.message.split("\n")[0];
        }
        let verdict = prepareError;
        if (!verdict) {
          try {
            verdict = await page.evaluate(state.assert);
          } catch (error) {
            verdict = `assertion threw: ${error.message.split("\n")[0]}`;
          }
        }
        const file = path.join(dir, `${state.id}.png`);
        await page.screenshot({ path: file, animations: "disabled" });
        const shot = {
          viewport: viewport.id, width: viewport.width, height: viewport.height,
          state: state.id, title: state.title, file: path.relative(ROOT, file),
          ms: Date.now() - started, ok: !verdict, problem: verdict || null,
        };
        shots.push(shot);
        if (verdict) failures.push(shot);
        console.log(`  ${verdict ? "FAIL" : " ok "} ${viewport.id}/${state.id}.png  ${shot.ms} ms${verdict ? `  — ${verdict}` : ""}`);
      }
      stage = `close:${viewport.id}`;
      await context.close();
      page = null;
    }
  } catch (error) {
    fatal = {
      stage,
      message: String((error && error.message) || error),
      stack: String((error && error.stack) || "").split("\n").slice(0, 12).join("\n"),
    };
    // Evidence before exit: the run's log host is not always reachable from
    // wherever the failure is being triaged, but the artefact is.
    if (page) {
      try {
        await page.screenshot({ path: path.join(out, "failure.png"), fullPage: true });
        fatal.screenshot = "failure.png";
      } catch (_) { /* the page may already be gone */ }
      try {
        fatal.page = await page.evaluate(() => ({
          url: location.href,
          title: document.title,
          hasApi: Boolean(window.PuppetNET),
          ready: window.PuppetNET ? window.PuppetNET.state.ready : null,
          provider: window.PuppetNET && window.PuppetNET.state.provider
            ? window.PuppetNET.state.provider.name : null,
          nodes: window.PuppetNET ? window.PuppetNET.state.nodes.size : null,
          bootStatus: document.querySelector("#boot-status")
            ? document.querySelector("#boot-status").textContent : null,
          appHidden: document.querySelector("#app") ? document.querySelector("#app").hidden : null,
        }));
      } catch (_) { /* same */ }
    }
    console.error(`\nscreenshot matrix FAILED during "${stage}":`);
    console.error(`  ${fatal.message}`);
    if (fatal.page) console.error(`  page state: ${JSON.stringify(fatal.page)}`);
    if (fatal.stack) {
      console.error(fatal.stack.split("\n").slice(1, 6).map((line) => `  ${line.trim()}`).join("\n"));
    }
  } finally {
    if (browser) { try { await browser.close(); } catch (_) { /* already down */ } }
    if (server) { try { server.close(); } catch (_) { /* already down */ } }
    writeFileSync(path.join(out, "matrix.json"), JSON.stringify({
      generated: new Date().toISOString(),
      durationMs: Date.now() - began,
      viewports: VIEWPORTS,
      states: STATES.map(({ id, title, viewports: only }) => ({ id, title, viewports: only || "all" })),
      shots, failures, pageErrors, fatal,
    }, null, 2) + "\n");
  }

  console.log(`\nscreenshot matrix: ${shots.length - failures.length}/${shots.length} states verified `
    + `in ${((Date.now() - began) / 1000).toFixed(1)}s → ${path.relative(ROOT, out)}/`);
  if (pageErrors.length) {
    console.log(`\nuncaught page errors (${pageErrors.length}):`);
    pageErrors.slice(0, 10).forEach((error) => console.log(`  ${error}`));
  }
  if (fatal) {
    console.log("\nMATRIX FAILED: the stage above and matrix.json in the artefact say where.");
    process.exitCode = 1;
  } else if (failures.length) {
    console.log(`\nMATRIX FAILED: ${failures.length} state(s) did not verify.`);
    process.exitCode = 1;
  } else if (pageErrors.length) {
    console.log("\nMATRIX FAILED: the page threw while being photographed.");
    process.exitCode = 1;
  } else {
    console.log("MATRIX PASSED.");
  }
}

await main();
