"""Aviation OSINT: aircraft registries, ADS-B telemetry and flight logs.

Three adapters over one domain, because "who flies what, with whom" is the
movement half of a puppeteering graph and no single source answers it:

``faa_registry`` (:class:`FaaRegistryAdapter`) — edge weight **1.0**
    The FAA's monthly *Releasable Aircraft* dump (``registry.faa.gov``): every
    US-registered N-number with its registrant, address and airframe data. An
    official register, so it is weighted like one. Two things come out of it:

    * ``(:Person|:Company)-[:OWNS]->(:Aircraft {tail_number, owner})``
    * ``(:Person)-[:SHARES_ADDRESS]->(:Person)`` — distinct registrants at one
      normalised address. A fleet held through one LLC, a registered agent's
      desk, or two principals sharing a hangar all look like this, and it is the
      same signal the offshore sources give for shell companies.

``adsb_exchange`` (:class:`AdsbExchangeAdapter`) — edge weight **0.8**
    Per-tail enrichment for a watchlist (``AIRCRAFT_TAIL_NUMBERS``) or for tails
    discovered elsewhere in the run. Tries the free community read-through of
    ADS-B Exchange data (``adsbdb.com``) first and, when
    ``ADSBEXCHANGE_API_KEY`` is set, the ADS-B Exchange v2 API on RapidAPI.
    Telemetry is second-hand and self-reported, hence 0.8 rather than 1.0.

``flight_logs`` (:class:`FlightLogAdapter`) — edge weight **0.8**
    Passenger manifests released as CSV, plain text or PDF (court exhibits,
    FOIA dumps, operator logs). Yields
    ``(:Person)-[:PASSENGER_ON {weight: 0.8}]->(:Aircraft)``.

    Two parsing modes, deliberately different:

    * **Tabular** — a delimited manifest with a name-ish column is parsed
      directly; every row becomes a ``PASSENGER_ON`` edge with the row as
      evidence.
    * **Unstructured** — when no tabular structure is found the log is emitted
      as a plain text document and the NLP pipeline extracts the people, which
      is what the co-occurrence/dependency machinery is for. Guessing names
      with a regex here would put "Flight Department" and "Departure Time" into
      the graph as people, so it is not done.

All three stream rather than buffer: the FAA dump is tens of megabytes packed
and hundreds of thousands of rows, so it is read line by line through
:mod:`puppetnet.sources.archive` and capped by ``row_limit``.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from ..domain import address_key, looks_like_tail_number
from ..logging_utils import get_logger
from ..models import (
    Document,
    Entity,
    EntityType,
    ExtractionMethod,
    Relation,
    RelationType,
    has_organizational_marker,
    iso,
    utcnow,
)
from ..parsing.craft import CraftDetector
from ..parsing.text_extract import extract_text
from .archive import canonical_column, first_present, iter_delimited_rows, stream_archive_members
from .base import SourceAdapter

__all__ = ["AviationAdapter", "FaaRegistryAdapter", "AdsbExchangeAdapter", "FlightLogAdapter"]

logger = get_logger("sources.aviation")

# --------------------------------------------------------------------------- #
# FAA Releasable Aircraft (MASTER.txt) — column aliases
# --------------------------------------------------------------------------- #
#: The FAA renames columns between releases and pads values with spaces, so
#: every logical field is resolved from an ordered alias list. If ``tail`` and
#: ``owner`` cannot be resolved the dump has changed shape and the adapter says
#: so instead of emitting garbage.
FAA_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "tail": ("n_number", "nnum", "tail_number", "tailnumber", "registration", "aircraft_registration"),
    "serial": ("aircraft_serial_number", "serial_number", "aircraft_serial", "acft_serial_number", "serial"),
    "mode_s": ("mode_s_code_hex", "mode_s_hex", "modes_hex", "icao_hex", "hex"),
    "mfr_code": ("aircraft_mfr_code", "ac_mfr_code", "manufacturer_code", "mfr_code"),
    "model_code": ("aircraft_model_code", "ac_model_code", "model_code"),
    "mfr_name": ("aircraft_mfr_name", "ac_mfr_name", "manufacturer_name", "manufacturer", "mfr_name"),
    "model_name": ("aircraft_model_name", "ac_model_name", "model_name", "model", "aircraft_model"),
    "year": ("year_mfr", "year_manufactured", "year"),
    "engines": ("no_engines", "number_of_engines", "engines", "engine_count"),
    "certification": ("certification_code", "certification", "airworthiness_certification_code", "airworthiness_code"),
    "airworth_date": ("air_worth_date", "airworth_date", "airworthiness_date"),
    "owner": (
        "registrant_business_name", "registrant_name", "registrant", "owner_name", "owner",
        "registrant_individual_full_name", "name_of_registrant",
    ),
    "street": ("street", "street1", "registrant_street", "address", "address1", "registrant_address"),
    "street2": ("street2", "registrant_street2", "address2"),
    "city": ("city", "registrant_city"),
    "state": ("state", "state_code", "registrant_state"),
    "zip": ("zip_code", "zip", "postal_code", "registrant_zip_code", "registrant_zip"),
    "country": ("country", "country_code", "registrant_country"),
    "unique_id": ("unique_id", "uniqueid"),
    "status": ("status_code", "status", "last_action_date", "expiration_date"),
    "other_names": tuple(f"other_names{i}" for i in range(1, 10)) + tuple(f"other_names_{i}" for i in range(1, 10)),
    "kit_mfr": ("kit_mfr_name", "kit_manufacturer"),
    "kit_model": ("kit_model", "kit_model_name"),
    "region": ("region", "county", "registrant_region"),
}

#: Registrant names that are clearly corporate even in FAA ALL-CAPS form.
_CORPORATE_MARKERS: tuple[str, ...] = (
    "llc", "inc", "incorporated", "ltd", "limited", "corp", "corporation", "company", "co",
    "lp", "llp", "plc", "trust", "trustee", "trustees", "bank", "na", "holdings", "holding",
    "group", "air", "aircraft", "aviation", "airways", "airlines", "jet", "jets", "charter",
    "leasing", "lease", "management", "services", "partners", "partnership", "foundation",
    "family", "estate", "revocable", "irrevocable", "nominee", "nominees", "sa", "ag", "gmbh",
    "nv", "bv", "pty", "pte", "ooo", "jsc", "sarl", "srl", "sas", "international", "overseas",
)

_HONORIFIC_STRIP_RE = re.compile(
    r"\b(mr|mrs|ms|miss|dr|prof|professor|sir|dame|lord|lady|hon|reverend|rev|captain|capt|"
    r"general|gen|colonel|col|major|maj|lieutenant|lt|senator|sen|governor|gov|minister)\b\.?\s*",
    re.IGNORECASE,
)
_NAME_STOPWORDS: frozenset[str] = frozenset(
    {
        "passenger", "passengers", "passenger manifest", "manifest", "flight", "flight log", "departure",
        "arrival", "departed", "arrived", "date", "time", "origin", "destination", "airport", "tail",
        "tail number", "aircraft", "pilot", "crew", "captain", "notes", "comments", "page", "total",
        "subtotal", "name", "names", "guests", "guest", "occupants", "occupant", "boarding", "deplaning",
        "log", "logs", "sheet", "sheet 1", "undated", "unknown", "n/a", "na", "none", "the", "and", "of",
        "with", "from", "to", "at", "on", "in", "for", "by", "no", "number", "record", "records", "entry",
        "entries", "source", "released", "document", "exhibit", "attachment", "table", "column", "row",
    }
)


class AviationAdapter(SourceAdapter):
    """Shared helpers for the three aviation sources."""

    adapter_name = "aviation"

    # ------------------------------------------------------------------ #
    # Entity construction
    # ------------------------------------------------------------------ #
    def aircraft_entity(self, tail: str, *, properties: dict[str, Any] | None = None, aliases: Iterable[str] = ()) -> Entity:
        """A ``:Craft`` node that the domain layer will label ``:Aircraft``."""
        registration = normalize_tail(tail)
        props = {"craft_kind": "Aircraft", "registration": registration, "tail_number": registration}
        props.update({k: v for k, v in (properties or {}).items() if v not in (None, "", [], {})})
        return self.entity(
            registration,
            EntityType.CRAFT,
            properties=props,
            aliases=[*(a for a in aliases if a and a.strip()), tail.strip()] if tail.strip() != registration else aliases,
        )

    def owner_entity(self, name: str, *, properties: dict[str, Any] | None = None, aliases: Iterable[str] = ()) -> Entity:
        """Registrant/owner as a ``:Person`` or ``:Organization`` node."""
        cleaned = clean_registrant_name(name)
        if not cleaned:
            raise ValueError("empty owner name")
        etype = EntityType.ORGANIZATION if looks_corporate(cleaned) else EntityType.PERSON
        surface = humanise_org_name(cleaned) if etype is EntityType.ORGANIZATION else humanise_name(cleaned)
        props = {"role": "registrant"} if etype is EntityType.ORGANIZATION else {}
        props.update({k: v for k, v in (properties or {}).items() if v not in (None, "", [], {})})
        return self.entity(surface, etype, properties=props, aliases=aliases)

    def place_entity(self, address: str, *, properties: dict[str, Any] | None = None) -> Entity | None:
        """A ``:Location`` node carrying the comparable address key."""
        cleaned = " ".join(str(address or "").split()).strip()
        if len(cleaned) < 6:
            return None
        key = address_key(cleaned)
        if not key:
            return None
        props = {"address": cleaned[:400], "address_key": key}
        props.update({k: v for k, v in (properties or {}).items() if v not in (None, "", [], {})})
        return self.entity(cleaned[:200], EntityType.LOCATION, properties=props)

    def country_entity(self, country: str, *, properties: dict[str, Any] | None = None) -> Entity | None:
        """A ``:Location`` node for a bare jurisdiction ("Malta", "United States").

        Deliberately separate from :meth:`place_entity`: a country has no street
        address, so it gets no ``address_key`` — otherwise every aircraft
        registered in the same country would look like it "shares an address".
        """
        name = " ".join(str(country or "").split()).strip()
        if len(name) < 2 or len(name) > 120:
            return None
        props = {"country": name, "address": name, "jurisdiction": name, "location_kind": "country"}
        props.update({k: v for k, v in (properties or {}).items() if v not in (None, "", [], {})})
        return self.entity(humanise_org_name(name), EntityType.LOCATION, properties=props)

    # ------------------------------------------------------------------ #
    # Shared relations
    # ------------------------------------------------------------------ #
    def ownership(
        self,
        owner: Entity,
        aircraft: Entity,
        *,
        evidence: str,
        evidence_score: float = 1.0,
        method: ExtractionMethod = ExtractionMethod.STRUCTURED,
        extra: dict[str, Any] | None = None,
    ) -> Relation:
        """``(:Person|:Company)-[:OWNS]->(:Aircraft)`` — the registry fact.

        No ``weight`` is set here: :data:`~puppetnet.models.DOMAIN_EDGE_WEIGHTS`
        owns the per-predicate graph weight (OWNS 1.0, PASSENGER_ON 0.8,
        SHARES_ADDRESS 0.8, …), so one table decides what the graph says. Only the
        analytics pass overrides it, to make a calculated ``PUPPET_MASTER_OF``
        edge's weight track its score.
        """
        return self.relation(
            owner,
            RelationType.OWNS,
            aircraft,
            evidence=evidence,
            evidence_score=evidence_score,
            method=method,
            extra=extra,
        )

    def registered_in(self, aircraft: Entity, place: Entity, *, evidence: str) -> Relation:
        return self.relation(aircraft, RelationType.REGISTERED_IN, place, evidence=evidence)

    def located_in(self, subject: Entity, place: Entity, *, evidence: str) -> Relation:
        return self.relation(subject, RelationType.LOCATED_IN, place, evidence=evidence)

    # ------------------------------------------------------------------ #
    # Bulk-dump helpers
    # ------------------------------------------------------------------ #
    def _stream_max_bytes(self) -> int:
        multiplier = float(self.option("stream_bytes_multiplier", 8))
        return int(max(1.0, multiplier) * int(self.settings.http_max_response_bytes))

    def _state_path(self) -> str:
        return os.path.join(str(self.settings.state_path), f"{self.spec.id}_state.json")

    def _load_state(self) -> dict[str, Any]:
        path = self._state_path()
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        path = self._state_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, ensure_ascii=False, default=str)
        except OSError as exc:
            self.log.warning("could not persist %s state: %s", self.spec.id, exc)

    def _dump_is_fresh(self, url: str, state: dict[str, Any]) -> bool:
        """True when this dump was already processed inside ``refresh_days``.

        A dump that yielded nothing is never considered fresh — a failed or
        reshaped download should be retried on the next run rather than skipped
        for a month.
        """
        refresh_days = int(self.option("refresh_days", 30))
        if refresh_days <= 0:
            return False
        entry = state.get(url) or {}
        if not entry.get("documents"):
            return False
        parsed = self.parse_datetime(entry.get("processed_at"))
        if parsed is None:
            return False
        return self.within_window(parsed, refresh_days)

    def _record_state(self, url: str, state: dict[str, Any], *, documents: int, rows: int) -> None:
        state[url] = {
            "processed_at": iso(utcnow()),
            "documents": documents,
            "rows": rows,
            "source_id": self.spec.id,
        }
        self._save_state(state)


# --------------------------------------------------------------------------- #
# 1. FAA aircraft registry
# --------------------------------------------------------------------------- #
class FaaRegistryAdapter(AviationAdapter):
    """N-number → registrant (owner) + address from the FAA monthly dump."""

    adapter_name = "faa_registry"

    def harvest(self) -> Iterator[Document]:
        url = str(self.option("dump_url") or self.settings.faa_registry_url or "").strip()
        if not url:
            self.log.info("no FAA registry URL configured — skipping")
            return

        state = self._load_state()
        if not self.option("force_refresh", False) and self._dump_is_fresh(url, state):
            self.log.info(
                "FAA registry dump processed less than %s day(s) ago — skipping (set force_refresh to override)",
                self.option("refresh_days", 30),
            )
            return

        tail_filter = {normalize_tail(t) for t in (self.option("tail_numbers") or self.settings.aircraft_tail_numbers or ()) if t}
        owner_filter = [str(name).lower() for name in (self.option("registrant_names") or ()) if name]
        state_filter = {str(code).strip().upper() for code in (self.option("states") or ()) if code}
        row_limit = int(self.option("row_limit", 20_000))

        documents = 0
        rows_seen = 0
        address_groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"owners": {}, "tails": []})

        for member, lines in stream_archive_members(
            self.client, url, spec=self.spec, max_bytes=self._stream_max_bytes(), member_filter=_is_master_member
        ):
            if self.ctx.budget_exhausted():
                break
            self.log.info("parsing FAA registry member %s", member)
            entities: dict[str, Entity] = {}
            relations: list[Relation] = []
            rows_in_member = 0
            usable_rows = 0
            warnings: list[str] = []

            for row in iter_delimited_rows(lines, aliases=FAA_COLUMN_ALIASES):
                if self.ctx.budget_exhausted():
                    warnings.append("budget exhausted — partial dump")
                    break
                rows_seen += 1
                rows_in_member += 1

                tail = normalize_tail(first_present(row, ("tail",)))
                if not tail:
                    continue
                if tail_filter and tail not in tail_filter:
                    continue
                owner_name = first_present(row, ("owner",)).strip()
                if not owner_name:
                    continue
                if owner_filter and not any(marker in owner_name.lower() for marker in owner_filter):
                    continue
                state_code = first_present(row, ("state",)).upper()
                if state_filter and state_code not in state_filter:
                    continue

                aircraft = self.aircraft_entity(
                    tail,
                    properties=_compact(
                        {
                            "serial_number": first_present(row, ("serial",)),
                            "mode_s_hex": first_present(row, ("mode_s",)),
                            "manufacturer": first_present(row, ("mfr_name",)),
                            "model": first_present(row, ("model_name",)),
                            "year": first_present(row, ("year",)),
                            "engines": first_present(row, ("engines",)),
                            "certification": first_present(row, ("certification",)),
                            "airworthiness_date": first_present(row, ("airworth_date",)),
                            "registry_country": first_present(row, ("country",)) or "United States",
                            "registry_state": state_code,
                            "faa_unique_id": first_present(row, ("unique_id",)),
                            "status": first_present(row, ("status",)),
                            "kit_manufacturer": first_present(row, ("kit_mfr",)),
                            "kit_model": first_present(row, ("kit_model",)),
                        }
                    ),
                    aliases=[tail.replace("N", "N-")],
                )
                try:
                    owner = self.owner_entity(
                        owner_name,
                        properties=_compact({"faa_registrant_id": first_present(row, ("unique_id",))}),
                        aliases=[a for a in _other_names(row) if a != owner_name],
                    )
                except ValueError:
                    continue

                usable_rows += 1
                entities[aircraft.canonical_key] = aircraft
                entities[owner.canonical_key] = owner
                aircraft.properties["owner"] = owner.name
                relations.append(
                    self.ownership(
                        owner,
                        aircraft,
                        evidence=f"FAA registry: {aircraft.name} registered to {owner.name}"
                        + (f" ({aircraft.properties.get('model') or 'aircraft'})" if aircraft.properties.get("model") else ""),
                    )
                )

                address = _join_address(row)
                place = self.place_entity(address, properties=_compact({"country": first_present(row, ("country",)) or "United States"}))
                if place is not None:
                    entities[place.canonical_key] = place
                    relations.append(self.located_in(owner, place, evidence=f"FAA registrant address for {aircraft.name}: {address[:160]}"))
                    relations.append(self.located_in(aircraft, place, evidence=f"FAA registry base address for {aircraft.name}: {address[:160]}"))
                    group = address_groups[place.properties.get("address_key") or place.canonical_key]
                    group["owners"].setdefault(owner.canonical_key, owner)
                    group["tails"].append(aircraft.name)
                    group.setdefault("place", place)

                country = first_present(row, ("country",)).strip()
                if country and country.upper() not in {"US", "USA", "UNITED STATES"}:
                    country_place = self.country_entity(country)
                    if country_place is not None:
                        entities[country_place.canonical_key] = country_place
                        relations.append(self.registered_in(aircraft, country_place, evidence=f"FAA registry country of registration: {country}"))

                if rows_in_member >= row_limit:
                    warnings.append(f"row_limit={row_limit} reached — partial dump")
                    self.log.info("FAA registry row limit (%d) reached", row_limit)
                    break

            if not relations:
                if rows_in_member == 0:
                    detail = "no rows parsed — the dump layout may have changed"
                elif usable_rows == 0:
                    detail = (
                        f"{rows_in_member} row(s) parsed but none carried a registration and a registrant — "
                        "check FAA_COLUMN_ALIASES against the current header"
                    )
                else:
                    detail = ""
                if detail:
                    warnings.append(detail)
                    self.log.warning("FAA registry member %s: %s", member, detail)
                    self.stats.bump_source(self.spec.id, "errors")
                    self.stats.record_error(self.spec.id, f"{member}: {detail}")
                continue

            document = self.make_document(
                f"{url}#{member}",
                title=f"FAA aircraft registry — {member}",
                text=_summarise_rows(entities, relations),
                content_type="text/csv",
                external_id=f"faa-{member}-{row_limit}",
                entities=list(entities.values()),
                relations=relations,
                extra={"faa_member": member, "faa_rows": rows_in_member, "faa_warnings": warnings},
            )
            documents += 1
            yield document

        # Shared-address pass: distinct registrants at one normalised address.
        shared = self._shared_address_documents(address_groups)
        for document in shared:
            documents += 1
            yield document

        self._record_state(url, state, documents=documents, rows=rows_seen)
        self.log.info("FAA registry: %d row(s) parsed → %d document(s)", rows_seen, documents)

    def _shared_address_documents(self, groups: dict[str, dict[str, Any]]) -> Iterator[Document]:
        """``(:Person)-[:SHARES_ADDRESS]->(:Person)`` for co-registered addresses.

        Capped on both sides: an address with 500 registrants (a registered
        agent's office) would produce a quadratic edge explosion, so groups
        above ``max_group_size`` are summarised instead of paired, and each
        address contributes at most ``max_pairs`` edges.
        """
        min_group = int(self.option("min_shared_address_size", 2))
        max_group = int(self.option("max_shared_address_size", 40))
        max_pairs = int(self.option("max_pairs_per_address", 24))
        if min_group < 2:
            return

        entities: dict[str, Entity] = {}
        relations: list[Relation] = []
        lines = ["FAA registry — registrants sharing a normalised address"]

        for key in sorted(groups):
            group = groups[key]
            owners: dict[str, Entity] = group.get("owners") or {}
            place = group.get("place")
            tails = list(group.get("tails") or [])
            if len(owners) < min_group or place is None:
                continue

            entities[place.canonical_key] = place
            people = sorted((owner for owner in owners.values() if owner.entity_type is EntityType.PERSON), key=lambda o: o.name)
            for owner in owners.values():
                entities[owner.canonical_key] = owner

            if len(owners) > max_group:
                # Too big to pair: record the address as an aggregation point so
                # the graph still shows that N registrants converge here.
                for owner in list(owners.values())[:max_pairs]:
                    relations.append(self.located_in(owner, place, evidence=f"one of {len(owners)} registrants at this address ({len(tails)} aircraft)"))
                lines.append(f"{place.name}: {len(owners)} registrants / {len(tails)} aircraft (group too large to pair)")
                continue

            pairs = 0
            for index, left in enumerate(people):
                for right in people[index + 1 :]:
                    if pairs >= max_pairs:
                        break
                    shared_tails = ", ".join(sorted(set(tails))[:6])
                    relations.append(
                        self.relation(
                            left,
                            RelationType.SHARES_ADDRESS,
                            right,
                            evidence=f"FAA registry: {left.name} and {right.name} share the registrant address {place.name} ({shared_tails})",
                            extra={"shared_aircraft": sorted(set(tails))[:16]},
                        )
                    )
                    entities[left.canonical_key] = left
                    entities[right.canonical_key] = right
                    pairs += 1
                if pairs >= max_pairs:
                    break
            if pairs:
                lines.append(f"{place.name}: {pairs} SHARES_ADDRESS edge(s) across {len(people)} individual registrant(s)")

        if not relations:
            return
        yield self.make_document(
            f"{self.spec.id}://shared-addresses",
            title="FAA registry — shared registrant addresses",
            text="\n".join(lines)[:400_000],
            content_type="text/plain",
            external_id=f"faa-shared-addresses-{len(relations)}",
            entities=list(entities.values()),
            relations=relations,
            extra={"faa_shared_addresses": len(relations)},
        )


# --------------------------------------------------------------------------- #
# 2. ADS-B Exchange / adsbdb
# --------------------------------------------------------------------------- #
class AdsbExchangeAdapter(AviationAdapter):
    """Per-tail aircraft enrichment from ADS-B Exchange data."""

    adapter_name = "adsb"

    def harvest(self) -> Iterator[Document]:
        tails = _normalise_tails(self.option("tail_numbers") or self.settings.aircraft_tail_numbers or ())
        callsigns = [str(item).strip() for item in (self.option("callsigns") or ()) if str(item).strip()]
        if not tails and not callsigns:
            self.log.info("no tail numbers or callsigns configured (AIRCRAFT_TAIL_NUMBERS) — skipping")
            return

        for target in [*tails, *callsigns]:
            if self.ctx.budget_exhausted():
                return
            document = self._lookup(target)
            if document is not None:
                yield document

    # ------------------------------------------------------------------ #
    def _lookup(self, target: str) -> Document | None:
        payload = self._from_adsbdb(target)
        origin = "adsbdb"
        if not payload and self._adsbexchange_configured():
            payload = self._from_adsbexchange(target)
            origin = "adsbexchange"
        if not payload:
            self.log.info("no ADS-B record for %s", target)
            return None

        record = _flatten_adsb_record(payload)
        tail = normalize_tail(str(record.get("reg") or record.get("tail") or target))
        if not tail:
            return None

        aircraft = self.aircraft_entity(
            tail,
            properties=_compact(
                {
                    "icao_hex": record.get("hex"),
                    "serial_number": record.get("serial") or record.get("cn"),
                    "manufacturer": record.get("manufacturer") or record.get("manu"),
                    "model": record.get("model") or record.get("type"),
                    "aircraft_type": record.get("t") or record.get("type_description"),
                    "year": record.get("year"),
                    "engines": record.get("engines"),
                    "registry_country": record.get("owner_country") or record.get("reg_owner_country"),
                    "operator": record.get("own_op") or record.get("operator"),
                    "last_seen_utc": record.get("last_seen_utc") or record.get("last_seen"),
                    "adsb_source": origin,
                }
            ),
        )
        entities: dict[str, Entity] = {aircraft.canonical_key: aircraft}
        relations: list[Relation] = []

        owner_name = str(record.get("registered_owner") or record.get("ownop") or record.get("owner") or "").strip()
        owner: Entity | None = None
        if owner_name and len(owner_name) > 1:
            owner = self.owner_entity(
                owner_name,
                properties=_compact({"website": record.get("registered_owner_website") or record.get("website")}),
            )
            entities[owner.canonical_key] = owner
            aircraft.properties["owner"] = owner.name
            relations.append(
                self.ownership(
                    owner,
                    aircraft,
                    evidence=f"{origin}: {tail} registered to {owner.name}",
                    evidence_score=0.95,
                    extra={"adsb_source": origin},
                )
            )
            relations.append(
                self.relation(
                    aircraft,
                    RelationType.REGISTERED_TO,
                    owner,
                    evidence=f"{origin}: {tail} registration holder {owner.name}",
                    evidence_score=0.95,
                    extra={"adsb_source": origin},
                )
            )

        location_text = str(record.get("registered_owner_location") or record.get("owner_location") or record.get("location") or "").strip()
        if location_text and owner is not None:
            place = self.place_entity(location_text, properties=_compact({"country": record.get("owner_country")}) )
            if place is not None:
                entities[place.canonical_key] = place
                relations.append(self.located_in(owner, place, evidence=f"{origin}: {owner.name} registrant location {location_text[:160]}"))

        country = str(record.get("owner_country") or record.get("reg_owner_country") or "").strip()
        if country:
            country_place = self.country_entity(country)
            if country_place is not None:
                entities[country_place.canonical_key] = country_place
                relations.append(self.registered_in(aircraft, country_place, evidence=f"{origin}: {tail} registry country {country}"))

        if not relations:
            self.log.info("ADS-B record for %s carried no owner information", tail)
            return None

        lines = [f"ADS-B record — {tail}", f"Source: {origin}", f"Type: {record.get('model') or record.get('type') or 'unknown'}"]
        if owner is not None:
            lines.append(f"Registered owner: {owner.name}")
        if location_text:
            lines.append(f"Owner location: {location_text}")

        return self.make_document(
            f"{self.spec.id}://{tail.lower()}",
            title=f"ADS-B — {tail}",
            text="\n".join(lines),
            content_type="application/json",
            external_id=f"adsb-{tail}-{origin}",
            entities=list(entities.values()),
            relations=relations,
            extra={"adsb_source": origin, "tail_number": tail},
        )

    # ------------------------------------------------------------------ #
    def _adsbexchange_configured(self) -> bool:
        return bool(self.settings.adsbexchange_api_key)

    def _from_adsbdb(self, target: str) -> dict[str, Any] | None:
        base = str(self.settings.adsbdb_endpoint or "https://www.adsbdb.com/api/v1").rstrip("/")
        kind = "callsign" if looks_like_tail_number(target) or re.fullmatch(r"[A-Z0-9]{2,8}", target.upper()) else "hex"
        for path in ({f"/callsign/{target}", f"/registration/{target}"} if kind == "callsign" else {f"/hex/{target}"}):
            payload = self.fetch_json(f"{base}{path}", mode="bot", headers={"Accept": "application/json"})
            if isinstance(payload, dict) and (payload.get("hex") or payload.get("reg") or payload.get("response")):
                return payload.get("response") if isinstance(payload.get("response"), dict) else payload
        return None

    def _from_adsbexchange(self, target: str) -> dict[str, Any] | None:
        base = str(self.settings.adsbexchange_endpoint or "https://adsbexchange-com1.p.rapidapi.com").rstrip("/")
        headers = {
            "x-rapidapi-key": str(self.settings.adsbexchange_api_key),
            "x-rapidapi-host": str(self.settings.rapidapi_host or "adsbexchange-com1.p.rapidapi.com"),
            "Accept": "application/json",
        }
        endpoint = f"/v2/tail/{target}" if looks_like_tail_number(target) else f"/v2/registration/{target}"
        payload = self.fetch_json(f"{base}{endpoint}", mode="bot", headers=headers)
        if not isinstance(payload, dict):
            return None
        aircraft = payload.get("ac")
        if isinstance(aircraft, list) and aircraft:
            first = aircraft[0]
            return first if isinstance(first, dict) else None
        return payload if payload.get("reg") or payload.get("hex") else None


# --------------------------------------------------------------------------- #
# 3. Flight logs / passenger manifests
# --------------------------------------------------------------------------- #
class FlightLogAdapter(AviationAdapter):
    """Passenger manifests → ``(:Person)-[:PASSENGER_ON]->(:Aircraft)``."""

    adapter_name = "flight_logs"

    #: Columns that hold a passenger name in a tabular manifest.
    NAME_COLUMNS: tuple[str, ...] = (
        "passenger", "passengers", "passenger name", "passenger_name", "name", "names",
        "full name", "fullname", "person", "persons", "occupant", "occupants", "guest",
        "guests", "pax", "manifest", "individual", "individuals", "traveler", "traveller",
    )
    #: Columns that hold the aircraft identity.
    TAIL_COLUMNS: tuple[str, ...] = (
        "tail", "tail number", "tail_number", "tailnumber", "registration", "reg",
        "aircraft", "aircraft registration", "aircraft_registration", "aircraft tail",
        "aircraft tail number", "n number", "n_number", "nnum",
    )
    DATE_COLUMNS: tuple[str, ...] = ("date", "flight date", "flight_date", "departed", "departure date", "timestamp", "day")
    ROUTE_COLUMNS: tuple[str, ...] = (
        "from", "to", "origin", "destination", "departure", "arrival", "route",
        "depart", "arrive", "dep", "arr", "from airport", "to airport",
    )

    def harvest(self) -> Iterator[Document]:
        urls = _urls(self.option("flight_log_urls") or self.settings.flight_log_urls or ())
        if not urls:
            self.log.info("no flight logs configured (FLIGHT_LOG_URLS) — skipping")
            return
        for url in urls:
            if self.ctx.budget_exhausted():
                return
            document = self._harvest_log(url)
            if document is not None:
                yield document

    # ------------------------------------------------------------------ #
    def _harvest_log(self, url: str) -> Document | None:
        result = self.fetch(url, mode="bot", accept="*/*")
        if not result.ok:
            self.stats.bump_source(self.spec.id, "errors")
            self.stats.record_error(self.spec.id, f"flight log fetch failed: {result.status} {result.error}")
            self.log.warning("could not fetch flight log %s (%s)", url, result.error or result.status)
            return None

        body = result.content or (result.text or "").encode("utf-8", "replace")
        raw_text = result.text or body.decode("utf-8", "replace")
        extracted = extract_text(body, content_type=result.content_type, url=url)

        # A delimited manifest has to be read from the raw payload: routing CSV
        # through the text extractor rewrites it as ``col=value`` prose and the
        # columns are gone. PDF and HTML manifests are the opposite case —
        # extraction is what makes them tabular at all — so both candidates are
        # tried, raw first.
        # No minimum length here: a three-line manifest is a perfectly good
        # manifest. _parse_tabular_manifest rejects prose on its own terms (it
        # needs a header row with a name column), and the word floor below only
        # guards the unstructured fallback, where a stub really is useless.
        for candidate in dict.fromkeys([raw_text, extracted.text or ""]):
            text = str(candidate or "")
            if not text.strip():
                continue
            document = self._parse_tabular_manifest(text, url=url)
            if document is not None:
                return document

        text = extracted.text or raw_text
        if len(text.split()) < 12:
            self.log.info("flight log %s produced no usable text (warnings=%s)", url, extracted.warnings)
            self.stats.bump_source(self.spec.id, "errors")
            return None
        return self._unstructured_log(text, url=url, content_type=result.content_type)

    def _parse_tabular_manifest(self, text: str, *, url: str) -> Document | None:
        """Delimited manifest with a name column → structured PASSENGER_ON edges.

        Returns ``None`` when the payload is not tabular, or is tabular but has
        no recognisable name column — the caller then treats it as prose.
        """
        delimiter = _sniff_delimiter(text, single_column_headers=self.NAME_COLUMNS)
        if delimiter is None:
            return None
        lines = [line for line in text.splitlines() if line.strip()]
        rows = list(iter_delimited_rows(iter(lines), delimiter=delimiter))
        if len(rows) < 1:
            return None

        header = {canonical for canonical in rows[0] if canonical}
        name_column = _first_column(header, self.NAME_COLUMNS)
        if not name_column:
            return None
        tail_column = _first_column(header, self.TAIL_COLUMNS)
        date_column = _first_column(header, self.DATE_COLUMNS)
        route_columns = [column for column in self.ROUTE_COLUMNS if _canonical(column) in header]

        entities: dict[str, Entity] = {}
        relations: list[Relation] = []
        warnings: list[str] = []
        skipped = 0
        default_tail = _normalise_tails(self.option("default_tail_numbers") or ())[0] if self.option("default_tail_numbers") else ""

        for index, row in enumerate(rows):
            if self.ctx.budget_exhausted():
                warnings.append("budget exhausted — partial manifest")
                break
            name = str(row.get(name_column) or "").strip()
            if not name or _is_non_person(name):
                skipped += 1
                continue

            tail = ""
            if tail_column:
                tail = normalize_tail(str(row.get(tail_column) or ""))
            if not tail:
                tail = _find_tail_in(" ".join(str(value) for value in row.values())) or default_tail
            if not tail:
                skipped += 1
                continue

            aircraft = self.aircraft_entity(tail, properties={"source_log": url[:300]})
            entities[aircraft.canonical_key] = aircraft
            person = self._person_from_manifest(name)
            if person is None:
                skipped += 1
                continue
            entities[person.canonical_key] = person

            evidence_bits = [f"row {index + 2} of {os.path.basename(url) or 'flight log'}: {name} aboard {tail}"]
            if date_column and str(row.get(date_column) or "").strip():
                evidence_bits.append(f"date {str(row.get(date_column)).strip()[:40]}")
            for column in route_columns[:2]:
                value = str(row.get(_canonical(column)) or "").strip()
                if value:
                    evidence_bits.append(f"{column} {value[:60]}")

            relations.append(
                self.relation(
                    person,
                    RelationType.PASSENGER_ON,
                    aircraft,
                    evidence="; ".join(evidence_bits)[:1000],
                    evidence_score=0.95,
                    method=ExtractionMethod.STRUCTURED,
                    extra={"flight_log": url[:300], "manifest_row": index + 2},
                )
            )

            owner_name = str(row.get("owner") or row.get("owner_name") or row.get("operator") or "").strip()
            if owner_name and not _is_non_person(owner_name):
                owner = self.owner_entity(owner_name)
                entities[owner.canonical_key] = owner
                relations.append(self.ownership(owner, aircraft, evidence=f"flight log {os.path.basename(url)}: {owner_name} operates {tail}"))

        if not relations:
            if skipped:
                warnings.append(f"{skipped} manifest row(s) had no usable name+tail pair")
            return None

        # Co-passengers: two people on the same aircraft in the same log share a
        # flight, which is the movement analogue of a shared address.
        if self.option("link_copassengers", True):
            relations.extend(self._copassenger_relations(entities, relations, url=url))

        lines = [
            f"Flight log — {os.path.basename(url) or url}",
            f"Manifest rows parsed: {len(rows)}",
            f"PASSENGER_ON edges: {len(relations)}",
            f"Aircraft: {', '.join(sorted({a.name for a in entities.values() if a.entity_type is EntityType.CRAFT})[:12])}",
        ]
        return self.make_document(
            url,
            title=f"Flight log — {os.path.basename(url) or url}"[:200],
            text="\n".join(lines),
            content_type="text/csv",
            external_id=f"flightlog-{_stable(url)}",
            entities=list(entities.values()),
            relations=relations,
            extra={"flight_log": url, "manifest_rows": len(rows), "warnings": warnings, "skipped_rows": skipped},
        )

    def _copassengers(self, relations: Sequence[Relation]) -> dict[str, list[Entity]]:
        by_craft: dict[str, list[Entity]] = defaultdict(list)
        for relation in relations:
            if relation.predicate is RelationType.PASSENGER_ON:
                by_craft[relation.obj.canonical_key].append(relation.subject)
        return by_craft

    def _copassenger_relations(self, entities: dict[str, Entity], relations: Sequence[Relation], *, url: str) -> list[Relation]:
        """``(:Person)-[:TRAVELED_WITH]->(:Person)`` for co-passengers."""
        out: list[Relation] = []
        max_pairs = int(self.option("max_copassenger_pairs", 60))
        for craft_key, passengers in self._copassengers(relations).items():
            craft = entities.get(craft_key)
            unique = sorted({p.canonical_key: p for p in passengers}.values(), key=lambda p: p.name)
            pairs = 0
            for index, left in enumerate(unique):
                for right in unique[index + 1 :]:
                    if pairs >= max_pairs:
                        return out
                    out.append(
                        self.relation(
                            left,
                            RelationType.TRAVELED_WITH,
                            right,
                            evidence=f"flight log {os.path.basename(url) or url}: both aboard {craft.name if craft else craft_key}",
                            evidence_score=0.9,
                            method=ExtractionMethod.STRUCTURED,
                            extra={"craft": craft.name if craft else craft_key},
                        )
                    )
                    pairs += 1
        return out

    def _person_from_manifest(self, name: str) -> Entity | None:
        cleaned = clean_registrant_name(name)
        if not cleaned or _is_non_person(cleaned):
            return None
        if looks_corporate(cleaned):
            # A manifest cell holding a company is a charter operator, not a passenger.
            return None
        surface = humanise_name(_HONORIFIC_STRIP_RE.sub("", cleaned).strip())
        tokens = [token for token in surface.split() if token]
        if len(tokens) < 2:
            return None
        # Two real words minimum: "John A McDonald" passes on its first and last
        # name, "J R" does not. Single-letter tokens are kept — middle initials
        # are normal in manifests — but they cannot carry the name alone.
        substantive = [token for token in tokens if len(re.sub(r"[^A-Za-z]", "", token)) >= 2]
        if len(substantive) < 2:
            return None
        return self.entity(surface, EntityType.PERSON, properties={"role": "passenger"})

    def _unstructured_log(self, text: str, *, url: str, content_type: str) -> Document:
        """No tabular structure: hand the text to the NLP pipeline instead."""
        tails = sorted({normalize_tail(match.text) for match in _craft_matches(text) if match.text})
        self.log.info(
            "flight log %s is unstructured (%d word(s)); passing to NLP%s",
            url,
            len(text.split()),
            f" with {len(tails)} aircraft reference(s)" if tails else "",
        )
        return self.make_document(
            url,
            title=f"Flight log — {os.path.basename(url) or url}"[:200],
            text=text,
            content_type=content_type or "text/plain",
            external_id=f"flightlog-{_stable(url)}",
            extra={"flight_log": url, "unstructured": True, "aircraft_references": tails[:32]},
        )


# --------------------------------------------------------------------------- #
# Module helpers
# --------------------------------------------------------------------------- #
_CRAFT_DETECTOR = CraftDetector()


def _craft_matches(text: str) -> list[Any]:
    """Aircraft/tail references in unstructured log text (for provenance only)."""
    return _CRAFT_DETECTOR.find_all(text or "")


def _is_master_member(filename: str) -> bool:
    """Only MASTER.txt (and obvious renames) matters from the FAA ZIP."""
    lower = filename.lower()
    return "master" in lower or lower.endswith((".csv", ".tsv", ".txt"))


def normalize_tail(value: str) -> str:
    """Canonical aircraft registration: ``" 123AB"`` → ``"N123AB"``, ``"9h vuc"`` → ``"9H-VUC"``.

    Two conventions have to survive:

    * **US (FAA)** — the registry stores N-numbers *without* the leading ``N``
      and space-padded to five characters (``"123AB"``, ``" 707WA"``), so the
      ``N`` is restored and the padding dropped. This is the single most common
      way the dump gets mis-parsed.
    * **Foreign (ICAO)** — ``9H-VUC``, ``G-EUPA``, ``VP-BBF`` keep the hyphen
      that separates the national prefix from the letters, because dropping it
      would collide with FAA forms and make the same airframe two nodes.
    """
    text = re.sub(r"\s+", "", str(value or "").strip().upper())
    if not text:
        return ""
    text = re.sub(r"[^A-Z0-9-]", "", text)
    if not text:
        return ""

    # US forms: with or without the leading N, hyphen or not.
    if re.fullmatch(r"N-?\d{1,5}[A-Z]{0,3}", text):
        return text.replace("-", "")
    if re.fullmatch(r"\d{1,5}[A-Z]{0,3}", text):
        return f"N{text}"

    # Foreign ICAO forms: prefix + optional hyphen + body.
    match = re.fullmatch(r"([A-Z0-9]{1,2})-?([A-Z0-9]{1,5})", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return text[:9]


def _normalise_tails(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in values or ():
        tail = normalize_tail(value)
        if tail and tail not in out:
            out.append(tail)
    return out


def _urls(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in values or ():
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def clean_registrant_name(name: str) -> str:
    """Normalise an ALL-CAPS registry name into a readable surface form."""
    cleaned = re.sub(r"\s+", " ", str(name or "")).strip()
    cleaned = re.sub(r"^(?:REGISTRANT|OWNER|NAME)\s*[:\-]\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" ,;:-")


#: Legal-form initialisms that must survive title-casing as acronyms.
#: "WELLS FARGO BANK NA TRUSTEE" → "Wells Fargo Bank NA Trustee", not "Na".
_ACRONYM_TOKENS: frozenset[str] = frozenset(
    {
        "llc", "plc", "lp", "llp", "oao", "ojsc", "pjsc", "jsc", "cjsc", "zao", "ooo", "pao",
        "nv", "bv", "ag", "sa", "na", "kg", "srl", "sas", "sarl", "spa", "as", "asa", "ab",
        "oy", "oyj", "hf", "ehf", "ans", "pte", "pty", "pvt", "sdn", "bhd", "eood", "uab",
        "sia", "kft", "doo", "d.o.o", "s.r.l", "s.a", "b.v", "n.v", "icao", "imo", "us",
        "usa", "uk", "eu", "una", "snc", "scs", "sca",
    }
)
#: Forms with a canonical mixed-case spelling.
_CAMEL_TOKENS: dict[str, str] = {"gmbh": "GmbH", "kgag": "KGaA", "ab": "AB", "oy": "Oy"}


def humanise_name(name: str) -> str:
    """``"JOHN A SMITH"`` → ``"John A Smith"``, keeping ``Mc``/``O'`` intact.

    Names that already carry mixed case are returned untouched: another source
    (OpenCorporates, Wikidata) chose that spelling deliberately.
    """
    text = str(name or "").strip()
    if not text or not text.isupper():
        return text
    titled = text.title()
    titled = re.sub(r"\bMc([A-Za-z])", lambda m: f"Mc{m.group(1).upper()}", titled)
    titled = re.sub(r"\bMac([A-Za-z])", lambda m: f"Mac{m.group(1).upper()}", titled)
    titled = re.sub(r"\bO'([A-Za-z])", lambda m: f"O'{m.group(1).upper()}", titled)
    return titled


def humanise_org_name(name: str) -> str:
    """``"BLUE SKY AVIATION LLC"`` → ``"Blue Sky Aviation LLC"``."""
    text = humanise_name(name)
    if text == str(name or "").strip():
        return text  # already mixed case — leave the source's spelling alone

    def _restore(match: re.Match[str]) -> str:
        lowered = match.group(0).lower()
        return _CAMEL_TOKENS.get(lowered, lowered.upper())

    return re.sub(r"\b[A-Za-z][A-Za-z.']*\b", lambda m: _restore(m) if m.group(0).lower() in _ACRONYM_TOKENS else m.group(0), text)


def looks_corporate(name: str) -> bool:
    """True for a registrant that is a company, trust or fleet operator."""
    text = str(name or "").strip()
    if not text:
        return False
    if has_organizational_marker(text):
        return True
    padded = f" {re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()} "
    return any(f" {marker} " in padded for marker in _CORPORATE_MARKERS)


def _is_non_person(name: str) -> bool:
    """Reject header cells, placeholders and role labels found in manifests."""
    text = re.sub(r"\s+", " ", str(name or "")).strip().lower()
    if not text:
        return True
    if text in _NAME_STOPWORDS:
        return True
    if re.fullmatch(r"[\W\d_]+", text):
        return True
    if text.startswith(("page ", "sheet ", "table ", "column ", "row ", "total", "subtotal")):
        return True
    return len(text) < 3


def _join_address(row: dict[str, str]) -> str:
    parts = [
        first_present(row, ("street",)),
        first_present(row, ("street2",)),
        first_present(row, ("city",)),
        first_present(row, ("state",)),
        first_present(row, ("zip",)),
        first_present(row, ("country",)),
    ]
    # The *display* form keeps the registry's own casing (FAA writes addresses in
    # ALL CAPS); comparison happens on ``address_key``, which place_entity
    # derives from the normalised form.
    return ", ".join(part for part in parts if part)


def _other_names(row: dict[str, str]) -> list[str]:
    values = [str(row.get(column) or "").strip() for column in FAA_COLUMN_ALIASES["other_names"]]
    return [value for value in values if value and not _is_non_person(value)][:9]


def _compact(properties: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in properties.items() if value not in (None, "", [], {})}


def _summarise_rows(entities: dict[str, Entity], relations: Sequence[Relation]) -> str:
    """Short provenance text — the structured edges carry the facts."""
    aircraft = sorted({entity.name for entity in entities.values() if entity.entity_type is EntityType.CRAFT})
    owners = sorted({entity.name for entity in entities.values() if entity.entity_type is EntityType.PERSON})
    companies = sorted({entity.name for entity in entities.values() if entity.entity_type is EntityType.ORGANIZATION})
    lines = [
        "FAA aircraft registry extract",
        f"Aircraft: {len(aircraft)}",
        f"Individual registrants: {len(owners)}",
        f"Corporate registrants: {len(companies)}",
        f"Relations: {len(relations)}",
        "",
        "Sample registrations:",
    ]
    lines += [f"- {tail}" for tail in aircraft[:40]]
    return "\n".join(lines)


def _sniff_delimiter(text: str, *, single_column_headers: Sequence[str] = ()) -> str | None:
    """Return the delimiter when the first lines look tabular, else ``None``.

    Deliberately permissive — a two-column manifest (``Passenger,Tail``) has one
    delimiter per line — because the header-name check in the caller is what
    actually rejects prose: a news article has no "passenger" column.

    A delimiter-free payload still counts as tabular when its first line *is* a
    known manifest header (a name-only manifest, where the aircraft comes from
    ``default_tail_numbers``); prose never opens with the bare word "Passenger".
    """
    lines = [line for line in text.splitlines() if line.strip()][:6]
    if len(lines) < 2:
        return None
    for delimiter in ("\t", ";", "|", ","):
        counts = [line.count(delimiter) for line in lines]
        if counts[0] >= 1 and all(count >= 1 for count in counts) and max(counts) - min(counts) <= 1:
            return delimiter
    if single_column_headers and all(char not in line for line in lines for char in ",;\t|"):
        first = canonical_column(lines[0])
        if first in {canonical_column(candidate) for candidate in single_column_headers}:
            return ","
    return None


def _canonical(value: str) -> str:
    return canonical_column(value)


def _first_column(header: Iterable[str], candidates: Sequence[str]) -> str:
    present = set(header)
    for candidate in candidates:
        canonical = _canonical(candidate)
        if canonical in present:
            return canonical
    return ""


def _find_tail_in(text: str) -> str:
    match = re.search(r"\bN\d{1,5}[A-Z]{0,3}\b|\b[A-Z]{1,2}-[A-Z0-9]{1,5}\b", str(text or "").upper())
    return normalize_tail(match.group(0)) if match else ""


def _fold_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key or "").lower()).strip("_")


def _flatten_adsb_record(payload: dict[str, Any]) -> dict[str, Any]:
    """adsbdb and ADSBx v2 use different key names for the same facts.

    ADS-B Exchange answers in camelCase (``ownOp``, ``dstCall``) inside a
    ``{"ac": [...]}`` envelope, adsbdb in snake_case inside ``{"response": ...}``.
    Keys are folded to ``own_op``-style before aliasing so one table covers both,
    and nested objects are lifted into the same flat namespace.
    """
    if not isinstance(payload, dict):
        return {}
    source: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            for inner_key, inner_value in value.items():
                source.setdefault(_fold_key(inner_key), inner_value)
        else:
            source.setdefault(_fold_key(key), value)
    record: dict[str, Any] = dict(source)
    for canonical, aliases in (
        ("reg", ("reg", "registration", "tail", "n_number")),
        ("hex", ("hex", "icao_hex", "icao", "modes_hex")),
        ("manufacturer", ("manufacturer", "manu", "mfr", "type_manufacturer")),
        ("model", ("model", "type", "aircraft_type", "ac_type")),
        ("registered_owner", ("registered_owner", "own_op", "ownop", "owner", "registrant", "registeredowner")),
        ("registered_owner_location", ("registered_owner_location", "owner_location", "registrant_location", "location")),
        ("owner_country", ("reg_owner_country", "owner_country", "country", "reg_country")),
        ("serial", ("serial_number", "serial", "cn", "construction_number")),
        ("year", ("year", "year_mfr", "built")),
        ("engines", ("engines", "no_engines", "engine")),
        ("last_seen_utc", ("last_seen_utc", "last_seen", "lastseen")),
    ):
        for alias in aliases:
            if record.get(canonical):
                break
            if source.get(alias):
                record[canonical] = source[alias]
                break
    return record


def _stable(value: str) -> str:
    import hashlib

    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]
