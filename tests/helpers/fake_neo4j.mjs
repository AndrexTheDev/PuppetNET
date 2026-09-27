/**
 * fake_neo4j.mjs — a stub Neo4j server for the smoke tests.
 *
 * There is no database in CI, and there must not be: what these suites assert is
 * what the Worker *sends* and what it *returns*, not whether Neo4j works. Two
 * suites share this file so they cannot drift apart — `worker_smoke.mjs` checks
 * the HTTP surface, and `web_smoke.mjs` drives the browser console against the
 * real Worker on top of it.
 *
 * It does not parse Cypher. It dispatches on distinctive substrings of the
 * statement the Worker emitted, and derives response columns from that
 * statement's own `RETURN … AS x` aliases. That makes the fixture a contract
 * test: rename a projected field and the stub starts returning null for it, so
 * the shaping assertions fail the way a real schema drift would. Anything it does
 * not recognise raises a Neo4j-style syntax error rather than returning nothing,
 * so an unexpected statement shape is loud.
 */

const GRAPH_INDEX = "puppetnet_entity_search";

/** Sortable properties the Worker is allowed to interpolate into ORDER BY. */
const STUB_SORTABLE = new Set([
  "anomaly_score", "betweenness", "degree_spike", "offshore_cluster_ratio",
  "confidence", "mention_count", "risk_score", "name", "canonical_key",
  "last_seen", "first_seen", "metrics_at", "weight", "observations",
]);

/** Every tx/commit request the Worker made, in order. */
const neo4jCalls = [];

/** Knobs a check flips to make the fake server misbehave. */
const neo4jBehaviour = {
  unauthorized: false,
  error: null,
  rawText: null,
  noFulltextIndex: false,
};

/* ---- fixture graph ------------------------------------------------------- */

function fxNode(key, name, extra = {}) {
  return {
    key,
    name,
    entity_type: extra.entity_type || "Organization",
    labels: extra.labels || ["Entity", "Organization"],
    jurisdiction: extra.jurisdiction || "",
    confidence: extra.confidence === undefined ? 0.9 : extra.confidence,
    mention_count: extra.mention_count === undefined ? 3 : extra.mention_count,
    degree: extra.degree === undefined ? 2 : extra.degree,
    betweenness: extra.betweenness === undefined ? 0.1 : extra.betweenness,
    anomaly_score: extra.anomaly_score === undefined ? 0.2 : extra.anomaly_score,
    degree_spike: extra.degree_spike === undefined ? 0 : extra.degree_spike,
    offshore_cluster_ratio: extra.offshore_cluster_ratio === undefined ? 0 : extra.offshore_cluster_ratio,
    cluster_id: extra.cluster_id === undefined ? null : extra.cluster_id,
    risk_score: extra.risk_score === undefined ? 0.1 : extra.risk_score,
    first_seen: "2026-01-04T09:00:00Z",
    last_seen: "2026-09-20T11:00:00Z",
    metrics_at: "2026-09-27T04:30:00Z",
    aliases: extra.aliases || [],
    source_ids: extra.source_ids || ["opencorporates"],
    doc_ids: extra.doc_ids || [],
    merged_from: [],
    match_score: extra.match_score === undefined ? 0 : extra.match_score,
    reg_number: extra.reg_number || null,
    company_number: extra.company_number || null,
    lei: extra.lei || null,
    imo: extra.imo || null,
    mmsi: extra.mmsi || null,
    tail_number: extra.tail_number || null,
    transponder: extra.transponder || null,
    icao24: extra.icao24 || null,
    wikidata_id: extra.wikidata_id || null,
    wikipedia_id: extra.wikipedia_id || null,
    opencorporates_url: extra.opencorporates_url || null,
    address_key: extra.address_key || null,
    shell_risk: extra.shell_risk || null,
    flag: extra.flag || null,
    nationality: extra.nationality || null,
    anomaly_degree_spike: null,
    anomaly_offshore_cluster_ratio: null,
    anomaly_betweenness: null,
    degree_prev: null,
  };
}

function fxEdge(id, source, target, type, extra = {}) {
  return {
    id,
    source,
    target,
    type,
    weight: extra.weight === undefined ? 0.8 : extra.weight,
    confidence: extra.confidence === undefined ? 0.75 : extra.confidence,
    source_weight: extra.source_weight === undefined ? 0.85 : extra.source_weight,
    method: extra.method || "rule:registry",
    observations: extra.observations === undefined ? 2 : extra.observations,
    evidence: extra.evidence || ["Meridian Holdings Ltd is the registered shareholder"],
    evidence_scores: extra.evidence_scores || [0.9],
    verb: extra.verb || type.toLowerCase(),
    rule: extra.rule || "oc-shareholder",
    source_id: extra.source_id || "opencorporates",
    doc_id: extra.doc_id || "oc:cy:HE312345",
    run_id: extra.run_id || "run-2026-09-27",
    negated: Boolean(extra.negated),
    hedged: Boolean(extra.hedged),
    passive: Boolean(extra.passive),
    first_seen: "2026-02-11T08:00:00Z",
    last_seen: "2026-09-18T08:00:00Z",
  };
}

function fxDoc(docId, title, extra = {}) {
  return {
    doc_id: docId,
    title,
    url: extra.url || `https://api.opencorporates.com/companies/cy/${docId.split(":").pop()}`,
    source_id: extra.source_id || "opencorporates",
    source_name: extra.source_name || "OpenCorporates",
    source_kind: extra.source_kind || "registry",
    source_weight: extra.source_weight === undefined ? 0.85 : extra.source_weight,
    published_at: extra.published_at || "2026-03-02T00:00:00Z",
    fetched_at: "2026-09-20T11:00:00Z",
    content_hash: extra.content_hash || "sha256:0f2c…",
    entity_count: extra.entity_count === undefined ? 4 : extra.entity_count,
    count: extra.count === undefined ? 2 : extra.count,
    confidence: extra.confidence === undefined ? 0.78 : extra.confidence,
    surface_forms: extra.surface_forms || ["Meridian Holdings"],
  };
}

const P_KASTELION = "PERSON:vladimir-kastelion-9f2c1a7b";
const O_MERIDIAN = "ORGANIZATION:meridian-holdings-ltd-3d81be04";
const O_SARNEN = "ORGANIZATION:sarnen-offshore-services-77aa02c1";
const F_TALLOW = "ORGANIZATION:tallow-creek-trust-5b1e9d40";
const L_LIMASSOL = "LOCATION:limassol-cy-e04c7719";
const A_TAIL = "AIRCRAFT:9h-kast-a4b2c6-61f0bb23";
const P_UNKNOWN = "PERSON:nobody-here-00000000";

const FX = {
  nodes: [
    fxNode(P_KASTELION, "Vladimir Kastelion", {
      entity_type: "Person", labels: ["Entity", "Person"], jurisdiction: "CY",
      aliases: ["V. Kastelion", "Кастелион"], betweenness: 0.412, anomaly_score: 0.83,
      degree: 4, mention_count: 17, cluster_id: 7, risk_score: 0.71,
      wikidata_id: "Q12345678", degree_spike: 3.5, offshore_cluster_ratio: 0.6,
    }),
    fxNode(O_MERIDIAN, "Meridian Holdings Ltd", {
      jurisdiction: "CY", company_number: "HE312345", betweenness: 0.288,
      anomaly_score: 0.51, degree: 4, cluster_id: 7, shell_risk: 0.42, mention_count: 9,
    }),
    fxNode(O_SARNEN, "Sarnen Offshore Services SA", {
      jurisdiction: "PA", labels: ["Entity", "Organization", "ShellCompany", "Offshore"],
      entity_type: "ShellCompany", betweenness: 0.19, anomaly_score: 0.66, degree: 3,
      cluster_id: 7, shell_risk: 0.88, address_key: "addr:panama-city-01",
    }),
    fxNode(F_TALLOW, "Tallow Creek Trust", {
      jurisdiction: "JE", labels: ["Entity", "Organization", "Foundation"],
      entity_type: "Foundation", betweenness: 0.04, anomaly_score: 0.12, degree: 1,
      cluster_id: 12, reg_number: "JE-F-9981",
    }),
    fxNode(L_LIMASSOL, "Limassol, Cyprus", {
      entity_type: "Location", labels: ["Entity", "Location"], jurisdiction: "CY",
      betweenness: 0.55, anomaly_score: 0.05, degree: 3, cluster_id: 7,
    }),
    fxNode(A_TAIL, "9H-KAST (Gulfstream G650)", {
      entity_type: "Aircraft", labels: ["Entity", "Craft", "Aircraft"], jurisdiction: "MT",
      betweenness: 0.02, anomaly_score: 0.31, degree: 1, cluster_id: 7,
      tail_number: "9H-KAST", icao24: "484556", transponder: "484556", flag: "MT",
    }),
  ],
  edges: [
    fxEdge(9001, P_KASTELION, O_MERIDIAN, "OWNS", { weight: 0.92, confidence: 0.88, method: "rule:registry", verb: "owns" }),
    fxEdge(9002, O_MERIDIAN, O_SARNEN, "SUBSIDIARY_OF", { weight: 0.71, confidence: 0.64, method: "rule:filing", verb: "is a subsidiary of" }),
    fxEdge(9003, O_SARNEN, A_TAIL, "REGISTERED_TO", { weight: 0.35, confidence: 0.3, method: "nlp:passenger", hedged: true, verb: "registered to" }),
    fxEdge(9004, P_KASTELION, L_LIMASSOL, "LOCATED_IN", { weight: 0.6, confidence: 0.9 }),
    fxEdge(9005, O_MERIDIAN, L_LIMASSOL, "REGISTERED_IN", { weight: 0.83, confidence: 0.95, method: "rule:registry" }),
    fxEdge(9006, O_SARNEN, F_TALLOW, "INTERMEDIARY_FOR", { weight: 0.44, confidence: 0.38, method: "nlp:leaks" }),
    fxEdge(9007, P_KASTELION, O_SARNEN, "CONTROLS", { weight: 0.58, confidence: 0.41, method: "nlp:leaks", observations: 5 }),
  ],
  docs: [
    fxDoc("oc:cy:HE312345", "Meridian Holdings Ltd — Cyprus registrar extract", { count: 3, confidence: 0.82 }),
    fxDoc("icij:offshore:4471", "Sarnen Offshore Services SA — Offshore Leaks", {
      source_id: "icij-offshore-leaks", source_name: "ICIJ Offshore Leaks", source_kind: "leaks",
      source_weight: 0.95, count: 5, confidence: 0.9, surface_forms: ["Sarnen Offshore", "Sarnen SA"],
      published_at: "2026-05-09T00:00:00Z",
    }),
  ],
};

const FX_BY_KEY = new Map(FX.nodes.map((node) => [node.key, node]));

/* ---- Cypher helpers ------------------------------------------------------ */

function stubError(message, code) {
  const error = new Error(message);
  error.code = code || "Neo.ClientError.Statement.SyntaxError";
  return error;
}

/** Split on commas that are not inside (), [] or {}. */
function splitTopLevel(text) {
  const parts = [];
  let depth = 0;
  let current = "";
  for (const char of text) {
    if ("([{".includes(char)) depth += 1;
    else if (")]}".includes(char)) depth -= 1;
    if (char === "," && depth === 0) {
      parts.push(current);
      current = "";
      continue;
    }
    current += char;
  }
  if (current.trim()) parts.push(current);
  return parts;
}

/** The column names the Worker asked for, taken from its own RETURN clause. */
function returnColumns(statement) {
  const index = statement.lastIndexOf("RETURN ");
  if (index < 0) throw stubError(`statement has no RETURN clause: ${statement.slice(0, 120)}`);
  return splitTopLevel(statement.slice(index + 7)).map((part) => {
    const trimmed = part.trim();
    const alias = /\bAS\s+([A-Za-z_][\w]*)\s*$/i.exec(trimmed);
    return alias ? alias[1] : trimmed;
  });
}

function rows(columns, objects) {
  return {
    columns,
    data: objects.map((object) => ({
      row: columns.map((name) => (Object.prototype.hasOwnProperty.call(object, name) ? object[name] : null)),
    })),
  };
}

/** Reject an ORDER BY property the Worker was not supposed to interpolate. */
function checkedSortKey(statement, pattern) {
  const match = pattern.exec(statement);
  if (!match) throw stubError(`could not find a sort key in: ${statement.slice(0, 160)}`);
  if (!STUB_SORTABLE.has(match[1])) {
    throw stubError(`refusing to ORDER BY an unexpected property '${match[1]}' — the Worker must only interpolate allowlisted names`);
  }
  return match[1];
}

/**
 * The direction of the *primary* sort key. Every table query ends with a
 * deterministic tiebreak (`id(r) ASC` / `canonical_key ASC`), so scanning the
 * whole ORDER BY clause for the word ASC would report ascending for a descending
 * query — the stub has to read the first term only.
 */
function sortDirection(statement) {
  const match = /ORDER BY\s+coalesce\([^()]*\)\s*(ASC|DESC)/i.exec(statement);
  return match && match[1].toUpperCase() === "ASC" ? "asc" : "desc";
}

function adjacency(direction) {
  const map = new Map();
  const push = (from, edge, other, outgoing) => {
    if (!map.has(from)) map.set(from, []);
    map.get(from).push({ edge, other, outgoing });
  };
  FX.edges.forEach((edge) => {
    if (direction !== "incoming") push(edge.source, edge, edge.target, true);
    if (direction !== "outgoing") push(edge.target, edge, edge.source, false);
  });
  return map;
}

function passesFilters(edge, parameters) {
  if (Array.isArray(parameters.types) && parameters.types.length && parameters.types.indexOf(edge.type) < 0) return false;
  if (Number(parameters.minWeight) > 0 && Number(edge.weight || 0) < Number(parameters.minWeight)) return false;
  return true;
}

function chainEntry(node) {
  return {
    key: node.key, name: node.name, entity_type: node.entity_type, labels: node.labels,
    jurisdiction: node.jurisdiction, cluster_id: node.cluster_id, anomaly_score: node.anomaly_score,
    betweenness: node.betweenness, confidence: node.confidence, mention_count: node.mention_count,
    risk_score: node.risk_score,
  };
}

function degreeOf(key) {
  return FX.edges.filter((edge) => edge.source === key || edge.target === key).length;
}

/** BFS over the fixture, honouring the direction the statement asked for. */
function shortestPath(from, to, direction, maxHops) {
  if (!FX_BY_KEY.has(from) || !FX_BY_KEY.has(to)) return null;
  const graph = adjacency(direction);
  const queue = [{ key: from, nodes: [from], edges: [] }];
  const visited = new Set([from]);
  while (queue.length) {
    const step = queue.shift();
    if (step.nodes.length - 1 >= maxHops) continue;
    for (const link of graph.get(step.key) || []) {
      if (visited.has(link.other)) continue;
      const nodes = step.nodes.concat([link.other]);
      const edges = step.edges.concat([link]);
      if (link.other === to) return { nodes, edges };
      visited.add(link.other);
      queue.push({ key: link.other, nodes, edges });
    }
  }
  return null;
}

/** Bounded simple-path enumeration, mirroring `MATCH p = (a)-[*1..n]-(b)`. */
function enumeratePaths(from, to, direction, maxHops, cap) {
  const graph = adjacency(direction);
  const found = [];
  const walk = (key, nodes, links) => {
    if (found.length >= cap) return;
    if (nodes.length - 1 >= maxHops) return;
    for (const link of graph.get(key) || []) {
      if (nodes.indexOf(link.other) >= 0) continue;
      const nextNodes = nodes.concat([link.other]);
      const nextLinks = links.concat([link]);
      if (link.other === to) {
        found.push({ nodes: nextNodes, edges: nextLinks });
        continue;
      }
      walk(link.other, nextNodes, nextLinks);
    }
  };
  walk(from, [from], []);
  return found;
}

/** Level-by-level BFS matching the generated neighbourhood statement. */
function neighborhood(rootKey, depth, parameters, limit) {
  if (!FX_BY_KEY.has(rootKey)) return null;
  const graph = adjacency("undirected");
  let seen = [FX_BY_KEY.get(rootKey)];
  let frontier = [FX_BY_KEY.get(rootKey)];
  for (let level = 0; level < depth; level += 1) {
    const candidates = [];
    frontier.forEach((node) => {
      (graph.get(node.key) || []).forEach((link) => {
        if (!passesFilters(link.edge, parameters)) return;
        // adjacency() stores the far side as a canonical key; the walk works in
        // node objects, so resolve it (an unknown key would be a fixture bug).
        const other = FX_BY_KEY.get(link.other);
        if (other) candidates.push(other);
      });
    });
    const seenKeys = new Set(seen.map((node) => node.key));
    const fresh = [];
    candidates.forEach((node) => {
      if (seenKeys.has(node.key)) return;
      seenKeys.add(node.key);
      fresh.push(node);
    });
    seen = seen.concat(fresh.slice(0, limit));
    frontier = fresh.slice(0, limit);
  }
  return seen
    .slice()
    .sort((a, b) => (a.key < b.key ? -1 : 1))
    .slice(0, limit);
}

function relevanceOf(node, q, lower) {
  const name = String(node.name || "").toLowerCase();
  if (node.key === q) return 100;
  const identifiers = [node.reg_number, node.company_number, node.lei, node.imo, node.mmsi,
    node.tail_number, node.transponder, node.icao24, node.wikidata_id, node.wikipedia_id,
    node.opencorporates_url];
  if (identifiers.some((value) => value && String(value) === q)) return 95;
  if (name === lower) return 90;
  if (name.startsWith(lower)) return 70;
  if (name.includes(lower)) return 50;
  if ((node.aliases || []).some((alias) => String(alias).toLowerCase().includes(lower))) return 40;
  if (String(node.jurisdiction || "").toLowerCase() === lower) return 12;
  return 0;
}

function labelMatches(node, parameters) {
  const labels = parameters.labels;
  if (!Array.isArray(labels) || !labels.length) return true;
  return labels.some((label) => node.labels.indexOf(label) >= 0);
}

/* ---- statement dispatcher ------------------------------------------------ */

function runStatement(statement, parameters = {}) {
  const columns = returnColumns(statement);

  if (statement.includes("CALL db.indexes()")) {
    const list = [{ name: "entity_canonical_key", type: "RANGE", state: "ONLINE" }];
    if (!neo4jBehaviour.noFulltextIndex) list.unshift({ name: GRAPH_INDEX, type: "FULLTEXT", state: "ONLINE" });
    return rows(columns, list);
  }

  if (/RETURN count\(e\) AS nodes/.test(statement)) return rows(columns, [{ nodes: FX.nodes.length }]);
  if (/RETURN count\(r\) AS edges/.test(statement)) return rows(columns, [{ edges: FX.edges.length }]);
  if (statement.includes("max(e.metrics_at)")) return rows(columns, [{ metrics_at: "2026-09-27T04:30:00Z" }]);

  // ---- overview: the ranked node page ------------------------------------
  // Both the overview page and a paged node table project `WITH e, count(r) AS
  // degree` and end in `LIMIT $limit`; only the table pages with SKIP, and only
  // the overview is keyed off $limit alone. Test SKIP first so the two cannot
  // silently answer for each other.
  if (statement.includes("WITH e, count(r) AS degree") && statement.includes("LIMIT $limit")
      && !statement.includes("$key") && !statement.includes("SKIP $skip")) {
    const metric = checkedSortKey(statement, /ORDER BY coalesce\(e\.(\w+), 0\) DESC/);
    const limit = Math.max(0, Number(parameters.limit) || 0);
    const picked = FX.nodes
      .filter((node) => labelMatches(node, parameters))
      .slice()
      .sort((a, b) => (b[metric] || 0) - (a[metric] || 0) || (a.key < b.key ? -1 : 1))
      .slice(0, limit)
      .map((node) => Object.assign({}, node, { degree: degreeOf(node.key) }));
    overviewKeep = picked.map((node) => node.key);
    return rows(columns, picked);
  }

  // ---- overview: edges induced by that page -------------------------------
  if (statement.includes("UNWIND keep AS a")) {
    const metric = checkedSortKey(statement, /ORDER BY coalesce\(e\.(\w+), 0\) DESC/);
    const limit = Math.max(0, Number(parameters.limit) || 0);
    const keep = new Set(
      FX.nodes
        .filter((node) => labelMatches(node, parameters))
        .slice()
        .sort((a, b) => (b[metric] || 0) - (a[metric] || 0) || (a.key < b.key ? -1 : 1))
        .slice(0, limit)
        .map((node) => node.key)
    );
    const edges = FX.edges.filter((edge) => keep.has(edge.source) && keep.has(edge.target));
    return rows(columns, edges.map((edge) => ({ edge })));
  }

  // ---- search: full-text index -------------------------------------------
  if (statement.includes("db.index.fulltext.queryNodes")) {
    if (neo4jBehaviour.noFulltextIndex) {
      throw stubError(`There is no such index: ${GRAPH_INDEX}`, "Neo.ClientError.Procedure.CallFailed");
    }
    const tokens = String(parameters.ftq || "").split(/\s+OR\s+/).map((token) => token.replace(/\*/g, "")).filter(Boolean);
    const limit = Math.max(0, Number(parameters.limit) || 0);
    const hits = FX.nodes
      .filter((node) => labelMatches(node, parameters))
      .map((node, index) => {
        const haystack = `${node.name} ${(node.aliases || []).join(" ")}`.toLowerCase();
        const matched = tokens.some((token) => haystack.includes(token));
        return matched ? Object.assign({}, node, { degree: degreeOf(node.key), match_score: Math.max(0.1, 2 - index * 0.25) }) : null;
      })
      .filter(Boolean)
      .sort((a, b) => b.match_score - a.match_score)
      .slice(0, limit);
    return rows(columns, hits);
  }

  // ---- search: deterministic scan ----------------------------------------
  if (statement.includes("WHEN e.canonical_key = $q THEN 100")) {
    const limit = Math.max(0, Number(parameters.limit) || 0);
    const q = String(parameters.q || "");
    const lower = String(parameters.lower || "");
    const hits = FX.nodes
      .filter((node) => labelMatches(node, parameters))
      .map((node) => ({ node, relevance: relevanceOf(node, q, lower) }))
      .filter((entry) => entry.relevance > 0)
      .sort((a, b) => b.relevance - a.relevance || degreeOf(b.node.key) - degreeOf(a.node.key))
      .slice(0, limit)
      .map((entry) => Object.assign({}, entry.node, { degree: degreeOf(entry.node.key), match_score: entry.relevance }));
    return rows(columns, hits);
  }

  // ---- single node --------------------------------------------------------
  if (statement.includes("MATCH (e:Entity {canonical_key: $key})") && statement.includes("WITH e, count(r) AS degree")) {
    const node = FX_BY_KEY.get(parameters.key);
    if (!node) return rows(columns, []);
    return rows(columns, [Object.assign({}, node, { degree: degreeOf(node.key) })]);
  }

  // ---- incident edges of a node -------------------------------------------
  if (statement.includes(")-[r]-(o:Entity)") && statement.includes("WITH e, r, o LIMIT")) {
    const edges = FX.edges.filter((edge) => edge.source === parameters.key || edge.target === parameters.key);
    return rows(columns, edges.slice(0, Number(parameters.edgeLimit) || edges.length).map((edge) => ({ edge })));
  }

  // ---- neighbour nodes of a node ------------------------------------------
  if (statement.includes("WITH DISTINCT o LIMIT $neighbourLimit")) {
    const neighbours = FX.edges
      .filter((edge) => edge.source === parameters.key || edge.target === parameters.key)
      .map((edge) => FX_BY_KEY.get(edge.source === parameters.key ? edge.target : edge.source))
      .filter(Boolean)
      .slice(0, Number(parameters.neighbourLimit) || 100)
      .map((node) => Object.assign({}, node, { degree: degreeOf(node.key) }));
    return rows(columns, neighbours);
  }

  // ---- citations ----------------------------------------------------------
  // The paged source table also matches Document-[m:MENTIONS]->Entity (to count
  // entities per document), so this branch has to require the keyed lookup that
  // only the inspector's citation query has — otherwise a table query would be
  // answered with citations and come back empty.
  if (statement.includes("[m:MENTIONS]") && statement.includes("(e:Entity {canonical_key: $key})")) {
    const node = FX_BY_KEY.get(parameters.key);
    if (!node) return rows(columns, []);
    const docs = FX.docs.map((doc) => Object.assign({}, doc, {
      count: doc.count, confidence: doc.confidence, surface_forms: doc.surface_forms, first_offset: 120,
    }));
    return rows(columns, docs.slice(0, Number(parameters.citationLimit) || docs.length));
  }

  // ---- generated neighbourhood walk ---------------------------------------
  if (statement.includes("WITH [root] AS seen0")) {
    let depth = 0;
    const levelPattern = /f(\d+) IS NOT NULL/g;
    let match = levelPattern.exec(statement);
    while (match) {
      depth = Math.max(depth, Number(match[1]) + 1);
      match = levelPattern.exec(statement);
    }
    if (depth === 0) throw stubError("neighbourhood statement has no expansion levels");
    const keep = neighborhood(parameters.key, depth, parameters, Number(parameters.limit) || 0);
    if (!keep) return rows(columns, []);
    const keepKeys = new Set(keep.map((node) => node.key));
    return rows(columns, keep.map((node) => ({
      ...node,
      degree: degreeOf(node.key),
      rels: FX.edges.filter((edge) => keepKeys.has(edge.source) && keepKeys.has(edge.target)),
    })));
  }

  // ---- shortest path ------------------------------------------------------
  if (statement.includes("shortestPath")) {
    const direction = statement.includes("(a)<-[*") ? "incoming" : statement.includes("]->(b)") ? "outgoing" : "undirected";
    const maxHops = Number(/\[\*1\.\.(\d+)\]/.exec(statement)[1]);
    const found = shortestPath(parameters.from, parameters.to, direction, maxHops);
    if (!found) return rows(columns, []);
    return rows(columns, [{
      chain: found.nodes.map((key) => chainEntry(FX_BY_KEY.get(key))),
      rels: found.edges.map((link) => link.edge),
      hops: found.edges.length,
    }]);
  }

  // ---- bounded path enumeration (alternatives / weighted cost) ------------
  if (statement.includes("LIMIT $enumCap")) {
    const direction = statement.includes("(a)<-[*") ? "incoming" : statement.includes("]->(b)") ? "outgoing" : "undirected";
    const maxHops = Number(/\[\*1\.\.(\d+)\]/.exec(statement)[1]);
    const weighted = statement.includes("reduce(c = 0.0");
    const useConfidence = statement.includes("r.confidence");
    const paths = enumeratePaths(parameters.from, parameters.to, direction, maxHops, Number(parameters.enumCap) || 100);
    const scored = paths.map((path) => {
      const cost = weighted
        ? path.edges.reduce((sum, link) => sum + (1.05 - Number(useConfidence ? link.edge.confidence : link.edge.weight) || 0), 0)
        : path.edges.length;
      return {
        chain: path.nodes.map((key) => chainEntry(FX_BY_KEY.get(key))),
        rels: path.edges.map((link) => link.edge),
        hops: path.edges.length,
        cost,
      };
    }).sort((a, b) => a.cost - b.cost || a.hops - b.hops)
      .slice(0, Number(parameters.alts) || 4);
    return rows(columns, scored);
  }

  // ---- table: edges -------------------------------------------------------
  if (statement.includes("MATCH ()-[r]->()") && statement.includes("RETURN count(r) AS total")) {
    const total = FX.edges.filter((edge) => tableEdgeMatches(edge, parameters, statement)).length;
    return rows(columns, [{ total }]);
  }
  if (statement.includes("MATCH ()-[r]->()") && statement.includes("SKIP $skip")) {
    const sortKey = checkedSortKey(statement, /ORDER BY coalesce\(r\.(\w+)/);
    const direction = sortDirection(statement);
    const picked = FX.edges
      .filter((edge) => tableEdgeMatches(edge, parameters, statement))
      .slice()
      .sort((a, b) => (direction === "asc" ? (a[sortKey] || 0) - (b[sortKey] || 0) : (b[sortKey] || 0) - (a[sortKey] || 0)) || a.id - b.id)
      .slice(Number(parameters.skip) || 0, (Number(parameters.skip) || 0) + (Number(parameters.limit) || 0));
    return rows(columns, picked.map((edge) => ({ edge })));
  }

  // ---- table: sources -----------------------------------------------------
  if (statement.includes("MATCH (d:Document)") && statement.includes("RETURN count(d) AS total")) {
    return rows(columns, [{ total: FX.docs.filter((doc) => tableDocMatches(doc, parameters)).length }]);
  }
  if (statement.includes("MATCH (d:Document)") && statement.includes("SKIP $skip")) {
    const picked = FX.docs
      .filter((doc) => tableDocMatches(doc, parameters))
      .slice(Number(parameters.skip) || 0, (Number(parameters.skip) || 0) + (Number(parameters.limit) || 0));
    return rows(columns, picked);
  }

  // ---- table: nodes -------------------------------------------------------
  if (statement.includes("MATCH (e:Entity)") && statement.includes("RETURN count(e) AS total")) {
    return rows(columns, [{ total: FX.nodes.filter((node) => tableNodeMatches(node, parameters)).length }]);
  }
  if (statement.includes("MATCH (e:Entity)") && statement.includes("SKIP $skip")) {
    const sortKey = checkedSortKey(statement, /ORDER BY coalesce\(e\.(\w+)/);
    const direction = sortDirection(statement);
    const picked = FX.nodes
      .filter((node) => tableNodeMatches(node, parameters))
      .slice()
      .sort((a, b) => {
        if (sortKey === "name") return direction === "asc" ? String(a.name).localeCompare(b.name) : String(b.name).localeCompare(a.name);
        return direction === "asc" ? (a[sortKey] || 0) - (b[sortKey] || 0) : (b[sortKey] || 0) - (a[sortKey] || 0);
      })
      .slice(Number(parameters.skip) || 0, (Number(parameters.skip) || 0) + (Number(parameters.limit) || 0))
      .map((node) => Object.assign({}, node, { degree: degreeOf(node.key) }));
    return rows(columns, picked);
  }

  throw stubError(`fake Neo4j has no handler for this statement: ${statement.slice(0, 200)}`);
}

function tableEdgeMatches(edge, parameters, statement) {
  if (Number(edge.weight || 0) < Number(parameters.minWeight || 0)) return false;
  if (Number(edge.confidence || 0) < Number(parameters.minConfidence || 0)) return false;
  if (Array.isArray(parameters.types) && parameters.types.length && parameters.types.indexOf(edge.type) < 0) return false;
  if (parameters.q && statement.includes("CONTAINS $q")) {
    const q = String(parameters.q).toLowerCase();
    const source = FX_BY_KEY.get(edge.source);
    const target = FX_BY_KEY.get(edge.target);
    const haystack = [edge.type, source && source.name, target && target.name, edge.source_id]
      .filter(Boolean).join(" ").toLowerCase();
    if (!haystack.includes(q)) return false;
  }
  return true;
}

function tableDocMatches(doc, parameters) {
  if (!parameters.q) return true;
  const q = String(parameters.q).toLowerCase();
  return [doc.title, doc.source_id, doc.doc_id].filter(Boolean).join(" ").toLowerCase().includes(q);
}

function tableNodeMatches(node, parameters) {
  if (!labelMatches(node, parameters)) return false;
  if (Number(node.confidence || 0) < Number(parameters.minConfidence || 0)) return false;
  if (parameters.q && String(parameters.q)) {
    const q = String(parameters.q).toLowerCase();
    const haystack = [node.name, node.key, (node.aliases || []).join(" "), node.reg_number, node.imo, node.mmsi,
      node.tail_number, node.transponder, node.lei].filter(Boolean).join(" ").toLowerCase();
    if (!haystack.includes(q)) return false;
  }
  return true;
}

/** Remembers the node keys the last overview page returned (for assertions). */
let overviewKeep = [];

export {
  GRAPH_INDEX,
  STUB_SORTABLE,
  FX,
  FX_BY_KEY,
  P_KASTELION,
  O_MERIDIAN,
  O_SARNEN,
  F_TALLOW,
  L_LIMASSOL,
  A_TAIL,
  P_UNKNOWN,
  neo4jCalls,
  neo4jBehaviour,
  runStatement,
  fakeNeo4jTxResponse,
  stubError,
  returnColumns,
  resetFakeNeo4j,
};

/**
 * Build Neo4j's tx/commit reply for a batch of statements, honouring every
 * behaviour knob. Returns plain data rather than a `Response` so each suite can
 * construct it in its own realm; the shape is `{status, contentType}` plus either
 * `json` or `text`.
 *
 * Both smoke suites inject upstream failures through this, so "the console
 * survives a 401" and "the Worker survives a 401" are the same 401.
 */
function fakeNeo4jTxResponse(statements) {
  if (neo4jBehaviour.rawText !== null) {
    return { status: 200, contentType: "text/html", text: neo4jBehaviour.rawText };
  }
  if (neo4jBehaviour.unauthorized) {
    return {
      status: 401,
      contentType: "application/json",
      json: {
        results: [],
        errors: [{
          code: "Neo.ClientError.Security.Unauthorized",
          message: "The client is unauthorized due to authentication failure.",
        }],
      },
    };
  }
  if (neo4jBehaviour.error) {
    return {
      status: 400,
      contentType: "application/json",
      json: {
        results: [],
        errors: [{
          code: neo4jBehaviour.error.code || "Neo.ClientError.Statement.SyntaxError",
          message: neo4jBehaviour.error.message || "boom",
        }],
      },
    };
  }
  try {
    const results = (statements || []).map((item) => runStatement(item.statement, item.parameters || {}));
    return { status: 200, contentType: "application/json", json: { results, errors: [] } };
  } catch (error) {
    return {
      status: 400,
      contentType: "application/json",
      json: {
        results: [],
        errors: [{ code: error.code || "Neo.ClientError.Statement.SyntaxError", message: error.message }],
      },
    };
  }
}

/** Clear captured calls and every behaviour override between checks. */
function resetFakeNeo4j() {
  neo4jCalls.length = 0;
  neo4jBehaviour.unauthorized = false;
  neo4jBehaviour.error = null;
  neo4jBehaviour.rawText = null;
  neo4jBehaviour.noFulltextIndex = false;
}
