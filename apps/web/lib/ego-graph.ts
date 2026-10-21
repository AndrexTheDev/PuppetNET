/**
 * Wave 3: static ego-graph SVG for entity profiles — no JavaScript at runtime.
 *
 * A pure, deterministic function of the published entity record: neighbours
 * are laid out evenly on a circle (position carries no meaning beyond
 * adjacency), solid lines are outgoing and dashed lines incoming relations,
 * and every neighbour links to its own profile. All interpolated text is
 * XML-escaped, so a hostile entity name stays text. The same entity always
 * renders byte-identical markup, which keeps the static export stable and the
 * pages diffable between revisions.
 */
import { ENTITY_TYPE_LABELS, entityPath, type EntityKind, type PublicEntity } from './entity-seo';

export const EGO_WIDTH = 720;
export const EGO_HEIGHT = 470;
const CENTER_RADIUS = 24;
const NEIGHBOR_RADIUS = 13;
const ORBIT = 165;
/** Bright-on-dark palette; contrast >= 3:1 for graphics against --color-forest-950. */
const TYPE_FILL: Record<EntityKind, string> = {
  Person: '#79b473',
  Organization: '#e0a458',
  Location: '#6fa8dc',
  Craft: '#c58fbf',
  Document: '#9aa5a0',
  Other: '#9aa5a0',
};
const CENTER_FILL = '#f2c14e';
const LINE_COLOR = '#a8b8b0';
const TEXT_COLOR = '#e8f0ec';

export function escapeXml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function truncate(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, max - 1).trimEnd()}…` : value;
}

export interface EgoNeighbor {
  id: string;
  slug: string;
  name: string;
  type: EntityKind;
  /** Unique directions of the merged relationships, outgoing first. */
  directions: ('out' | 'in')[];
  predicates: string[];
  x: number;
  y: number;
}

/** Merge multi-relationship neighbours, then place them evenly on a circle
 *  starting at 12 o'clock clockwise. Input order (related[] is already sorted
 *  and bounded) decides the angle — no randomness, no clock. */
export function egoLayout(entity: PublicEntity): EgoNeighbor[] {
  const byId = new Map<string, typeof entity.related>();
  for (const related of entity.related) {
    const bucket = byId.get(related.id);
    if (bucket) bucket.push(related); else byId.set(related.id, [related]);
  }
  const count = byId.size;
  if (!count) return [];
  const cx = EGO_WIDTH / 2;
  const cy = EGO_HEIGHT / 2;
  return [...byId.values()].map((links, index) => {
    const angle = -Math.PI / 2 + (2 * Math.PI * index) / count;
    const directions = links.map((link) => link.direction);
    return {
      id: links[0].id,
      slug: links[0].slug,
      name: links[0].name,
      type: links[0].type,
      directions: [...new Set(['out' as const, 'in' as const].filter((d) => directions.includes(d)))],
      predicates: [...new Set(links.map((link) => link.predicate))],
      x: cx + Math.cos(angle) * ORBIT,
      y: cy + Math.sin(angle) * ORBIT,
    };
  });
}

/** Render the ego graph as an inline-SVG string, or null when the entity has
 *  no published neighbour to show. */
export function buildEgoGraphSvg(entity: PublicEntity): string | null {
  const neighbors = egoLayout(entity);
  if (!neighbors.length) return null;
  const cx = EGO_WIDTH / 2;
  const cy = EGO_HEIGHT / 2;
  const titleId = `ego-title-${entity.slug}`;
  const descId = `ego-desc-${entity.slug}`;
  const parts: string[] = [];
  parts.push(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${EGO_WIDTH} ${EGO_HEIGHT}" role="img" aria-labelledby="${titleId} ${descId}" class="ego-graph">`,
  );
  parts.push(
    `<title id="${titleId}">Ego-Graph: ${escapeXml(entity.name)} mit ${neighbors.length} direkten Verbindungen</title>`,
  );
  parts.push(
    `<desc id="${descId}">Statisches Diagramm ohne JavaScript. ${escapeXml(entity.name)} in der Mitte, direkte Verbindungen gleichmäßig im Uhrzeigersinn: ${escapeXml(neighbors.map((n) => n.name).join(', '))}. Durchgezogene Linien sind ausgehende, gestrichelte eingehende Beziehungen. Die Position auf dem Kreis hat keine Bedeutung.</desc>`,
  );
  for (const node of neighbors) {
    const dx = node.x - cx;
    const dy = node.y - cy;
    const length = Math.hypot(dx, dy) || 1;
    node.directions.forEach((direction, index) => {
      // Bidirectional pairs get two offset parallel lines instead of one
      // ambiguous stroke; single-direction lines stay centred.
      const offset = node.directions.length > 1 ? (index === 0 ? -4 : 4) : 0;
      const px = (-dy / length) * offset;
      const py = (dx / length) * offset;
      const dash = direction === 'in' ? ' stroke-dasharray="5 4"' : '';
      parts.push(
        `<line class="ego-edge ego-edge-${direction}" x1="${(cx + px).toFixed(1)}" y1="${(cy + py).toFixed(1)}" x2="${(node.x + px).toFixed(1)}" y2="${(node.y + py).toFixed(1)}" stroke="${LINE_COLOR}" stroke-width="1.6"${dash} />`,
      );
    });
    const mx = (cx + node.x) / 2;
    const my = (cy + node.y) / 2;
    parts.push(
      `<text class="ego-edge-label" x="${mx.toFixed(1)}" y="${(my - 6).toFixed(1)}" text-anchor="middle" font-size="9" fill="${LINE_COLOR}">${escapeXml(truncate(node.predicates.join(', '), 24))}</text>`,
    );
  }
  for (const node of neighbors) {
    const anchor = Math.abs(node.x - cx) < 30 ? 'middle' : node.x > cx ? 'start' : 'end';
    const lx = node.x + (anchor === 'start' ? NEIGHBOR_RADIUS + 6 : anchor === 'end' ? -(NEIGHBOR_RADIUS + 6) : 0);
    const ly = anchor === 'middle' ? (node.y > cy ? node.y + NEIGHBOR_RADIUS + 17 : node.y - NEIGHBOR_RADIUS - 9) : node.y + 4;
    parts.push(
      `<a href="${escapeXml(entityPath(node.slug))}" class="ego-link"><circle class="ego-node" cx="${node.x.toFixed(1)}" cy="${node.y.toFixed(1)}" r="${NEIGHBOR_RADIUS}" fill="${TYPE_FILL[node.type] ?? TYPE_FILL.Other}" stroke="#0b1512" stroke-width="1.5" /><text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" text-anchor="${anchor}" font-size="11" fill="${TEXT_COLOR}">${escapeXml(truncate(node.name, 20))}</text><title>${escapeXml(node.name)} — ${escapeXml(ENTITY_TYPE_LABELS[node.type] ?? node.type)}</title></a>`,
    );
  }
  parts.push(
    `<circle class="ego-center" cx="${cx}" cy="${cy}" r="${CENTER_RADIUS}" fill="${CENTER_FILL}" stroke="#0b1512" stroke-width="2" />`,
  );
  parts.push(
    `<text x="${cx}" y="${cy + CENTER_RADIUS + 19}" text-anchor="middle" font-size="13" font-weight="700" fill="${TEXT_COLOR}">${escapeXml(truncate(entity.name, 26))}</text>`,
  );
  parts.push(
    `<g class="ego-legend" font-size="10" fill="${LINE_COLOR}"><line x1="16" y1="${EGO_HEIGHT - 16}" x2="44" y2="${EGO_HEIGHT - 16}" stroke="${LINE_COLOR}" stroke-width="1.6" /><text x="50" y="${EGO_HEIGHT - 13}">ausgehend</text><line x1="122" y1="${EGO_HEIGHT - 16}" x2="150" y2="${EGO_HEIGHT - 16}" stroke="${LINE_COLOR}" stroke-width="1.6" stroke-dasharray="5 4" /><text x="156" y="${EGO_HEIGHT - 13}">eingehend</text></g>`,
  );
  parts.push('</svg>');
  return parts.join('');
}
