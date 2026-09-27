#!/usr/bin/env python3
"""PuppetNET Telegram alert engine — cluster-bridge push and daily anomaly digest.

The maintenance pass (:mod:`graph_analytics`) produces a JSON report describing
what changed in the graph. This module turns that report into operator-facing
Telegram messages, and nothing else: it never touches Neo4j, never re-scores
nodes and never decides what is anomalous. The split is deliberate — the alert
layer has to keep working while the database is down, rate-limited or being
pruned, and it has to be replayable against a report that already exists.

Two message classes, matching the operating model:

1. **Bridge alerts (near real time).** Sent when a node that first appeared
   inside the alert window connects two clusters that had no path between them
   before. That is the single highest-value signal in an influence graph: a new
   intermediary that makes two previously isolated networks reachable from one
   another. Each alert carries a suppression key so a re-run of the same report,
   or a node that keeps its bridge status for days, cannot spam the channel.

2. **Daily digest.** The top-N anomaly-scored nodes from the last 24 hours,
   where the score is ``w1·betweenness + w2·degree_spike + w3·offshore_ratio``.
   At most one digest per chat per UTC day, unless forced.

Security notes (this module handles a bearer-equivalent secret):

* The bot token travels in the URL path — that is the only authentication the
  Bot API offers. A :class:`TokenRedactor` logging filter is installed for the
  lifetime of the process so the token cannot reach stdout, GitHub Actions logs
  or a crash report even when a *library* logs the URL it gave up on.
* Calls are made with ``allow_worker=False``: routing ``api.telegram.org``
  through the edge relay would hand the token to a third party.
* ``mode="bot"`` is used, i.e. the honest bot user agent. Impersonating a
  browser to an API that we authenticate to would be both pointless and a lie.
* Nothing containing the token is written to ``.state`` or ``reports``.

Usage::

    python telegram_bot.py --test                     # verify token + chats
    python telegram_bot.py --report reports/graph_maintenance_<run>.json --all
    python telegram_bot.py --all --dry-run             # print, never send
    python telegram_bot.py --digest --force            # ignore the daily guard
    python telegram_bot.py --all --json                # machine-readable summary

Exit codes: ``0`` delivered (or nothing to deliver), ``1`` bad arguments,
``2`` at least one delivery failed, ``3`` configuration or report problem.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import html
import json
import logging
import os
import platform
import re
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# --- bootstrap: allow `python telegram_bot.py` from a clean checkout -------- #
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from puppetnet.config import load_settings  # noqa: E402
from puppetnet.logging_utils import (  # noqa: E402
    banner,
    configure_logging,
    get_logger,
    human_int,
)
from puppetnet.net.proxy_client import FetchClient, FetchResult  # noqa: E402

__version__ = "1.5.0"

logger = get_logger("telegram.bot")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_DELIVERY = 2
EXIT_CONFIG = 3

#: Bot API hard limit per message. Splitting is done on line boundaries so no
#: chunk ever ends inside an HTML tag.
MAX_MESSAGE_CHARS = 4096
#: Reserved for the `` <i>(2/3)</i>`` continuation marker, which is appended
#: *after* packing. 24 characters covers up to 99/99 chunks with room to spare;
#: under-reserving produces a chunk one character over the API limit, and
#: Telegram rejects the message outright rather than truncating it.
CHUNK_SUFFIX_RESERVE = 24

DEFAULT_API_BASE = "https://api.telegram.org"
DEFAULT_PARSE_MODE = "HTML"
STATE_FILE = "telegram_alerts.json"
#: Destination used by a dry run when no chat id is configured. Nothing is sent,
#: so the value only has to be recognisable in the printed preview.
DRY_RUN_CHAT = "(dry-run)"
REPORT_GLOB = "graph_maintenance_*.json"

#: Telegram allows ~30 messages/second globally and ~20/minute per group. One
#: message per second with a small burst is far below both and keeps a channel
#: readable when a maintenance run surfaces a dozen bridges.
DEFAULT_RATE_PER_SEC = 1.0
DEFAULT_BURST = 5.0

#: A bridge alert is not repeated for this long, even if the node is still a
#: bridge in the next run (it usually is — that is the point of a bridge).
DEFAULT_SUPPRESSION_HOURS = 24.0
DEFAULT_BRIDGE_LIMIT = 10
DEFAULT_DIGEST_TOP_N = 5

#: Never sleep longer than this on a 429 `retry_after`; a hostile or broken
#: response must not stall a CI job for ten minutes.
MAX_RETRY_AFTER_SECONDS = 120.0
DEFAULT_SEND_RETRIES = 3

#: Telegram's HTML subset. Anything else (tables, lists, divs, spans) is rejected
#: with a 400, so the formatter is deliberately limited to these.
_ALLOWED_TAGS = ("b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "blockquote")

TOKEN_PATTERN = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def utc_now() -> datetime:
    """Timezone-aware UTC now — every timestamp in this module is UTC."""
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    """ISO-8601 with a ``Z`` suffix, matching the graph's timestamp format."""
    value = moment or utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp tolerantly; ``None`` when unusable."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def as_float(value: Any, default: float = 0.0) -> float:
    """Best-effort float — report values come from JSON and may be ``None``."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default  # NaN guard


def as_int(value: Any, default: int = 0) -> int:
    """Best-effort int."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def escape(text: Any) -> str:
    """HTML-escape for Telegram's HTML parse mode.

    Entity names come from open sources and routinely contain ``&``, ``<`` or
    quotes (``"Acme & Sons <holding>"``). Unescaped, Telegram answers with a 400
    and the alert is lost — which is the worst possible failure mode for a
    security signal.
    """
    return html.escape(str(text if text is not None else ""), quote=False)


def tag(text: Any, name: str = "b") -> str:
    """Wrap ``text`` in an allowed Telegram tag, escaping the content."""
    return f"<{name}>{escape(text)}</{name}>"


def bullet(label: str, value: Any, *, precision: int | None = None) -> str:
    """One ``• label: value`` line, skipping empty values."""
    if value is None or value == "" or value == [] or value == {}:
        return ""
    if isinstance(value, float) and precision is not None:
        rendered = f"{value:.{precision}f}"
    elif isinstance(value, bool):
        rendered = "yes" if value else "no"
    elif isinstance(value, (list, tuple)):
        rendered = ", ".join(escape(item) for item in value)
    else:
        # Always escaped, including values that *look* like markup. The previous
        # version passed anything matching `<…>` through unescaped ("pre-formatted
        # by the caller"): an entity name or an error message is not a caller, and
        # `<b>pwned</b>` from a hostile source would have arrived in the channel as
        # markup. Callers that want a tag build it with `tag()`.
        rendered = escape(value)
    return f"• {escape(label)}: {rendered}"


def compact(lines: Iterable[str]) -> str:
    """Join non-empty lines."""
    return "\n".join(line for line in lines if line)


def chunk_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split ``text`` into Telegram-sized chunks without breaking markup.

    Packing happens on line boundaries first (the formatter emits one fact per
    line, so this always yields coherent chunks). A single oversized line is
    hard-split at a space, and the cut point is walked back if it would land
    inside a ``<tag>`` — a stray ``<`` makes Telegram reject the whole message.
    """
    text = (text or "").rstrip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    budget = max(64, limit - CHUNK_SUFFIX_RESERVE)
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        if current:
            chunks.append("\n".join(current).rstrip())
            current = []
            size = 0

    for line in text.split("\n"):
        line_length = len(line) + (1 if current else 0)
        if size + line_length <= budget:
            current.append(line)
            size += line_length
            continue
        flush()
        if len(line) <= budget:
            current.append(line)
            size = len(line)
            continue
        # Oversized single line: hard-split on spaces, never inside a tag.
        remainder = line
        while len(remainder) > budget:
            cut = remainder.rfind(" ", 0, budget)
            if cut <= 0:
                cut = budget
            while cut > 1 and _splits_inside_tag(remainder, cut):
                cut -= 1
            current.append(remainder[:cut].rstrip())
            flush()
            remainder = remainder[cut:].lstrip()
        if remainder:
            current.append(remainder)
            size = len(remainder)
    flush()

    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"{part} <i>({index}/{total})</i>" for index, part in enumerate(chunks, start=1)]
    return chunks


def _splits_inside_tag(text: str, cut: int) -> bool:
    """True when ``text[:cut]`` ends inside an unclosed ``<...>``."""
    opened = text.rfind("<", 0, cut)
    if opened < 0:
        return False
    closed = text.find(">", opened, cut)
    return closed < 0


def parse_chat_ids(raw: Any) -> list[str]:
    """Split a comma/space/newline separated chat-id list.

    Accepts numeric ids, ``@channelname`` handles and ``-100…`` supergroups.
    Empty entries are dropped; the order is preserved because the first chat is
    treated as the primary destination in ``--test``.
    """
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        parts = [str(item) for item in raw]
    else:
        parts = re.split(r"[,\s]+", str(raw))
    return [part.strip() for part in parts if part.strip()]


def redact_token(text: Any, token: str = "") -> str:
    """Replace a bot token with ``<REDACTED>`` in arbitrary text."""
    rendered = str(text or "")
    if token:
        rendered = rendered.replace(token, "<REDACTED>")
    return TOKEN_PATTERN.sub("<REDACTED>", rendered)


class TokenRedactor(logging.Filter):
    """Logging filter that scrubs the bot token out of every record.

    :class:`~puppetnet.net.proxy_client.FetchClient` logs the full URL when it
    gives up on a request (``giving up on %s after N attempts``). For Telegram
    the token *is* part of the URL, so without this filter a network outage
    would print the secret into the GitHub Actions log — permanently, because
    workflow logs are retained. The filter is attached to the root logger, which
    also covers records emitted by third-party libraries.
    """

    def __init__(self, token: str) -> None:
        super().__init__(name="telegram-token-redactor")
        self.token = str(token or "")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if self.token:
                record.msg = redact_token(record.msg, self.token)
                if record.args:
                    record.args = tuple(
                        redact_token(arg, self.token) if isinstance(arg, str) else arg for arg in record.args
                    )
            elif isinstance(record.msg, str):
                record.msg = TOKEN_PATTERN.sub("<REDACTED>", record.msg)
        except Exception:  # noqa: BLE001 - a redactor must never break logging
            return True
        return True


def install_token_redactor(token: str) -> TokenRedactor:
    """Attach a :class:`TokenRedactor` to the root logger *and its handlers*.

    Attaching to the logger alone is not enough, and getting this wrong is a
    credential leak: a logger's own filters run only for records logged directly
    to it. Records emitted by ``puppetnet.net.client`` propagate to the root
    without passing its filters — they are filtered only by the *handlers* they
    reach. So the redactor is installed on both, plus the ``puppetnet`` logger
    and its handlers, which is where :func:`configure_logging` puts them.
    """
    targets: list[logging.Logger | logging.Handler] = []
    for name in ("", "puppetnet"):
        log = logging.getLogger(name)
        targets.append(log)
        targets.extend(log.handlers)

    installed = [item for target in targets for item in target.filters if isinstance(item, TokenRedactor)]
    redactor = installed[0] if installed else TokenRedactor(token)
    redactor.token = str(token or "") or redactor.token
    for target in targets:
        if not any(item is redactor for item in target.filters):
            target.addFilter(redactor)
    return redactor


def uninstall_token_redactor(redactor: TokenRedactor | None) -> None:
    """Detach every redactor installed by :func:`install_token_redactor`."""
    if redactor is None:
        return
    for name in ("", "puppetnet"):
        log = logging.getLogger(name)
        for target in (log, *log.handlers):
            for item in list(target.filters):
                if isinstance(item, TokenRedactor):
                    with contextlib.suppress(ValueError):
                        target.removeFilter(item)


# --------------------------------------------------------------------------- #
# Report access
# --------------------------------------------------------------------------- #


def find_latest_report(report_dir: Path | str) -> Path | None:
    """Newest ``graph_maintenance_*.json`` in ``report_dir`` by mtime then name."""
    directory = Path(report_dir)
    if not directory.is_dir():
        return None
    candidates = [path for path in directory.glob(REPORT_GLOB) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime, path.name))


def load_report(path: Path | str) -> dict[str, Any]:
    """Load a maintenance report, tolerating a partially written file."""
    report_path = Path(path)
    if not report_path.is_file():
        raise FileNotFoundError(f"maintenance report not found: {report_path}")
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"maintenance report is not readable JSON: {report_path} ({exc})") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"maintenance report must be a JSON object: {report_path}")
    return payload


def report_age_hours(report: Mapping[str, Any], *, now: datetime | None = None) -> float:
    """Hours between the report's ``generated_at`` and ``now``."""
    generated = parse_iso(report.get("generated_at"))
    if generated is None:
        return float("inf")
    return max(0.0, ((now or utc_now()) - generated).total_seconds() / 3600.0)


def report_bridge_alerts(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Bridge alerts from a report, best-scored first, defensively typed."""
    raw = report.get("bridge_alerts")
    if not isinstance(raw, list):
        return []
    alerts = [item for item in raw if isinstance(item, Mapping)]
    alerts.sort(key=lambda item: -as_float(item.get("betweenness")))
    return [dict(item) for item in alerts]


def report_top_anomalies(report: Mapping[str, Any], limit: int = DEFAULT_DIGEST_TOP_N) -> list[dict[str, Any]]:
    """Top anomaly rows from a report (``top_anomalies`` or ``centrality.top``)."""
    raw = report.get("top_anomalies")
    if not isinstance(raw, list) or not raw:
        centrality = report.get("centrality")
        raw = centrality.get("top") if isinstance(centrality, Mapping) else None
    if not isinstance(raw, list):
        return []
    rows = [dict(item) for item in raw if isinstance(item, Mapping)]
    rows.sort(key=lambda item: -as_float(item.get("anomaly_score") or item.get("score")))
    return rows[: max(0, int(limit))]


# --------------------------------------------------------------------------- #
# Message formatting
# --------------------------------------------------------------------------- #


def _jurisdictions(alert: Mapping[str, Any]) -> str:
    """Human-readable jurisdiction list from an alert or anomaly row."""
    values = alert.get("jurisdictions") or alert.get("jurisdiction") or []
    if isinstance(values, str):
        values = [values]
    unique: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in unique:
            unique.append(text)
    return ", ".join(escape(item) for item in unique[:6])


def _clusters(row: Mapping[str, Any]) -> list[str]:
    """Cluster ids/names an alert bridges.

    ``graph_analytics`` emits ``bridged_clusters`` (stable ids) and
    ``bridged_cluster_names`` (id plus two member names, which is what a human
    wants to read). Older or hand-written reports may use ``clusters``.
    """
    for key in ("bridged_cluster_names", "bridged_clusters", "clusters"):
        values = row.get(key)
        if isinstance(values, str):
            values = [values]
        if values:
            return [str(item) for item in values if str(item)]
    return []


def _labels(row: Mapping[str, Any]) -> str:
    """Domain labels, dropping the base ``Entity`` label."""
    values = row.get("labels") or []
    if isinstance(values, str):
        values = [values]
    unique = [str(item) for item in values or [] if str(item) and str(item) != "Entity"]
    return ", ".join(escape(item) for item in unique[:6])


def _neighbours(row: Mapping[str, Any], limit: int = 5) -> list[str]:
    """Neighbour names (or keys) attached to an alert/anomaly row."""
    values = row.get("neighbours") or row.get("neighbors") or []
    if isinstance(values, str):
        values = [values]
    rendered: list[str] = []
    for value in values or []:
        if isinstance(value, Mapping):
            text = str(value.get("name") or value.get("canonical_key") or "")
        else:
            text = str(value or "")
        if text and text not in rendered:
            rendered.append(text)
        if len(rendered) >= limit:
            break
    return rendered


def format_bridge_alert(alert: Mapping[str, Any], *, rank: int = 1, total: int = 1) -> str:
    """HTML for one new-cluster-bridge alert.

    The message answers the three questions an analyst asks first: *what
    connected*, *to what*, and *how strong is the evidence*. Everything else
    (full metrics, provenance) lives in the report referenced at the bottom.
    """
    name = str(alert.get("name") or alert.get("canonical_key") or "unknown entity")
    clusters = _clusters(alert)
    header = "🚨 NEW CLUSTER BRIDGE"
    if total > 1:
        header += f" {rank}/{total}"

    lines = [
        tag(header),
        tag(name),
        "",
        bullet("Bridges clusters", ", ".join(escape(item) for item in clusters) or "—"),
        bullet("Labels", _labels(alert)),
        bullet("Jurisdictions", _jurisdictions(alert)),
        bullet("Betweenness", as_float(alert.get("betweenness")), precision=4),
        bullet("Degree", as_int(alert.get("degree"))),
        bullet("Anomaly score", as_float(alert.get("anomaly_score")), precision=4),
        bullet("Offshore cluster ratio", as_float(alert.get("offshore_cluster_ratio")), precision=3),
        bullet("Jurisdiction class", escape(str(alert.get("jurisdiction_class") or ""))),
        bullet("Articulation point", "yes" if alert.get("articulation_point") else ""),
        bullet("First seen", escape(str(alert.get("first_seen") or ""))),
        bullet("Neighbours", _neighbours(alert)),
    ]
    reasons = alert.get("reasons") or []
    if isinstance(reasons, Sequence) and not isinstance(reasons, str) and reasons:
        lines.append(bullet("Why", [str(item) for item in reasons[:3]]))
    key = str(alert.get("dedupe_key") or alert.get("canonical_key") or "")
    if key:
        lines.append("")
        lines.append(f"<code>{escape(key[:96])}</code>")
    return compact(lines)


def format_digest(
    report: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    top_n: int = DEFAULT_DIGEST_TOP_N,
    window_hours: float = 24.0,
) -> str:
    """HTML for the daily top-N anomaly digest."""
    run_id = str(report.get("run_id") or "unknown")
    generated = str(report.get("generated_at") or "")
    centrality = report.get("centrality") if isinstance(report.get("centrality"), Mapping) else {}
    weights = centrality.get("weights") if isinstance(centrality.get("weights"), Mapping) else {}

    lines = [
        tag(f"📊 DAILY ANOMALY DIGEST — top {top_n}"),
        f"<i>{escape(generated)}</i>",
        "",
        bullet("Run", escape(run_id)),
        bullet("Window", f"{int(window_hours)}h"),
        bullet("Nodes scored", human_int(as_int(centrality.get("nodes_scored")))),
        bullet("Clusters", human_int(as_int(centrality.get("clusters")))),
        bullet(
            "Weights",
            " / ".join(
                f"{escape(key)} {as_float(value):.2f}"
                for key, value in (("betweenness", weights.get("betweenness")),
                                   ("spike", weights.get("degree_spike")),
                                   ("offshore", weights.get("offshore_ratio")))
                if value is not None
            ),
        ),
        bullet("Bridges detected", human_int(len(report_bridge_alerts(report)))),
    ]

    if not rows:
        lines += ["", "<i>No nodes crossed the anomaly threshold in this window.</i>"]
        return compact(lines)

    for position, row in enumerate(rows, start=1):
        name = str(row.get("name") or row.get("canonical_key") or "unknown entity")
        score = as_float(row.get("anomaly_score") or row.get("score"))
        components = row.get("components") if isinstance(row.get("components"), Mapping) else {}
        lines += [
            "",
            f"{tag(f'{position}. {name}')} — <b>{score:.4f}</b>",
            bullet("Type", escape(str(row.get("entity_type") or ""))),
            bullet("Labels", _labels(row)),
            bullet("Jurisdictions", _jurisdictions(row)),
            bullet("Cluster", escape(str(row.get("cluster_id") or row.get("cluster") or ""))),
            bullet("Cluster size", as_int(row.get("cluster_size")) if row.get("cluster_size") else ""),
            bullet(
                "Betweenness",
                as_float(components.get("betweenness", row.get("betweenness"))),
                precision=4,
            ),
            bullet("24h degree spike", as_float(components.get("degree_spike")), precision=3),
            bullet(
                "Offshore cluster ratio",
                as_float(components.get("offshore_cluster_ratio", row.get("offshore_cluster_ratio"))),
                precision=3,
            ),
            bullet("Degree", as_int(row.get("degree"))),
            bullet("Neighbours", _neighbours(row)),
            bullet("Reasons", [str(item) for item in (row.get("reasons") or [])[:4]]),
        ]
    return compact(lines)


def format_status_report(outcome_report: Mapping[str, Any]) -> str:
    """Compact HTML summary of what this run sent (used by ``--test``)."""
    lines = [
        tag("✅ TELEGRAM ALERT ENGINE"),
        bullet("Bot", escape(str(outcome_report.get("bot_username") or ""))),
        bullet("Chats", [escape(str(item)) for item in (outcome_report.get("chats") or [])]),
        bullet("Bridge alerts", as_int(outcome_report.get("bridge_sent"))),
        bullet("Suppressed", as_int(outcome_report.get("bridge_suppressed"))),
        bullet("Digests", as_int(outcome_report.get("digest_sent"))),
        bullet("Failed", as_int(outcome_report.get("failures"))),
        bullet("Dry run", bool(outcome_report.get("dry_run"))),
    ]
    return compact(lines)


def format_failure_alert(
    message: str,
    *,
    workflow: str = "",
    run_url: str = "",
    exit_code: Any = None,
    source: str = "",
    when: datetime | None = None,
) -> str:
    """Compact warning for a *scheduled run* that failed.

    The alert passes below need a maintenance report to say anything; this one
    exists for the case where no report will ever arrive — the hourly harvest
    aborted, the credentials expired, the runner could not reach Neo4j. Without
    it a broken 24-hour schedule is invisible: the next run overwrites the log
    line nobody reads, and the first symptom is a graph that quietly stopped
    growing.
    """
    # `bullet()` escapes its value; passing a pre-escaped string here escaped it
    # twice and produced "&amp;lt;" in the channel.
    lines = [
        tag("⚠️ PuppetNET run failed"),
        bullet("What", message.strip()[:400]),
    ]
    if workflow:
        lines.append(bullet("Workflow", workflow))
    if source:
        lines.append(bullet("Source", source))
    if exit_code is not None and str(exit_code) != "":
        lines.append(bullet("Exit code", str(exit_code)))
    if run_url and str(run_url).startswith("http"):
        lines.append(bullet("Run", f'<a href="{escape(run_url)}">open the run log</a>'))
    lines.append(bullet("Time", iso(when)))
    return compact(lines)


def failure_key(
    message: str,
    *,
    workflow: str = "",
    source: str = "",
    exit_code: Any = None,
) -> str:
    """Suppression key for a failure alert.

    Deliberately *not* keyed on the wording: the same broken source reports a
    slightly different error on every run (a timeout for 30 s, then connection
    refused, then a read timeout), and the operator wants one alert per broken
    thing — one per 24 h window, see ``AlertLedger.suppression_hours`` — not one
    per sentence. Identity is ``workflow | source | exit code``; the message only
    decides the key when all three are empty, which happens on a hand-written
    dispatch.
    """
    identity = f"{workflow}|{source}|{exit_code if exit_code not in (None, '') else ''}"
    if identity == "||":
        identity = f"||{message.strip()[:200]}"
    digest = hashlib.sha1(identity.encode()).hexdigest()[:16]
    return f"failure:{digest}"


# --------------------------------------------------------------------------- #
# Ledger — suppression state
# --------------------------------------------------------------------------- #


@dataclass
class AlertLedger:
    """Persistent record of what was already sent, and when.

    Stored as ``.state/telegram_alerts.json``. Two independent guards live here:

    * ``sent`` maps a suppression key (``bridge:<canonical_key>:<clusters>``) to
      the ISO timestamp of its last delivery. A bridge stays a bridge for days;
      without this the channel would receive the same alert on every run.
    * ``digests`` maps a chat id to the UTC date of its last digest, giving the
      "one digest per day" contract even when the workflow runs hourly.

    Counters are cumulative across runs so an operator can see, at a glance,
    whether the engine is silently suppressing everything (a common
    misconfiguration: a stale state file left over from testing).
    """

    path: Path
    suppression_hours: float = DEFAULT_SUPPRESSION_HOURS
    data: dict[str, Any] = field(default_factory=dict)
    dirty: bool = False
    #: ``False`` in a dry run: marks are kept in memory so the run is internally
    #: consistent, but the file on disk is left alone. Persisting a dry run would
    #: silently suppress the first *real* delivery — the worst kind of bug, because
    #: the operator sees an engine that works and hears nothing.
    persist: bool = True

    # -- persistence ------------------------------------------------------ #
    @classmethod
    def load(
        cls,
        path: Path | str,
        *,
        suppression_hours: float = DEFAULT_SUPPRESSION_HOURS,
        persist: bool = True,
    ) -> AlertLedger:
        """Load the ledger, starting empty on a missing or corrupt file."""
        ledger_path = Path(path)
        data: dict[str, Any] = {}
        try:
            payload = json.loads(ledger_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                data = payload
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as exc:
            logger.warning("telegram ledger %s is unreadable (%s) — starting a fresh one", ledger_path, exc)
            data = {}
        data.setdefault("sent", {})
        data.setdefault("digests", {})
        data.setdefault("counters", {})
        if not isinstance(data["sent"], dict):
            data["sent"] = {}
        if not isinstance(data["digests"], dict):
            data["digests"] = {}
        if not isinstance(data["counters"], dict):
            data["counters"] = {}
        return cls(path=ledger_path, suppression_hours=suppression_hours, data=data, persist=persist)

    def save(self) -> bool:
        """Atomically persist the ledger; returns False when unwritable."""
        if not self.persist:
            if self.dirty:
                logger.info("dry run — telegram ledger %s left untouched", self.path)
            self.dirty = False
            return True
        if not self.dirty:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            payload = json.dumps(self.data, indent=2, sort_keys=True, ensure_ascii=False)
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            logger.warning("could not persist telegram ledger %s: %s", self.path, exc)
            return False
        self.dirty = False
        logger.debug("telegram ledger written → %s", self.path)
        return True

    # -- suppression ------------------------------------------------------ #
    def suppressed(self, key: str, *, now: datetime | None = None) -> bool:
        """True when ``key`` was delivered inside the suppression window."""
        if not key:
            return False
        moment = parse_iso(self.data["sent"].get(key))
        if moment is None:
            return False
        age_hours = ((now or utc_now()) - moment).total_seconds() / 3600.0
        return age_hours < self.suppression_hours

    def mark_sent(self, key: str, *, now: datetime | None = None) -> None:
        """Record delivery of ``key``."""
        if not key:
            return
        self.data["sent"][key] = iso(now)
        self.dirty = True

    def digest_sent_today(self, chat_id: str, *, now: datetime | None = None) -> bool:
        """True when ``chat_id`` already received a digest today (UTC)."""
        moment = parse_iso(self.data["digests"].get(str(chat_id)))
        if moment is None:
            return False
        return moment.date() == (now or utc_now()).date()

    def mark_digest(self, chat_id: str, *, now: datetime | None = None) -> None:
        """Record that ``chat_id`` received a digest today."""
        self.data["digests"][str(chat_id)] = iso(now)
        self.dirty = True

    def bump(self, counter: str, amount: int = 1) -> None:
        """Increment a cumulative counter."""
        counters = self.data["counters"]
        counters[counter] = as_int(counters.get(counter)) + amount
        self.dirty = True

    def prune(self, *, now: datetime | None = None, keep_hours: float | None = None) -> int:
        """Drop suppression entries older than the window; returns how many.

        Without this the ledger grows for the lifetime of the repository and, in
        a long-running deployment, every entity the graph ever contained ends up
        in it.
        """
        horizon = keep_hours if keep_hours is not None else max(self.suppression_hours * 7, 168.0)
        cutoff = (now or utc_now()) - timedelta(hours=horizon)
        stale = [key for key, value in self.data["sent"].items() if (parse_iso(value) or cutoff) < cutoff]
        for key in stale:
            del self.data["sent"][key]
        if stale:
            self.dirty = True
        return len(stale)

    def describe(self) -> dict[str, Any]:
        """JSON-safe view for reports and ``--json``."""
        return {
            "path": str(self.path),
            "suppression_hours": self.suppression_hours,
            "tracked_alerts": len(self.data.get("sent") or {}),
            "tracked_digests": len(self.data.get("digests") or {}),
            "persisted": self.persist,
            "counters": dict(self.data.get("counters") or {}),
        }


# --------------------------------------------------------------------------- #
# Sender
# --------------------------------------------------------------------------- #


class TelegramAuthError(RuntimeError):
    """Raised when the API rejects the token itself (401/403).

    Continuing to send after this would produce one failure per chat per
    message; the run stops instead and reports a configuration problem.
    """


@dataclass
class SendOutcome:
    """Result of one logical message (possibly several chunks)."""

    chat_id: str
    kind: str = "message"          # bridge | digest | test
    ok: bool = False
    dry_run: bool = False
    chunks: int = 0
    message_ids: list[int] = field(default_factory=list)
    status: int = 0
    error: str = ""
    retry_after_seconds: float = 0.0
    attempts: int = 0
    dedupe_key: str = ""
    preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TelegramSender:
    """Thin, honest Bot API client on top of :class:`FetchClient`.

    Responsibilities: build the URL, apply a per-host politeness bucket, chunk
    long messages, honour ``429 retry_after``, and turn every failure into a
    :class:`SendOutcome` instead of an exception — one unreachable chat must not
    stop alerts reaching the others.
    """

    def __init__(
        self,
        settings: Any,
        token: str,
        *,
        client: FetchClient | None = None,
        api_base: str = DEFAULT_API_BASE,
        parse_mode: str = DEFAULT_PARSE_MODE,
        rate_per_sec: float = DEFAULT_RATE_PER_SEC,
        burst: float = DEFAULT_BURST,
        retries: int = DEFAULT_SEND_RETRIES,
        dry_run: bool = False,
        sleeper: Callable[[float], None] = time.sleep,
        disable_link_preview: bool = True,
        preview_stream: Any = None,
    ) -> None:
        self.settings = settings
        self.token = str(token or "")
        self.api_base = str(api_base or DEFAULT_API_BASE).rstrip("/")
        self.parse_mode = str(parse_mode or DEFAULT_PARSE_MODE)
        self.retries = max(1, int(retries))
        self.dry_run = bool(dry_run)
        self._sleeper = sleeper
        self.disable_link_preview = bool(disable_link_preview)
        #: Where the dry-run preview goes. ``main`` points this at stderr when
        #: ``--json`` is set, so stdout stays a single parseable document.
        self.preview_stream = preview_stream if preview_stream is not None else sys.stdout
        self.owns_client = client is None
        self.client = client or FetchClient(settings)
        # ``rate_limit=`` on request() only reaches the relay task, so the local
        # politeness bucket has to be configured explicitly.
        with contextlib.suppress(Exception):
            self.client.delay_queue.set_host_policy(self.host, rate_per_sec=rate_per_sec, burst=burst)
        self.stats: dict[str, int] = {"calls": 0, "sent": 0, "failed": 0, "throttled": 0, "chunks": 0}

    # -- plumbing --------------------------------------------------------- #
    @property
    def host(self) -> str:
        """Hostname of the configured API base (for the delay queue)."""
        match = re.match(r"https?://([^/]+)", self.api_base)
        return match.group(1) if match else "api.telegram.org"

    def method_url(self, method: str) -> str:
        """Absolute URL for a Bot API method. Contains the token — never log it."""
        return f"{self.api_base}/bot{self.token}/{method}"

    def close(self) -> None:
        """Release the client when this sender created it."""
        if self.owns_client:
            with contextlib.suppress(Exception):
                self.client.close()

    def __enter__(self) -> TelegramSender:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # -- API -------------------------------------------------------------- #
    def call(self, method: str, payload: Mapping[str, Any] | None = None) -> FetchResult | None:
        """POST one Bot API method, retrying on 429 with the server's own delay.

        Returns the :class:`FetchResult`, or ``None`` in dry-run mode. Raises
        :class:`TelegramAuthError` on 401/403 because no amount of retrying fixes
        a bad token, and hammering the API with one risks a ban.
        """
        body = dict(payload or {})
        url = self.method_url(method)
        self.stats["calls"] += 1
        last: FetchResult | None = None

        for attempt in range(1, self.retries + 1):
            if self.dry_run:
                logger.info("[dry-run] POST %s %s", method, json.dumps(body, ensure_ascii=False)[:400])
                return None
            result = self.client.request(
                url,
                method="POST",
                json_body=body,
                mode="bot",
                allow_worker=False,          # never hand the token to the relay
                respect_robots=False,        # an authenticated API is not a crawl target
                source_id="telegram",
            )
            last = result
            if result.ok:
                return result

            retry_after = _retry_after(result)
            if result.status in (401, 403):
                raise TelegramAuthError(f"Telegram rejected the bot token (HTTP {result.status})")
            if result.status == 429 and attempt < self.retries:
                wait = min(retry_after or 5.0, MAX_RETRY_AFTER_SECONDS)
                self.stats["throttled"] += 1
                logger.warning("telegram %s throttled (429) — waiting %.1fs (attempt %d/%d)", method, wait, attempt, self.retries)
                self._sleeper(wait)
                continue
            if result.status in (400, 404):
                # Bad chat id, malformed HTML, blocked bot: retrying is pointless.
                logger.error("telegram %s rejected with HTTP %d: %s", method, result.status, _error_description(result))
                return result
            if attempt < self.retries:
                wait = min(2.0 * attempt, 15.0)
                logger.warning("telegram %s failed (%s) — retrying in %.1fs", method, result.error or f"HTTP {result.status}", wait)
                self._sleeper(wait)
        return last

    def send_text(self, chat_id: str, text: str, *, kind: str = "message", dedupe_key: str = "") -> SendOutcome:
        """Send ``text`` to ``chat_id``, chunked, with dry-run support."""
        outcome = SendOutcome(chat_id=str(chat_id), kind=kind, dry_run=self.dry_run, dedupe_key=dedupe_key)
        chunks = chunk_text(text)
        outcome.chunks = len(chunks)
        self.stats["chunks"] += len(chunks)
        if not chunks:
            outcome.error = "empty message"
            self.stats["failed"] += 1
            return outcome

        if self.dry_run:
            outcome.preview = chunks[0][:400]
            for index, chunk in enumerate(chunks, start=1):
                print(
                    f"\n--- [dry-run] telegram {kind} → chat {chat_id} ({index}/{len(chunks)}) ---",
                    file=self.preview_stream,
                )
                print(chunk, file=self.preview_stream)
            logger.info(
                "[dry-run] would POST sendMessage to chat %s (%d chunk(s), %d chars, parse_mode=%s)",
                chat_id, len(chunks), sum(len(chunk) for chunk in chunks), self.parse_mode,
            )
            outcome.ok = True
            self.stats["sent"] += len(chunks)
            return outcome

        for index, chunk in enumerate(chunks, start=1):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": self.parse_mode,
                "disable_web_page_preview": self.disable_link_preview,
            }
            result = self.call("sendMessage", payload)
            if result is None:
                outcome.error = "no response"
                self.stats["failed"] += 1
                return outcome
            outcome.attempts += result.attempts or 1
            outcome.status = result.status
            if not result.ok:
                outcome.error = _error_description(result) or f"HTTP {result.status}"
                self.stats["failed"] += 1
                logger.error(
                    "telegram delivery to chat %s failed (%s) — chunk %d/%d dropped",
                    chat_id, outcome.error, index, len(chunks),
                )
                return outcome
            message_id = _message_id(result)
            if message_id:
                outcome.message_ids.append(message_id)

        outcome.ok = True
        self.stats["sent"] += len(chunks)
        logger.info("telegram %s delivered to chat %s (%d chunk(s))", kind, chat_id, len(chunks))
        return outcome

    def get_me(self) -> dict[str, Any]:
        """``getMe`` — validates the token and returns the bot's identity."""
        result = self.call("getMe")
        if result is None:
            return {"dry_run": True}
        if not result.ok:
            return {"ok": False, "status": result.status, "error": _error_description(result)}
        payload = _json_payload(result)
        bot = payload.get("result") if isinstance(payload.get("result"), Mapping) else {}
        return {
            "ok": bool(payload.get("ok")),
            "id": as_int(bot.get("id")),
            "username": str(bot.get("username") or ""),
            "first_name": str(bot.get("first_name") or ""),
            "can_join_groups": bool(bot.get("can_join_groups")),
        }


def _json_payload(result: FetchResult) -> dict[str, Any]:
    """Parse a Bot API JSON envelope, tolerating a non-JSON body."""
    try:
        payload = json.loads(result.text or "")
    except (TypeError, ValueError):
        return {"ok": False, "description": (result.text or "")[:200]}
    return payload if isinstance(payload, dict) else {"ok": False, "description": "unexpected response shape"}


def _error_description(result: FetchResult) -> str:
    """Telegram's own ``description`` field, which explains 400s precisely."""
    payload = _json_payload(result)
    description = str(payload.get("description") or result.error or "")
    return description[:300]


def _retry_after(result: FetchResult) -> float:
    """``parameters.retry_after`` from a 429 envelope."""
    payload = _json_payload(result)
    parameters = payload.get("parameters")
    if isinstance(parameters, Mapping):
        return as_float(parameters.get("retry_after"))
    return as_float(result.meta.get("retry_after") if hasattr(result, "meta") else 0.0)


def _message_id(result: FetchResult) -> int:
    """``result.message_id`` from a successful ``sendMessage``."""
    payload = _json_payload(result)
    inner = payload.get("result")
    if isinstance(inner, Mapping):
        return as_int(inner.get("message_id"))
    return 0


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


@dataclass
class AlertRunReport:
    """What one alert run did — the payload for ``--json`` and the CI summary."""

    run_id: str = ""
    generated_at: str = field(default_factory=iso)
    report_path: str = ""
    report_run_id: str = ""
    report_age_hours: float = 0.0
    bot_username: str = ""
    chats: list[str] = field(default_factory=list)
    bridge_alerts_available: int = 0
    bridge_sent: int = 0
    bridge_suppressed: int = 0
    bridge_capped: int = 0
    digest_sent: int = 0
    digest_suppressed: int = 0
    digest_rows: int = 0
    failures: int = 0
    dry_run: bool = False
    forced: bool = False
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    ledger: dict[str, Any] = field(default_factory=dict)
    sender_stats: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    status: str = "completed"
    seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AlertEngine:
    """Turns a maintenance report into delivered Telegram messages.

    The engine owns the *policy* (what is worth sending, how often) while
    :class:`TelegramSender` owns the *transport*. Keeping them apart means the
    policy can be unit-tested without any HTTP at all, which matters because the
    Bot API is unreachable from CI sandboxes.
    """

    def __init__(
        self,
        settings: Any,
        sender: TelegramSender,
        ledger: AlertLedger,
        *,
        chats: Sequence[str] | None = None,
        bridge_limit: int = DEFAULT_BRIDGE_LIMIT,
        digest_top_n: int = DEFAULT_DIGEST_TOP_N,
        window_hours: float = 24.0,
        force: bool = False,
    ) -> None:
        self.settings = settings
        self.sender = sender
        self.ledger = ledger
        self.chats = list(chats or [])
        self.bridge_limit = max(0, int(bridge_limit))
        self.digest_top_n = max(1, int(digest_top_n))
        self.window_hours = max(1.0, float(window_hours))
        self.force = bool(force)

    # -- policy ----------------------------------------------------------- #
    def _bridge_key(self, alert: Mapping[str, Any]) -> str:
        """Suppression key for a bridge alert.

        Includes the joined cluster ids: the *same* node bridging a *different*
        pair of clusters is new information and deserves its own alert, while a
        repeat of the identical bridge is noise.
        """
        explicit = str(alert.get("dedupe_key") or "")
        if explicit:
            return explicit
        key = str(alert.get("canonical_key") or alert.get("name") or "unknown")
        clusters = "|".join(sorted(_clusters(alert)))
        return f"bridge:{key}:{clusters}"

    def select_bridges(self, report: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int, int]:
        """``(alerts_to_send, suppressed, capped)`` for one report."""
        alerts = report_bridge_alerts(report)
        selected: list[dict[str, Any]] = []
        suppressed = 0
        for alert in alerts:
            key = self._bridge_key(alert)
            if not self.force and self.ledger.suppressed(key):
                suppressed += 1
                logger.debug("bridge alert suppressed (sent <%.0fh ago): %s", self.ledger.suppression_hours, key)
                continue
            selected.append(alert)
        capped = max(0, len(selected) - self.bridge_limit)
        if capped:
            logger.warning(
                "%d bridge alert(s) exceed the per-run limit of %d — the strongest %d are sent, the rest wait for the next run",
                capped, self.bridge_limit, self.bridge_limit,
            )
            selected = selected[: self.bridge_limit]
        return selected, suppressed, capped

    # -- delivery --------------------------------------------------------- #
    def push_bridges(self, report: Mapping[str, Any]) -> list[SendOutcome]:
        """Send bridge alerts to every configured chat."""
        run_report_outcomes: list[SendOutcome] = []
        selected, suppressed, capped = self.select_bridges(report)
        self.pending_suppressed = suppressed
        self.pending_capped = capped
        self.pending_available = len(report_bridge_alerts(report))
        if not selected:
            logger.info("no new bridge alerts to push (%d suppressed)", suppressed)
            return run_report_outcomes

        total = len(selected)
        for position, alert in enumerate(selected, start=1):
            text = format_bridge_alert(alert, rank=position, total=total)
            key = self._bridge_key(alert)
            for chat_id in self.chats:
                outcome = self.sender.send_text(chat_id, text, kind="bridge", dedupe_key=key)
                if outcome.ok and not self.sender.dry_run:
                    self.ledger.mark_sent(key)
                    self.ledger.bump("bridge_alerts_sent")
                elif not outcome.ok:
                    self.ledger.bump("bridge_alerts_failed")
                elif outcome.ok and self.sender.dry_run:
                    self.ledger.bump("bridge_alerts_dry_run")
                run_report_outcomes.append(outcome)
            # One ledger entry per alert, not per chat: suppression is about the
            # *fact*, and the fact was delivered to the whole audience.
            if self.sender.dry_run and any(outcome.ok for outcome in run_report_outcomes[-len(self.chats):]):
                self.ledger.mark_sent(key)
        return run_report_outcomes

    def send_digest(self, report: Mapping[str, Any]) -> list[SendOutcome]:
        """Send the daily top-N digest to chats that have not had one today."""
        outcomes: list[SendOutcome] = []
        rows = report_top_anomalies(report, self.digest_top_n)
        self.pending_digest_rows = len(rows)
        text = format_digest(report, rows, top_n=self.digest_top_n, window_hours=self.window_hours)

        targets = [chat for chat in self.chats if self.force or not self.ledger.digest_sent_today(chat)]
        skipped = len(self.chats) - len(targets)
        self.pending_digest_suppressed = skipped
        if skipped:
            logger.info("digest already delivered today to %d chat(s)", skipped)
        if not targets:
            return outcomes

        for chat_id in targets:
            outcome = self.sender.send_text(chat_id, text, kind="digest", dedupe_key=f"digest:{utc_now():%Y-%m-%d}")
            if outcome.ok:
                self.ledger.mark_digest(chat_id)
                self.ledger.bump("digests_sent" if not self.sender.dry_run else "digests_dry_run")
            else:
                self.ledger.bump("digests_failed")
            outcomes.append(outcome)
        return outcomes

    # -- orchestration ---------------------------------------------------- #
    def run(
        self,
        report: Mapping[str, Any],
        *,
        report_path: str = "",
        bridges: bool = True,
        digest: bool = True,
    ) -> AlertRunReport:
        """Execute the configured alert passes and return a run report."""
        started = time.perf_counter()
        run_report = AlertRunReport(
            run_id=str(getattr(self.settings, "run_id", "") or f"alerts-{utc_now():%Y%m%dT%H%M%SZ}"),
            report_path=report_path,
            report_run_id=str(report.get("run_id") or ""),
            report_age_hours=round(report_age_hours(report), 3),
            chats=list(self.chats),
            dry_run=self.sender.dry_run,
            forced=self.force,
        )
        self.pending_suppressed = 0
        self.pending_capped = 0
        self.pending_available = 0
        self.pending_digest_rows = 0
        self.pending_digest_suppressed = 0

        if not self.chats:
            run_report.errors.append("no chat ids configured (TELEGRAM_CHAT_IDS)")
            run_report.status = "failed"
            run_report.seconds = round(time.perf_counter() - started, 3)
            return run_report

        if bridges:
            try:
                outcomes = self.push_bridges(report)
                run_report.outcomes += [outcome.to_dict() for outcome in outcomes]
                run_report.bridge_alerts_available = self.pending_available
                run_report.bridge_suppressed = self.pending_suppressed
                run_report.bridge_capped = self.pending_capped
                run_report.bridge_sent = sum(1 for outcome in outcomes if outcome.ok)
            except TelegramAuthError as exc:
                run_report.errors.append(redact_token(exc, self.sender.token))
                run_report.status = "failed"
                self.ledger.save()
                run_report.seconds = round(time.perf_counter() - started, 3)
                return run_report
            except Exception as exc:  # noqa: BLE001 - one bad alert must not stop the digest
                message = redact_token(f"bridge pass failed: {exc.__class__.__name__}: {exc}", self.sender.token)
                logger.error(message)
                run_report.errors.append(message)

        if digest:
            try:
                outcomes = self.send_digest(report)
                run_report.outcomes += [outcome.to_dict() for outcome in outcomes]
                run_report.digest_rows = self.pending_digest_rows
                run_report.digest_suppressed = self.pending_digest_suppressed
                run_report.digest_sent = sum(1 for outcome in outcomes if outcome.ok)
            except TelegramAuthError as exc:
                run_report.errors.append(redact_token(exc, self.sender.token))
                run_report.status = "failed"
            except Exception as exc:  # noqa: BLE001
                message = redact_token(f"digest pass failed: {exc.__class__.__name__}: {exc}", self.sender.token)
                logger.error(message)
                run_report.errors.append(message)

        run_report.failures = sum(1 for outcome in run_report.outcomes if not outcome.get("ok"))
        if run_report.status != "failed":
            run_report.status = "failed" if run_report.failures and not run_report.bridge_sent and not run_report.digest_sent else (
                "partial" if run_report.failures else "completed"
            )
        if run_report.errors and run_report.status == "completed":
            run_report.status = "partial"
        run_report.sender_stats = dict(self.sender.stats)
        run_report.ledger = self.ledger.describe()
        self.ledger.save()
        run_report.seconds = round(time.perf_counter() - started, 3)
        logger.info(
            "alerts: %d bridge(s) sent, %d suppressed, %d digest(s) sent, %d failure(s) in %.2fs",
            run_report.bridge_sent, run_report.bridge_suppressed, run_report.digest_sent, run_report.failures, run_report.seconds,
        )
        return run_report


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def resolve_token(settings: Any, override: str = "") -> str:
    """Bot token from the CLI override or the environment."""
    token = str(override or "").strip()
    if token:
        return token
    for attribute in ("telegram_bot_token", "bot_token"):
        candidate = str(getattr(settings, attribute, "") or "").strip()
        if candidate:
            return candidate
    return str(os.environ.get("TELEGRAM_BOT_TOKEN", "") or "").strip()


def resolve_chats(settings: Any, override: str = "") -> list[str]:
    """Chat ids from the CLI override, settings, or the environment."""
    if override.strip():
        return parse_chat_ids(override)
    configured = parse_chat_ids(getattr(settings, "telegram_chat_ids", ""))
    if configured:
        return configured
    return parse_chat_ids(os.environ.get("TELEGRAM_CHAT_IDS", ""))


def build_sender(
    settings: Any,
    token: str,
    *,
    dry_run: bool,
    client: FetchClient | None = None,
    preview_stream: Any = None,
) -> TelegramSender:
    """Construct a :class:`TelegramSender` from settings, with sane defaults."""
    return TelegramSender(
        settings,
        token,
        client=client,
        api_base=str(getattr(settings, "telegram_api_base", DEFAULT_API_BASE) or DEFAULT_API_BASE),
        parse_mode=str(getattr(settings, "telegram_parse_mode", DEFAULT_PARSE_MODE) or DEFAULT_PARSE_MODE),
        rate_per_sec=as_float(getattr(settings, "telegram_rate_per_sec", DEFAULT_RATE_PER_SEC), DEFAULT_RATE_PER_SEC),
        burst=as_float(getattr(settings, "telegram_burst", DEFAULT_BURST), DEFAULT_BURST),
        retries=as_int(getattr(settings, "telegram_send_retries", DEFAULT_SEND_RETRIES), DEFAULT_SEND_RETRIES),
        dry_run=dry_run,
        disable_link_preview=bool(getattr(settings, "telegram_disable_link_preview", True)),
        preview_stream=preview_stream,
    )


def build_ledger(settings: Any, *, state_path: Path | str | None = None, persist: bool = True) -> AlertLedger:
    """Load (or create) the suppression ledger.

    ``persist=False`` (dry runs) reads the real ledger — so suppression is
    evaluated against actual history — but writes nothing back.
    """
    suppression_hours = as_float(
        getattr(settings, "telegram_suppression_hours", DEFAULT_SUPPRESSION_HOURS), DEFAULT_SUPPRESSION_HOURS
    )
    if state_path:
        path = Path(state_path)
    else:
        # ``Settings.state_path`` resolves an empty STATE_DIR to the repository
        # root, which would litter the checkout with state files (and risk
        # committing one). Fall back to the documented default instead.
        configured = str(getattr(settings, "state_dir", "") or "").strip()
        base = Path(configured) if configured else Path(".state")
        if not base.is_absolute():
            base = REPO_ROOT / base
        path = base / STATE_FILE
    ledger = AlertLedger.load(path, suppression_hours=suppression_hours, persist=persist)
    removed = ledger.prune()
    if removed:
        logger.info("telegram ledger pruned %d stale suppression entr(ies)", removed)
    return ledger


def resolve_report_path(settings: Any, explicit: str = "", report_dir: str = "") -> Path:
    """Which maintenance report to alert on.

    ``--report`` wins. Otherwise the newest report in ``--report-dir`` (or
    ``settings.report_path``). A missing report is a hard error for the bridge
    pass: silently sending nothing would look identical to "no bridges found".
    """
    if explicit:
        return Path(explicit)
    directory = Path(report_dir) if report_dir else Path(getattr(settings, "report_path", Path("reports")))
    latest = find_latest_report(directory)
    if latest is None:
        raise FileNotFoundError(f"no {REPORT_GLOB} report found in {directory} — run graph_analytics.py first")
    return latest


def summarise(run_report: AlertRunReport) -> str:
    """Human-readable one-screen summary."""
    lines = [
        f"status        : {run_report.status}",
        f"report        : {run_report.report_path or '(none)'}",
        f"report run    : {run_report.report_run_id or '(unknown)'} (age {run_report.report_age_hours:.1f}h)",
        f"bot           : {run_report.bot_username or '(not verified)'}",
        f"chats         : {', '.join(run_report.chats) or '(none)'}",
        f"bridges       : {run_report.bridge_sent} sent / {run_report.bridge_suppressed} suppressed / "
        f"{run_report.bridge_capped} over limit ({run_report.bridge_alerts_available} available)",
        f"digest        : {run_report.digest_sent} sent / {run_report.digest_suppressed} already sent today "
        f"({run_report.digest_rows} row(s))",
        f"failures      : {run_report.failures}",
        f"dry run       : {'yes' if run_report.dry_run else 'no'}",
        f"ledger        : {run_report.ledger.get('path', '')} "
        f"({run_report.ledger.get('tracked_alerts', 0)} suppression key(s))",
        f"seconds       : {run_report.seconds:.2f}",
    ]
    if run_report.errors:
        lines.append("errors        :")
        lines += [f"  - {error}" for error in run_report.errors[:8]]
    return "\n".join(lines)


def emit_github_output(run_report: AlertRunReport) -> None:
    """Append run facts to ``$GITHUB_OUTPUT`` for the workflow summary."""
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    payload = {
        "telegram_status": run_report.status,
        "telegram_bridge_sent": run_report.bridge_sent,
        "telegram_bridge_suppressed": run_report.bridge_suppressed,
        "telegram_digest_sent": run_report.digest_sent,
        "telegram_failures": run_report.failures,
        "telegram_report": run_report.report_path,
        "telegram_seconds": f"{run_report.seconds:.2f}",
    }
    try:
        with open(target, "a", encoding="utf-8") as handle:
            for key, value in payload.items():
                handle.write(f"{key}={value}\n")
    except OSError as exc:
        logger.warning("could not write GITHUB_OUTPUT: %s", exc)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    """Argument parser mirroring the conventions of ``ingest.py``."""
    parser = argparse.ArgumentParser(
        prog="telegram_bot.py",
        description="Push cluster-bridge alerts and the daily anomaly digest to Telegram.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python telegram_bot.py --test\n"
            "  python telegram_bot.py --all --dry-run\n"
            "  python telegram_bot.py --report reports/graph_maintenance_20260927.json --all\n"
            "  python telegram_bot.py --digest --force --json\n"
        ),
    )
    passes = parser.add_argument_group("passes")
    passes.add_argument("--all", action="store_true", help="bridge alerts and daily digest (default)")
    passes.add_argument("--bridge", action="store_true", help="push new cluster-bridge alerts only")
    passes.add_argument("--digest", action="store_true", help="send the top-N anomaly digest only")
    passes.add_argument("--test", action="store_true", help="verify the token (getMe) and send a test message")
    passes.add_argument(
        "--failure",
        default="",
        metavar="TEXT",
        help="warn about a failed scheduled run (no report needed); suppressed like every other alert",
    )

    sources = parser.add_argument_group("input")
    sources.add_argument("--report", default="", help="maintenance report JSON to alert on")
    sources.add_argument("--report-dir", default="", help="directory to pick the newest report from")
    sources.add_argument("--top", type=int, default=0, help="digest rows (default: settings or 5)")
    sources.add_argument("--bridge-limit", type=int, default=0, help="max bridge alerts per run")
    sources.add_argument("--window-hours", type=float, default=24.0, help="digest window in hours")
    sources.add_argument("--workflow", default="", help="workflow name for --failure (e.g. \"Hourly Ingest\")")
    sources.add_argument("--source", default="", help="source id for --failure, when one source is at fault")
    sources.add_argument("--exit-code", default="", help="exit code of the failed run, for --failure")
    sources.add_argument("--run-url", default="", help="link to the failed run, for --failure")

    delivery = parser.add_argument_group("delivery")
    delivery.add_argument("--chat-id", default="", help="override TELEGRAM_CHAT_IDS (comma separated)")
    delivery.add_argument("--token", default="", help="override TELEGRAM_BOT_TOKEN (prefer the environment)")
    delivery.add_argument("--force", action="store_true", help="ignore suppression and the daily digest guard")
    delivery.add_argument("--dry-run", action="store_true", help="format and print messages without sending")
    delivery.add_argument("--state", default="", help="ledger path (default: <state_dir>/telegram_alerts.json)")

    output = parser.add_argument_group("output")
    output.add_argument("--json", action="store_true", help="print the run report as JSON")
    output.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    output.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")
    output.add_argument("--version", action="version", version=f"PuppetNET telegram alert engine {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    env_overrides = {key: value for key, value in os.environ.items() if key.startswith("PUPPETNET_")}
    # validate=False: this tool never opens a database or fetches a source, so
    # demanding NEO4J_PASSWORD (or a relay) here would be a false alarm — and it
    # would make the alert engine unrunnable on a host that only sends messages.
    settings = load_settings({**dict(os.environ), **env_overrides}, validate=False)
    level = "DEBUG" if args.verbose else ("WARNING" if args.quiet else str(getattr(settings, "log_level", "INFO")))
    # With --json, stdout carries the report only: everything else goes to stderr
    # so the output can be piped straight into jq.
    configure_logging(
        level=level,
        json_output=bool(getattr(settings, "log_json", False)),
        stream=sys.stderr if args.json else None,
    )

    token = resolve_token(settings, args.token)
    chats = resolve_chats(settings, args.chat_id)
    dry_run = bool(args.dry_run)

    redactor = install_token_redactor(token)
    try:
        if not token:
            message = (
                "no bot token: set TELEGRAM_BOT_TOKEN (or PUPPETNET_TELEGRAM_BOT_TOKEN). "
                "Create one with @BotFather; keep it out of the repository."
            )
            logger.error(message)
            print(f"telegram alerts: {message}", file=sys.stderr)
            return EXIT_CONFIG
        if not chats and not dry_run:
            message = "no chat ids: set TELEGRAM_CHAT_IDS (numeric id or @channelname)"
            logger.error(message)
            print(f"telegram alerts: {message}", file=sys.stderr)
            return EXIT_CONFIG
        if not chats:
            # A dry run with no channel configured is exactly how an operator
            # previews the message format before wiring up a bot, so it gets a
            # placeholder destination instead of an error.
            chats = [DRY_RUN_CHAT]
            logger.info("dry run without chat ids — rendering to a placeholder destination")

        bridge_limit = as_int(args.bridge_limit) or as_int(
            getattr(settings, "telegram_bridge_alert_limit", DEFAULT_BRIDGE_LIMIT), DEFAULT_BRIDGE_LIMIT
        )
        digest_top_n = as_int(args.top) or as_int(
            getattr(settings, "telegram_digest_top_n", DEFAULT_DIGEST_TOP_N), DEFAULT_DIGEST_TOP_N
        )
        window_hours = max(1.0, float(args.window_hours or 24.0))

        print(
            banner(f"PuppetNET Telegram alerts — v{__version__} · python {platform.python_version()}"),
            file=sys.stderr if args.json else sys.stdout,
        )
        logger.info(
            "config: %d chat(s), bridge limit %d, digest top %d, window %.0fh, dry-run %s",
            len(chats), bridge_limit, digest_top_n, window_hours, dry_run,
        )

        sender = build_sender(
            settings, token, dry_run=dry_run, preview_stream=sys.stderr if args.json else sys.stdout
        )
        ledger = build_ledger(settings, state_path=args.state or None, persist=not dry_run)
        engine = AlertEngine(
            settings,
            sender,
            ledger,
            chats=chats,
            bridge_limit=bridge_limit,
            digest_top_n=digest_top_n,
            window_hours=window_hours,
            force=bool(args.force),
        )

        # --- operational failure warning -------------------------------- #
        # Placed before the report passes because there may be no report: this is
        # the pass for the run that died before it wrote one.
        if args.failure:
            key = failure_key(
                args.failure, workflow=args.workflow, source=args.source, exit_code=args.exit_code
            )
            text = format_failure_alert(
                args.failure,
                workflow=args.workflow,
                run_url=args.run_url,
                exit_code=args.exit_code,
                source=args.source,
            )
            run_report = AlertRunReport(
                run_id=str(getattr(settings, "run_id", "") or f"alerts-{utc_now():%Y%m%dT%H%M%SZ}"),
                chats=list(chats),
                dry_run=dry_run,
                forced=bool(args.force),
            )
            if not args.force and ledger.suppressed(key):
                # One alert per broken thing per suppression window: an hourly cron
                # would otherwise send the same warning 24 times a day, and a
                # channel that cries wolf gets muted — including the alert that
                # matters.
                logger.info("failure alert suppressed (sent <%.0fh ago): %s", ledger.suppression_hours, key)
                run_report.bridge_suppressed = 1
            else:
                for chat_id in chats:
                    outcome = sender.send_text(chat_id, text, kind="failure", dedupe_key=key)
                    run_report.outcomes.append(outcome.to_dict())
                    if outcome.ok:
                        run_report.bridge_sent += 1
                        if not dry_run:
                            ledger.mark_sent(key)
                            ledger.bump("failure_alerts_sent")
                    else:
                        run_report.failures += 1
                        ledger.bump("failure_alerts_failed")
            run_report.sender_stats = dict(sender.stats)
            run_report.status = (
                "failed" if run_report.failures else ("partial" if run_report.errors else "completed")
            )
            run_report.ledger = ledger.describe()
            ledger.save()
            emit_github_output(run_report)
            print(json.dumps(run_report.to_dict(), indent=2, ensure_ascii=False) if args.json else summarise(run_report))
            sender.close()
            return EXIT_OK if run_report.status != "failed" else EXIT_DELIVERY

        # --- connectivity check ----------------------------------------- #
        if args.test:
            identity = sender.get_me()
            logger.info("getMe → %s", json.dumps(identity, ensure_ascii=False))
            run_report = AlertRunReport(
                run_id=str(getattr(settings, "run_id", "") or f"alerts-{utc_now():%Y%m%dT%H%M%SZ}"),
                chats=list(chats),
                dry_run=dry_run,
                bot_username=str(identity.get("username") or ""),
                ledger=ledger.describe(),
            )
            if not dry_run and not identity.get("ok", True):
                run_report.errors.append(f"getMe failed: {identity.get('error') or identity.get('status')}")
                run_report.status = "failed"
            if chats:
                outcome = sender.send_text(
                    chats[0],
                    compact([
                        tag("🔔 PuppetNET alert engine online"),
                        bullet("Version", __version__),
                        bullet("Bot", escape(str(identity.get("username") or ""))),
                        bullet("Chats", [escape(chat) for chat in chats]),
                        bullet("Timestamp", iso()),
                    ]),
                    kind="test",
                )
                run_report.outcomes.append(outcome.to_dict())
                if not outcome.ok:
                    run_report.failures = 1
                    run_report.status = "failed"
                    run_report.errors.append(outcome.error)
            print(json.dumps(run_report.to_dict(), indent=2, ensure_ascii=False) if args.json else summarise(run_report))
            sender.close()
            return EXIT_OK if run_report.status != "failed" else EXIT_DELIVERY

        # --- report-driven passes --------------------------------------- #
        want_bridges = bool(args.all or args.bridge or not args.digest)
        want_digest = bool(args.all or args.digest or not args.bridge)
        if args.bridge and not args.all:
            want_digest = False
        if args.digest and not args.all:
            want_bridges = False

        try:
            report_path = resolve_report_path(settings, args.report, args.report_dir)
            report = load_report(report_path)
        except (FileNotFoundError, ValueError) as exc:
            logger.error("%s", exc)
            # Also on stderr: the log stream can be silenced by LOG_LEVEL, and a
            # CLI that exits 3 without saying why is useless in a cron job.
            print(f"telegram alerts: {exc}", file=sys.stderr)
            sender.close()
            return EXIT_CONFIG

        age = report_age_hours(report)
        if age > max(window_hours * 2, 48.0):
            logger.warning(
                "report %s is %.1fh old — alerts may describe a graph state that no longer exists",
                report_path.name, age,
            )

        run_report = engine.run(report, report_path=str(report_path), bridges=want_bridges, digest=want_digest)
        run_report.bot_username = ""
        emit_github_output(run_report)

        if args.json:
            print(json.dumps(run_report.to_dict(), indent=2, ensure_ascii=False))
        else:
            print(summarise(run_report))

        sender.close()
        if run_report.status == "failed":
            return EXIT_DELIVERY if run_report.failures else EXIT_CONFIG
        return EXIT_OK
    finally:
        uninstall_token_redactor(redactor)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
