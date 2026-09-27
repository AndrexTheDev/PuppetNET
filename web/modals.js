/**
 * modals.js — PuppetNET production package: donations, legal pages, guide,
 * contact and SEO hydration.
 *
 * Loaded after app.js with `defer`. It is deliberately *additive*:
 *
 *   - Modal open/close is delegated to app.js (`PuppetNET.actions.openModal` /
 *     `closeModal`) whenever the console is present, so `state.modalOpen` stays
 *     the single source of truth and the existing Escape cascade, focus
 *     handling and `[data-close]` delegation keep working for these dialogs.
 *     When app.js is absent (a bare static page embedding only this file), a
 *     self-contained fallback takes over.
 *   - Copying uses `PuppetNET.actions.copyText` when available (secure-context
 *     clipboard with a textarea/execCommand fallback), otherwise its own copy
 *     of that fallback.
 *   - Feedback goes through the console toast when present.
 *
 * Nothing here is inline: no `onclick`, no `eval`, no remote script, no webfont,
 * no analytics. `web/_headers` forbids inline script, and this file stays inside
 * that rule — QR codes are rendered from the vendored `vendor/qrcode.min.js`
 * (MIT) as SVG, never fetched from a third-party QR service, because handing a
 * QR image service the donation address would also tell it who is looking.
 *
 * Keyboard: `d` opens the donation dialog (the letter is unused by the console).
 *
 * Author: AndrexTheDev <hippie.highho@gmail.com> — MIT licence, same as the rest
 * of PuppetNET.
 */
(function () {
  "use strict";

  /* ==========================================================================
     1. Identity, coins and copy
     ======================================================================== */

  var AUTHOR = {
    name: "AndrexTheDev",
    email: "hippie.highho@gmail.com",
    github: "https://github.com/AndrexTheDev",
    repo: "https://github.com/AndrexTheDev/PuppetNET",
  };

  /**
   * Donation addresses. Constants on purpose: they are rendered into the DOM as
   * text nodes and into QR codes, never through HTML built from variables, so
   * there is no injection surface here at all.
   */
  var COINS = [
    {
      id: "sol",
      ticker: "SOL",
      name: "Solana",
      network: "Solana mainnet-beta",
      colour: "#14f195",
      address: "79KsqtJJdhKFJ9woxnYgtf3nq7HxQveafWBCtC3mxWi8",
      note: "Base58 address. Solana transfers settle in seconds and cost a fraction of a cent, which makes this the cheapest way to keep the lights on.",
    },
    {
      id: "btc",
      ticker: "BTC",
      name: "Bitcoin",
      network: "Bitcoin, native segwit (bech32)",
      colour: "#f7931a",
      address: "bc1qeqzrlfg3edrydk4s0hecakc82gp26n5p7hkc7f",
      note: "bech32 address starting with bc1q. Send on the Bitcoin network only — tokens sent over other chains to this address are unrecoverable.",
    },
    {
      id: "eth",
      ticker: "ETH",
      name: "Ethereum",
      network: "Ethereum mainnet (ERC-20 compatible)",
      colour: "#8a92b2",
      address: "0xBC3fab34f69bc9f6661608C3FB36dDdC313C42F7",
      note: "Checksummed EVM address. ERC-20 tokens on Ethereum mainnet are welcome; do not send via other EVM networks without checking first.",
    },
  ];

  var DONATE_MODAL = "#modal-donate";

  /* ==========================================================================
     2. Small DOM helpers
     ======================================================================== */

  function $(selector, root) { return (root || document).querySelector(selector); }
  function $$(selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        if (key === "class") node.className = attrs[key];
        else if (key === "text") node.textContent = attrs[key];
        else if (key === "html") node.innerHTML = attrs[key];
        else node.setAttribute(key, attrs[key]);
      });
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  /** The console, when it is on the page. Everything degrades without it. */
  function consoleApi() { return window.PuppetNET || null; }

  function toast(title, body, kind) {
    var api = consoleApi();
    if (api && api.actions && typeof api.actions.toast === "function") {
      api.actions.toast(title, body, kind || "info");
      return;
    }
    if (typeof console !== "undefined" && console.info) console.info("[puppetnet] " + title + " — " + body);
  }

  async function copyText(text) {
    var api = consoleApi();
    if (api && api.actions && typeof api.actions.copyText === "function") return api.actions.copyText(text);
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_) { /* fall through to the legacy path */ }
    try {
      var ta = el("textarea", { class: "sr-only" });
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;top:-1000px;opacity:0";
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch (_) { return false; }
  }

  /* ==========================================================================
     3. Modal plumbing — delegate to the console, fall back standalone
     ======================================================================== */

  var standaloneOpen = null;

  function openModal(id) {
    var modal = $(id);
    if (!modal) return;
    var api = consoleApi();
    if (api && api.actions && typeof api.actions.openModal === "function") {
      // One dialog at a time. The console's openModal only records which dialog is
      // open — it does not hide a previous one — and the footer's five links sit
      // next to each other, so "Terms" clicked while "Disclaimer" is showing would
      // otherwise stack two cards on top of each other.
      if (api.state && api.state.modalOpen && api.state.modalOpen !== id) api.actions.closeModal();
      api.actions.openModal(id);
      return;
    }
    modal.hidden = false;
    standaloneOpen = id;
    var focusable = modal.querySelector("button, input, select, a[href]");
    if (focusable) setTimeout(function () { focusable.focus(); }, 40);
  }

  function closeModal() {
    var api = consoleApi();
    if (api && api.actions && typeof api.actions.closeModal === "function") {
      api.actions.closeModal();
      return;
    }
    if (standaloneOpen) {
      var modal = $(standaloneOpen);
      if (modal) modal.hidden = true;
      standaloneOpen = null;
    }
  }

  function isOpen(id) {
    var api = consoleApi();
    if (api && api.state) return api.state.modalOpen === id;
    return standaloneOpen === id;
  }

  /** Any element with data-open-modal="#id" opens that dialog, by delegation. */
  function bindOpeners() {
    document.addEventListener("click", function (event) {
      var opener = event.target.closest ? event.target.closest("[data-open-modal]") : null;
      if (!opener) return;
      event.preventDefault();
      openModal(opener.getAttribute("data-open-modal"));
    });
    document.addEventListener("keydown", function (event) {
      // `d` opens the donation dialog; never while the analyst is typing.
      if (event.key !== "d" || event.metaKey || event.ctrlKey || event.altKey) return;
      var target = event.target;
      var tag = target && target.tagName ? String(target.tagName).toLowerCase() : "";
      if (tag === "input" || tag === "textarea" || tag === "select" || (target && target.isContentEditable)) return;
      if (isOpen(DONATE_MODAL)) return;
      var api = consoleApi();
      if (api && api.state && api.state.modalOpen) return; // let the console's own cascade run
      event.preventDefault();
      openModal(DONATE_MODAL);
    });
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      document.body.classList.add("reduce-motion");
    }
  }

  /* ==========================================================================
     4. Donation dialog — tabs, QR, copy
     ======================================================================== */

  var activeCoin = COINS[0].id;

  function coinById(id) {
    for (var i = 0; i < COINS.length; i += 1) if (COINS[i].id === id) return COINS[i];
    return COINS[0];
  }

  /** Fingerprint for a human eye: first 6 and last 6 characters. */
  function fingerprint(address) {
    return address.slice(0, 6) + "…" + address.slice(-6);
  }

  /**
   * Render the QR as inline SVG from the vendored generator. `createSvgTag`
   * returns markup built only from the address we pass in (a constant), so
   * assigning it is safe; the console's CSP allows inline *style* but not inline
   * script, and an SVG data block contains neither executable content nor a
   * `<script>`.
   */
  function renderQr(host, coin) {
    host.innerHTML = "";
    if (typeof window.qrcode !== "function") {
      host.appendChild(el("p", { class: "qr-missing", text: "QR rendering unavailable — the vendored generator did not load. The address above is still copyable." }));
      return;
    }
    try {
      var qr = window.qrcode(0, "M"); // 0 = smallest type that fits; M = 15% recovery
      qr.addData(coin.address);
      qr.make();
      host.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true });
      var svg = host.querySelector("svg");
      if (svg) {
        svg.setAttribute("role", "img");
        svg.setAttribute("aria-label", "QR code for the " + coin.name + " donation address");
        svg.classList.add("qr-svg");
      }
    } catch (err) {
      host.innerHTML = "";
      host.appendChild(el("p", { class: "qr-missing", text: "This address could not be rendered as a QR code. Copy it instead, and verify every character." }));
    }
  }

  function renderCoin(coin) {
    activeCoin = coin.id;

    $$("#modal-donate .donate-tab").forEach(function (tab) {
      var on = tab.getAttribute("data-coin") === coin.id;
      tab.classList.toggle("is-active", on);
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.setAttribute("tabindex", on ? "0" : "-1");
    });

    var name = $("#donate-coin-name");
    if (name) name.textContent = coin.name + " · " + coin.network;
    var addr = $("#donate-address");
    if (addr) addr.textContent = coin.address;
    var fp = $("#donate-fingerprint");
    if (fp) fp.textContent = fingerprint(coin.address);
    var note = $("#donate-note");
    if (note) note.textContent = coin.note;

    var qrHost = $("#donate-qr");
    if (qrHost) renderQr(qrHost, coin);

    var copy = $("#donate-copy");
    if (copy) {
      copy.setAttribute("data-coin", coin.id);
      copy.textContent = "Copy address";
    }
  }

  function bindDonate() {
    var modal = $("#modal-donate");
    if (!modal) return;

    var tabs = $("#donate-tabs");
    if (tabs) {
      tabs.addEventListener("click", function (event) {
        var tab = event.target.closest(".donate-tab");
        if (tab) renderCoin(coinById(tab.getAttribute("data-coin")));
      });
      // Roving focus: arrow keys move between coins, like a real tablist.
      tabs.addEventListener("keydown", function (event) {
        if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
        var list = $$(".donate-tab", tabs);
        var index = list.findIndex(function (tab) { return tab.classList.contains("is-active"); });
        var next = (index + (event.key === "ArrowRight" ? 1 : list.length - 1)) % list.length;
        event.preventDefault();
        renderCoin(coinById(list[next].getAttribute("data-coin")));
        list[next].focus();
      });
    }

    var copy = $("#donate-copy");
    if (copy) {
      copy.addEventListener("click", async function () {
        var coin = coinById(copy.getAttribute("data-coin") || activeCoin);
        var ok = await copyText(coin.address);
        if (ok) {
          copy.textContent = "Copied ✓";
          copy.classList.add("is-copied");
          setTimeout(function () {
            copy.textContent = "Copy address";
            copy.classList.remove("is-copied");
          }, 1600);
          toast(coin.ticker + " address copied", "Verify " + fingerprint(coin.address) + " in your wallet before sending.", "success");
        } else {
          toast("Clipboard blocked", "Select the address and copy it manually — " + fingerprint(coin.address), "warn");
        }
      });
    }

    renderCoin(coinById(activeCoin));
  }

  /* ==========================================================================
     5. Contact dialog — copy the email the same way
     ======================================================================== */

  function bindContact() {
    var copy = $("#contact-copy");
    if (!copy) return;
    copy.addEventListener("click", async function () {
      var ok = await copyText(AUTHOR.email);
      copy.textContent = ok ? "Copied ✓" : "Copy failed";
      setTimeout(function () { copy.textContent = "Copy email"; }, 1600);
      if (ok) toast("Email copied", AUTHOR.email, "success");
    });
  }

  /* ==========================================================================
     6. SEO hydration
     ======================================================================== */

  /**
   * The console is deployed to an origin that is not known at build time — a
   * Pages subdomain, a custom domain, localhost, a USB stick. Relative `og:*`
   * and canonical values are legal placeholders but useless to a crawler, so on
   * boot every URL-ish tag (and the JSON-LD graph) is rewritten against
   * `location.origin`. The static values stay as the no-JS fallback.
   */
  function hydrateSeo() {
    var origin = window.location && window.location.origin;
    if (!origin || origin === "null" || origin === "file://") return;

    function absolute(value) {
      if (!value) return value;
      if (/^[a-z][a-z0-9+.-]*:/i.test(value)) return value; // already absolute
      try { return new URL(value, window.location.href).href; } catch (_) { return value; }
    }

    var canonical = $('link[rel="canonical"]');
    if (canonical) canonical.setAttribute("href", absolute(canonical.getAttribute("href") || "./"));

    [["property", "og:url"], ["property", "og:image"], ["name", "twitter:image"]].forEach(function (pair) {
      var meta = document.head.querySelector('meta[' + pair[0] + '="' + pair[1] + '"]');
      if (meta) meta.setAttribute("content", absolute(meta.getAttribute("content")));
    });

    var ld = $('script[type="application/ld+json"]');
    if (!ld) return;
    try {
      var graph = JSON.parse(ld.textContent);
      var nodes = graph && Array.isArray(graph["@graph"]) ? graph["@graph"]
        : Array.isArray(graph) ? graph : [graph];
      nodes.forEach(function (node) {
        // `@id` first: it is the node's identity, and every reference to it has to
        // move with it or the graph splits into two unrelated entities.
        ["@id", "url", "image", "screenshot", "codeRepository", "license"].forEach(function (key) {
          if (typeof node[key] === "string" && !/^(https?:|mailto:)/i.test(node[key])) node[key] = absolute(node[key]);
        });
        ["author", "maintainer", "publisher"].forEach(function (key) {
          var ref = node[key];
          if (!ref || typeof ref !== "object") return;
          if (typeof ref["@id"] === "string") ref["@id"] = absolute(ref["@id"]);
          if (typeof ref.url === "string") ref.url = absolute(ref.url);
        });
        if (node.potentialAction && node.potentialAction.target) {
          var target = node.potentialAction.target;
          if (typeof target.urlTemplate === "string") target.urlTemplate = absolute(target.urlTemplate);
          if (typeof target["@id"] === "string") target["@id"] = absolute(target["@id"]);
        }
      });
      ld.textContent = JSON.stringify(graph);
    } catch (_) { /* leave the shipped JSON-LD untouched rather than corrupt it */ }
  }

  /* ==========================================================================
     7. Footer chrome
     ======================================================================== */

  function bindFooter() {
    var year = $("#foot-year");
    if (year) year.textContent = String(new Date().getFullYear());
    var email = $("#foot-email");
    if (email && email.textContent === "") email.textContent = AUTHOR.email;
  }

  /* ==========================================================================
     8. Boot
     ======================================================================== */

  function init() {
    bindOpeners();
    bindDonate();
    bindContact();
    bindFooter();
    hydrateSeo();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  window.PuppetNETModals = {
    version: "1.0.0",
    author: AUTHOR,
    coins: COINS,
    open: openModal,
    close: closeModal,
    renderCoin: renderCoin,
    hydrateSeo: hydrateSeo,
    fingerprint: fingerprint,
  };
})();
