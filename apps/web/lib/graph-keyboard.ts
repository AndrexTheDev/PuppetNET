/**
 * Keyboard model for the WebGL graph.
 *
 * The canvas has no DOM nodes to focus, so it behaves as a single composite
 * widget: one tab stop, arrow keys move a virtual cursor, Enter activates the
 * cursor and Escape clears it. Keeping the mapping pure makes it testable
 * without a browser (WCAG 2.1.1 — everything operable by mouse is operable by
 * keyboard, with no keyboard trap).
 */

export interface KeyboardNode {
  id: string;
  label: string;
  type: string;
  confidence: number;
}

export type GraphKeyAction =
  | { type: 'move'; delta: number }
  | { type: 'first' }
  | { type: 'last' }
  | { type: 'select' }
  | { type: 'clear' }
  | { type: 'zoom'; factor: number }
  | { type: 'reset' }
  | { type: 'none' };

export function graphKeyAction(key: string): GraphKeyAction {
  switch (key) {
    case 'ArrowRight':
    case 'ArrowDown':
      return { type: 'move', delta: 1 };
    case 'ArrowLeft':
    case 'ArrowUp':
      return { type: 'move', delta: -1 };
    case 'Home':
      return { type: 'first' };
    case 'End':
      return { type: 'last' };
    case 'Enter':
    case ' ':
    case 'Spacebar':
      return { type: 'select' };
    case 'Escape':
    case 'Esc':
      return { type: 'clear' };
    case '+':
    case '=':
      return { type: 'zoom', factor: 0.8 };
    case '-':
    case '_':
      return { type: 'zoom', factor: 1.25 };
    case '0':
      return { type: 'reset' };
    default:
      return { type: 'none' };
  }
}

/** Wrap-around stepping, so Home/End and the arrows never dead-end. */
export function nextIndex(current: number, delta: number, length: number): number {
  if (length <= 0) return 0;
  const safe = Number.isFinite(current) ? Math.trunc(current) : 0;
  return (((safe + delta) % length) + length) % length;
}

export function describeGraphNode(node: KeyboardNode, index: number, total: number): string {
  const position = `Knoten ${index + 1} von ${total}`;
  const confidence = Math.round(node.confidence * 100);
  const kind = node.type === 'Organization' ? 'Organisation' : node.type;
  return `${position}: ${node.label}. Typ ${kind}. Confidence ${confidence} Prozent. Eingabe öffnet die Details.`;
}

export function graphInstructions(): string {
  return (
    'Interaktiver Netzwerkgraph. Pfeiltasten bewegen den Fokus zwischen Knoten, '
    + 'Eingabe oder Leertaste öffnet die Details, Escape schließt sie. '
    + 'Plus und Minus zoomen, Null setzt die Ansicht zurück. '
    + 'Alle Knoten stehen zusätzlich in der Tabellenansicht.'
  );
}
