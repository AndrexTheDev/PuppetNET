"""Aviation source tests: FAA registry, ADS-B lookups and flight logs.

No network and no sockets: the registry dump is a local file in ``tmp_path``
(which is also how an operator feeds a dump they already downloaded), and the
ADS-B/flight-log endpoints are answered by a scripted fake client.

The invariants asserted here are the ones the graph depends on:

* an FAA row becomes ``(:Person|:Company)-[:OWNS]->(:Aircraft {tail_number})``
  at the official-register weight of **1.0**, never a person/company mix-up;
* two individual registrants at one normalised address become
  ``(:Person)-[:SHARES_ADDRESS]->(:Person)`` at **0.8** — and a registered
  agent's desk with hundreds of registrants does *not* become a quadratic
  edge explosion;
* a passenger manifest becomes ``(:Person)-[:PASSENGER_ON]->(:Aircraft)`` at
  **0.8**, while an unstructured log is handed to the NLP pass as text instead
  of being regex-guessed into people.
"""

from __future__ import annotations

import dataclasses
import json
import time
import zipfile
from collections.abc import Iterator
from typing import Any

import pytest

from puppetnet.models import Document, EntityType, IngestStats, RelationType, SourceSpec, SourceType
from puppetnet.net.proxy_client import FetchResult
from puppetnet.sources import AdapterContext, create_adapter
from puppetnet.sources.adsb import (
    AdsbExchangeAdapter,
    FaaRegistryAdapter,
    FlightLogAdapter,
    humanise_name,
    looks_corporate,
    normalize_tail,
)
from puppetnet.sources.registry import get_spec

# --------------------------------------------------------------------------- #
# Fakes & fixtures
# --------------------------------------------------------------------------- #

ADSBDB = "https://www.adsbdb.com/api/v1"
ADSBX = "https://adsbexchange-com1.p.rapidapi.com"


class FakeFetchClient:
    """Scripted stand-in for :class:`FetchClient` (request + stream_lines)."""

    def __init__(self, responses: dict[str, Any] | None = None, *, default: Any = None) -> None:
        self.responses = dict(responses or {})
        self.default = default
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def request(self, url: str, **kwargs: Any) -> FetchResult:
        self.calls.append((url, kwargs))
        scripted = self.responses.get(url, self.default)
        if isinstance(scripted, FetchResult):
            return scripted
        if isinstance(scripted, dict):
            return FetchResult(url=url, status=200, ok=True, **scripted)
        if scripted is None:
            return FetchResult(url=url, status=404, ok=False, error="not-scripted")
        raise AssertionError(f"unexpected scripted response for {url}: {scripted!r}")

    def stream_lines(self, url: str, **kwargs: Any) -> Iterator[str]:
        self.calls.append((url, kwargs))
        scripted = self.responses.get(url, self.default)
        if isinstance(scripted, FetchResult):
            yield from (scripted.text or "").splitlines()
            return
        raise AssertionError(f"stream_lines was not scripted for {url}")

    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]

    def kwargs_for(self, url: str) -> dict[str, Any]:
        for called_url, kwargs in self.calls:
            if called_url == url:
                return kwargs
        raise AssertionError(f"{url} was never requested")


def ok_json(url: str, payload: Any) -> FetchResult:
    body = json.dumps(payload)
    return FetchResult(url=url, status=200, ok=True, content_type="application/json", text=body, content=body.encode())


def ok_text(url: str, text: str, content_type: str = "text/csv") -> FetchResult:
    return FetchResult(url=url, status=200, ok=True, content_type=content_type, text=text, content=text.encode())


@pytest.fixture()
def env(settings, tmp_path):
    return dataclasses.replace(
        settings,
        dry_run=True,
        state_dir=str(tmp_path / "state"),
        report_dir=str(tmp_path / "reports"),
        sources_file=str(tmp_path / "none.yaml"),
        spacy_models=[],
        flight_log_urls=[],
        aircraft_tail_numbers=[],
    )


def context_for(env, spec: SourceSpec, client: Any = None) -> AdapterContext:
    return AdapterContext(
        settings=env,
        client=client if client is not None else FakeFetchClient(),
        stats=IngestStats(),
        spec=spec,
        run_id="run-aviation-test",
        deadline=time.monotonic() + 600,
    )


def registry_spec(**options: Any) -> SourceSpec:
    """The real registry spec, with test options merged in."""
    base = get_spec("faa_registry")
    assert base is not None
    return base.with_options(**options)


def harvest(adapter, env) -> list[Document]:
    return list(adapter.run(limit=int(adapter.spec.max_documents)))


# --------------------------------------------------------------------------- #
# FAA dump fixtures — the real column names, the real ALL-CAPS values
# --------------------------------------------------------------------------- #

#: Three registrants at one Brickell Avenue suite (a trust, an individual, an
#: LLC) plus a second individual at the same suite and one registrant elsewhere —
#: enough to exercise OWNS, LOCATED_IN and the person↔person SHARES_ADDRESS pass.
FAA_MASTER = """N_NUMBER,AIRCRAFT_SERIAL_NUMBER,MODE_S_CODE_HEX,AIRCRAFT_MFR_NAME,AIRCRAFT_MODEL_NAME,YEAR_MFR,REGISTRANT_BUSINESS_NAME,STREET,STREET2,CITY,STATE,ZIP_CODE,COUNTRY,AIR_WORTH_DATE,UNIQUE_ID,OTHER_NAMES1,OTHER_NAMES2
123AB,5678,A1B2C3,GULFSTREAM AEROSPACE,G650,2015,KERIMOV FAMILY TRUST,1200 BRICKELL AVE STE 900,,MIAMI,FL,33131,US,20150402,U1000,,
707WA,9911,D4E5F6,BOMBARDIER INC,CL600-2B16,2011,JOHN A MCDONALD,1200 BRICKELL AVE STE 900,,MIAMI,FL,33131,US,20110611,U1001,,
987CD,2233,A7B8C9,CIRRUS DESIGN,SR22,2019,MARIA GARCIA,44 OLD MILL ROAD,,STAMFORD,CT,06902,US,20190130,U1002,,
555EF,4455,D1D2D3,CESSNA AIRCRAFT,525,2008,BLUE SKY AVIATION LLC,1200 BRICKELL AVE STE 900,,MIAMI,FL,33131,US,20080915,U1003,,
666GH,6677,E1E2E3,PIPER AIRCRAFT,PA28,1999,IVAN PETROV,1200 BRICKELL AVE STE 900,,MIAMI,FL,33131,US,19990201,U1004,IVAN I PETROV,
"""

#: Same facts, different column names — a reshaped release must still parse.
FAA_RENAMED = """Tail Number,Registrant,Address,City,State,Zip Code,Country,Manufacturer,Model
123AB,KERIMOV FAMILY TRUST,1200 Brickell Ave Ste 900,Miami,FL,33131,US,GULFSTREAM AEROSPACE,G650
707WA,JOHN A MCDONALD,1200 Brickell Ave Ste 900,Miami,FL,33131,US,BOMBARDIER INC,CL600-2B16
"""

#: A registered agent's desk: far too many registrants to pair up.
AGENT_DESK_HEADER = "N_NUMBER,REGISTRANT_BUSINESS_NAME,STREET,CITY,STATE,ZIP_CODE,COUNTRY\n"
AGENT_DESK_ROWS = "".join(
    f"100{i:02d}A,PERSON {i:03d},1 CORPORATION TRUST CENTER,WILMINGTON,DE,19801,US\n" for i in range(60)
)


@pytest.fixture()
def faa_dump(tmp_path):
    path = tmp_path / "MASTER.txt"
    path.write_text(FAA_MASTER, encoding="utf-8")
    return str(path)


@pytest.fixture()
def faa_zip(tmp_path):
    path = tmp_path / "ReleasableAircraft.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("MASTER.txt", FAA_MASTER)
        archive.writestr("readme.html", "<html>not a dump</html>")
        archive.writestr("DEREGISTERED.txt", "ignore me\n")
    return str(path)


# --------------------------------------------------------------------------- #
# Registration normalisation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123AB", "N123AB"),          # FAA form: no leading N
        (" 707WA", "N707WA"),         # ... and space padding
        ("N707WA", "N707WA"),
        ("N-707WA", "N707WA"),
        ("n 123ab", "N123AB"),
        ("9H-VUC", "9H-VUC"),         # foreign ICAO keeps its hyphen
        ("9h vuc", "9H-VUC"),
        ("G-EUPA", "G-EUPA"),
        ("VP-BBF", "VP-BBF"),
        ("D-AIXX", "D-AIXX"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_tail_handles_both_conventions(raw, expected):
    assert normalize_tail(raw) == expected


def test_normalize_tail_never_collapses_a_foreign_registration_into_a_us_one():
    assert normalize_tail("9H-VUC") != normalize_tail("9VUC")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("KERIMOV FAMILY TRUST", True),
        ("BLUE SKY AVIATION LLC", True),
        ("WELLS FARGO BANK NA TRUSTEE", True),
        ("ACME HOLDINGS LTD", True),
        ("JOHN A MCDONALD", False),
        ("MARIA GARCIA", False),
        ("IVAN PETROV", False),
        ("Lincolnshire Farms", False),   # substring "inc" must not fire
        ("Prince Charles", False),
    ],
)
def test_looks_corporate_separates_vehicles_from_people(name, expected):
    assert looks_corporate(name) is expected


def test_humanise_name_keeps_scottish_and_irish_forms():
    assert humanise_name("JOHN A MCDONALD") == "John A McDonald"
    assert humanise_name("SEAN O'BRIEN") == "Sean O'Brien"
    assert humanise_name("Maria Garcia") == "Maria Garcia"


# --------------------------------------------------------------------------- #
# FAA registry adapter
# --------------------------------------------------------------------------- #


def test_faa_registry_maps_registrants_to_aircraft(env, faa_dump):
    spec = registry_spec(dump_url=faa_dump, row_limit=100)
    adapter = FaaRegistryAdapter(spec, context_for(env, spec))
    documents = harvest(adapter, env)

    assert documents, "the dump produced no documents"
    relations = [relation for document in documents for relation in document.relations]
    entities = {entity.canonical_key: entity for document in documents for entity in document.entities}

    owns = [r for r in relations if r.rel_type == RelationType.OWNS.value]
    assert len(owns) == 5, [r.evidence for r in owns]
    for relation in owns:
        assert relation.source_weight == pytest.approx(1.0)
        assert relation.method == "structured"
        assert relation.weight == pytest.approx(1.0), "OWNS is a domain weight-1.0 edge"

    aircraft = [entity for entity in entities.values() if entity.entity_type is EntityType.CRAFT]
    assert {a.name for a in aircraft} == {"N123AB", "N707WA", "N987CD", "N555EF", "N666GH"}
    gulfstream = next(a for a in aircraft if a.name == "N123AB")
    assert gulfstream.properties["tail_number"] == "N123AB"
    assert gulfstream.properties["craft_kind"] == "Aircraft"
    assert gulfstream.properties["owner"] == "Kerimov Family Trust"
    assert gulfstream.properties["model"] == "G650"

    people = {e.name: e for e in entities.values() if e.entity_type is EntityType.PERSON}
    assert "John A McDonald" in people and "Maria Garcia" in people
    companies = {e.name for e in entities.values() if e.entity_type is EntityType.ORGANIZATION}
    assert "Kerimov Family Trust" in companies and "Blue Sky Aviation LLC" in companies


def test_faa_registry_survives_a_reshaped_dump(env, tmp_path):
    path = tmp_path / "renamed.csv"
    path.write_text(FAA_RENAMED, encoding="utf-8")
    spec = registry_spec(dump_url=str(path))
    adapter = FaaRegistryAdapter(spec, context_for(env, spec))
    documents = harvest(adapter, env)

    relations = [r for d in documents for r in d.relations if r.rel_type == RelationType.OWNS.value]
    assert {r.subject.name for r in relations} == {"Kerimov Family Trust", "John A McDonald"}
    assert {r.obj.name for r in relations} == {"N123AB", "N707WA"}


def test_faa_registry_reads_only_master_txt_from_the_zip(env, faa_zip):
    spec = registry_spec(dump_url=faa_zip)
    adapter = FaaRegistryAdapter(spec, context_for(env, spec))
    documents = harvest(adapter, env)

    assert documents
    assert all("readme" not in (d.extra.get("faa_member") or "").lower() for d in documents)
    assert any("MASTER.txt" in (d.extra.get("faa_member") or "") for d in documents)


def test_shared_registrant_address_links_the_individuals(env, faa_dump):
    spec = registry_spec(dump_url=faa_dump)
    documents = harvest(FaaRegistryAdapter(spec, context_for(env, spec)), env)
    relations = [r for d in documents for r in d.relations]

    shared = [r for r in relations if r.rel_type == RelationType.SHARES_ADDRESS.value]
    assert shared, "no SHARES_ADDRESS edge for two registrants at one address"
    for relation in shared:
        assert relation.subject.entity_type is EntityType.PERSON
        assert relation.obj.entity_type is EntityType.PERSON
        assert relation.weight == pytest.approx(0.8), "SHARES_ADDRESS is a domain weight-0.8 edge"
        assert "1200 BRICKELL AVE" in relation.evidence.upper()
        assert relation.extra.get("shared_aircraft")

    pairs = {frozenset((r.subject.name, r.obj.name)) for r in shared}
    # McDonald and Petrov share the Brickell Ave registrant address; Garcia does
    # not (different address) and the trust/LLC are companies, not individuals.
    assert pairs == {frozenset(("John A McDonald", "Ivan Petrov"))}


def test_a_registered_agents_desk_is_summarised_not_squared(env, tmp_path):
    path = tmp_path / "agent.csv"
    path.write_text(AGENT_DESK_HEADER + AGENT_DESK_ROWS, encoding="utf-8")
    spec = registry_spec(dump_url=str(path), max_shared_address_size=10, max_pairs_per_address=5)
    adapter = FaaRegistryAdapter(spec, context_for(env, spec))
    documents = harvest(adapter, env)
    relations = [r for d in documents for r in d.relations]

    # 60 registrants at one address would be 1 770 pairs; the cap turns that
    # into a bounded set of "N registrants converge here" edges instead.
    shared = [r for r in relations if r.rel_type == RelationType.SHARES_ADDRESS.value]
    assert shared == []
    located = [r for r in relations if r.rel_type == RelationType.LOCATED_IN.value]
    assert any("one of 60 registrants" in r.evidence for r in located)
    assert len(located) <= 5 * 2 + 60 * 2


def test_faa_registry_filters_by_tail_number(env, faa_dump):
    # force_refresh: the three filter tests share a state directory, and the
    # monthly-dump cache would (correctly) skip the second and third.
    spec = registry_spec(dump_url=faa_dump, tail_numbers=["123AB", "N707WA"], force_refresh=True)
    documents = harvest(FaaRegistryAdapter(spec, context_for(env, spec)), env)
    tails = {r.obj.name for d in documents for r in d.relations if r.rel_type == RelationType.OWNS.value}
    assert tails == {"N123AB", "N707WA"}


def test_faa_registry_filters_by_state(env, faa_dump):
    spec = registry_spec(dump_url=faa_dump, states=["CT"], force_refresh=True)
    documents = harvest(FaaRegistryAdapter(spec, context_for(env, spec)), env)
    tails = {r.obj.name for d in documents for r in d.relations if r.rel_type == RelationType.OWNS.value}
    assert tails == {"N987CD"}


def test_faa_registry_filters_by_registrant_name(env, faa_dump):
    spec = registry_spec(dump_url=faa_dump, registrant_names=["petrov"], force_refresh=True)
    documents = harvest(FaaRegistryAdapter(spec, context_for(env, spec)), env)
    tails = {r.obj.name for d in documents for r in d.relations if r.rel_type == RelationType.OWNS.value}
    assert tails == {"N666GH"}


def test_a_garbled_dump_warns_instead_of_emitting_garbage(env, tmp_path):
    path = tmp_path / "broken.csv"
    path.write_text("SOMETHING,ELSE\n1,2\n", encoding="utf-8")
    spec = registry_spec(dump_url=path.as_posix())
    adapter = FaaRegistryAdapter(spec, context_for(env, spec))
    documents = harvest(adapter, env)

    assert documents == []
    assert adapter.ctx.stats.per_source[spec.id]["errors"] >= 1


def test_a_missing_dump_does_not_raise(env, tmp_path):
    spec = registry_spec(dump_url=str(tmp_path / "nope.csv"))
    adapter = FaaRegistryAdapter(spec, context_for(env, spec))
    assert harvest(adapter, env) == []


def test_the_monthly_dump_is_not_reparsed_within_the_refresh_window(env, faa_dump):
    spec = registry_spec(dump_url=faa_dump, refresh_days=30)
    first = harvest(FaaRegistryAdapter(spec, context_for(env, spec)), env)
    assert first

    # Second run: state says the dump was processed today, so nothing to do.
    second_adapter = FaaRegistryAdapter(spec, context_for(env, spec))
    assert harvest(second_adapter, env) == []

    # ... unless the operator forces it.
    forced = registry_spec(dump_url=faa_dump, refresh_days=30, force_refresh=True)
    assert harvest(FaaRegistryAdapter(forced, context_for(env, forced)), env)


def test_a_dump_that_yielded_nothing_is_retried_not_skipped(env, tmp_path, faa_dump):
    empty = tmp_path / "empty.csv"
    empty.write_text("N_NUMBER,REGISTRANT_BUSINESS_NAME\n", encoding="utf-8")
    spec = registry_spec(dump_url=empty.as_posix(), refresh_days=30)
    assert harvest(FaaRegistryAdapter(spec, context_for(env, spec)), env) == []

    # The failed attempt must not poison the state: point at a good dump and the
    # same URL/state pair still harvests.
    good = registry_spec(dump_url=faa_dump, refresh_days=30)
    assert harvest(FaaRegistryAdapter(good, context_for(env, good)), env)


def test_row_limit_stops_a_huge_dump(env, faa_dump):
    spec = registry_spec(dump_url=faa_dump, row_limit=2)
    documents = harvest(FaaRegistryAdapter(spec, context_for(env, spec)), env)
    owns = [r for d in documents for r in d.relations if r.rel_type == RelationType.OWNS.value]
    assert len(owns) == 2
    assert any("row_limit" in str(d.extra.get("faa_warnings")) for d in documents)


def test_registry_state_is_written_once_per_dump(env, faa_dump):
    spec = registry_spec(dump_url=faa_dump)
    harvest(FaaRegistryAdapter(spec, context_for(env, spec)), env)
    state_file = env.state_path / f"{spec.id}_state.json"
    assert state_file.exists()
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload[faa_dump]["documents"] >= 1
    assert payload[faa_dump]["rows"] == 5


# --------------------------------------------------------------------------- #
# ADS-B adapter
# --------------------------------------------------------------------------- #

ADSBDB_PAYLOAD = {
    "response": {
        "hex": "A9B2C3",
        "flight": "N707WA",
        "reg": "N707WA",
        "manufacturer": "Bombardier",
        "model": "CL-600-2B16 (Challenger 605)",
        "type": "GLF5",
        "year": "2011",
        "serial_number": "9911",
        "registered_owner": "KERIMOV FAMILY TRUST",
        "registered_owner_location": "1200 Brickell Ave Ste 900, Miami, FL 33131",
        "registered_owner_website": "https://example.test",
        "reg_owner_country": "United States",
        "engines": "2",
        "last_seen_utc": "2026-09-20T11:22:33Z",
    }
}

ADSBX_PAYLOAD = {
    "msg": "found 1 aircraft",
    "total": 1,
    "ctime": 1_700_000_000,
    "ac": [
        {
            "hex": "511ABC",
            "flight": "9HVUC ",
            "reg": "9H-VUC",
            "t": "GLF6",
            "ownOp": "GULFSTREAM LEASING LTD",
            "year": "2019",
            "cn": "6200",
            "pos": [35.9, 14.5],
        }
    ],
}


def adsb_spec(**options: Any) -> SourceSpec:
    base = get_spec("adsb_exchange")
    assert base is not None
    return base.with_options(**options)


def test_adsbdb_lookup_produces_owner_and_registry_edges(env):
    client = FakeFetchClient({f"{ADSBDB}/callsign/N707WA": ok_json(f"{ADSBDB}/callsign/N707WA", ADSBDB_PAYLOAD)})
    spec = adsb_spec(tail_numbers=["N707WA"])
    adapter = AdsbExchangeAdapter(spec, context_for(env, spec, client))
    documents = harvest(adapter, env)

    assert len(documents) == 1
    document = documents[0]
    assert document.source_weight == pytest.approx(0.8)
    relations = {r.rel_type for r in document.relations}
    assert {RelationType.OWNS.value, RelationType.REGISTERED_TO.value, RelationType.LOCATED_IN.value, RelationType.REGISTERED_IN.value} <= relations

    owner = next(e for e in document.entities if e.name == "Kerimov Family Trust")
    assert owner.entity_type is EntityType.ORGANIZATION
    aircraft = next(e for e in document.entities if e.entity_type is EntityType.CRAFT)
    assert aircraft.name == "N707WA"
    assert aircraft.properties["tail_number"] == "N707WA"
    assert aircraft.properties["owner"] == "Kerimov Family Trust"
    assert aircraft.properties["adsb_source"] == "adsbdb"

    place = next(e for e in document.entities if e.entity_type is EntityType.LOCATION and "Brickell" in e.name)
    assert place.properties["address_key"]
    assert place.properties["address"].startswith("1200 Brickell Ave"), "the display form keeps the source casing"


def test_adsbexchange_rapidapi_is_used_when_a_key_is_present(env, monkeypatch):
    monkeypatch.setattr(env, "adsbexchange_api_key", "test-key")
    monkeypatch.setattr(env, "adsbdb_endpoint", "")
    client = FakeFetchClient({f"{ADSBX}/v2/tail/9H-VUC": ok_json(f"{ADSBX}/v2/tail/9H-VUC", ADSBX_PAYLOAD)})
    spec = adsb_spec(tail_numbers=["9H-VUC"])
    adapter = AdsbExchangeAdapter(spec, context_for(env, spec, client))
    documents = harvest(adapter, env)

    assert len(documents) == 1
    headers = client.kwargs_for(f"{ADSBX}/v2/tail/9H-VUC").get("headers") or {}
    assert headers["x-rapidapi-key"] == "test-key"
    assert headers["x-rapidapi-host"] == "adsbexchange-com1.p.rapidapi.com"

    document = documents[0]
    aircraft = next(e for e in document.entities if e.entity_type is EntityType.CRAFT)
    assert aircraft.name == "9H-VUC", "a foreign registration must keep its hyphen"
    owner = next(e for e in document.entities if e.entity_type is EntityType.ORGANIZATION)
    assert owner.name == "Gulfstream Leasing Ltd"
    assert document.extra["adsb_source"] == "adsbexchange"


def test_the_paid_api_is_a_fallback_when_adsbdb_has_no_record(env, monkeypatch):
    monkeypatch.setattr(env, "adsbexchange_api_key", "test-key")
    client = FakeFetchClient({f"{ADSBX}/v2/tail/9H-VUC": ok_json(f"{ADSBX}/v2/tail/9H-VUC", ADSBX_PAYLOAD)})
    spec = adsb_spec(tail_numbers=["9H-VUC"])
    documents = harvest(AdsbExchangeAdapter(spec, context_for(env, spec, client)), env)

    assert len(documents) == 1
    assert documents[0].extra["adsb_source"] == "adsbexchange"
    # adsbdb was tried first and missed.
    assert any(url.startswith(ADSBDB) for url in client.urls())


def test_a_record_without_an_owner_produces_no_document(env):
    payload = {"response": {"hex": "A1B2C3", "reg": "N999ZZ", "model": "C172"}}
    client = FakeFetchClient({f"{ADSBDB}/callsign/N999ZZ": ok_json(f"{ADSBDB}/callsign/N999ZZ", payload)})
    spec = adsb_spec(tail_numbers=["N999ZZ"])
    assert harvest(AdsbExchangeAdapter(spec, context_for(env, spec, client)), env) == []


def test_an_unconfigured_watchlist_makes_no_requests(env):
    client = FakeFetchClient()
    spec = adsb_spec()
    assert harvest(AdsbExchangeAdapter(spec, context_for(env, spec, client)), env) == []
    assert client.urls() == []


def test_adsb_watchlist_comes_from_settings(env, monkeypatch):
    monkeypatch.setattr(env, "aircraft_tail_numbers", ["707WA", "N123AB"])
    client = FakeFetchClient(default=FetchResult(url="x", status=404, ok=False, error="nope"))
    spec = adsb_spec()
    harvest(AdsbExchangeAdapter(spec, context_for(env, spec, client)), env)
    assert f"{ADSBDB}/callsign/N707WA" in client.urls()
    assert f"{ADSBDB}/callsign/N123AB" in client.urls()


# --------------------------------------------------------------------------- #
# Flight logs
# --------------------------------------------------------------------------- #

MANIFEST_CSV = """Date,Tail Number,From,To,Passenger,Notes
2019-08-09,N707WA,TETERBORO,NICE,JEFFREY SMITH,charter
2019-08-09,N707WA,TETERBORO,NICE,MARIA GARCIA,
2019-08-09,N707WA,TETERBORO,NICE,Passenger,header repeat
2019-08-10,N707WA,NICE,PARIS,JOHN A MCDONALD,
2019-08-10,N123AB,NICE,PARIS,BLUE SKY AVIATION LLC,company on the manifest
2019-08-11,,,ANONYMOUS GUEST,no tail on this row
"""

MANIFEST_TSV = "Passenger\tTail\nIGOR SECHIN\tN707WA\nOLEG DERIPASKA\tN707WA\n"

UNSTRUCTURED_LOG = """FLIGHT LOG — UNDATED
Aircraft operated on behalf of the registrant departed Teterboro for Nice on the
ninth of August, arriving after dark. The cabin carried several guests whose
names were redacted by the court before release. Fuel was uplifted in Nice and
the crew remained overnight. No further detail is available in this exhibit.
"""


def flight_spec(**options: Any) -> SourceSpec:
    base = get_spec("flight_logs")
    assert base is not None
    return base.with_options(**options)


def test_a_tabular_manifest_becomes_passenger_on_edges(env):
    url = "https://example.test/manifest.csv"
    client = FakeFetchClient({url: ok_text(url, MANIFEST_CSV)})
    spec = flight_spec(flight_log_urls=[url])
    adapter = FlightLogAdapter(spec, context_for(env, spec, client))
    documents = harvest(adapter, env)

    assert len(documents) == 1
    document = documents[0]
    assert document.source_weight == pytest.approx(0.8)

    boarded = [r for r in document.relations if r.rel_type == RelationType.PASSENGER_ON.value]
    assert {r.subject.name for r in boarded} == {"Jeffrey Smith", "Maria Garcia", "John A McDonald"}
    for relation in boarded:
        assert relation.obj.name == "N707WA"
        assert relation.weight == pytest.approx(0.8), "PASSENGER_ON is a domain weight-0.8 edge"
        assert relation.method == "structured"
        assert "aboard N707WA" in relation.evidence
        assert "TETERBORO" in relation.evidence or "NICE" in relation.evidence
        assert relation.extra["evidence_score"] == pytest.approx(0.95)
        # 0.8 source weight × 1.0 structured method × 0.95 evidence.
        assert relation.confidence == pytest.approx(0.8 * 0.95)

    # "Passenger" (a repeated header) and the company row are not passengers;
    # the row with no tail cannot be attributed to an aircraft.
    assert all(r.subject.name != "Passenger" for r in document.relations)
    assert not any(r.subject.name == "Blue Sky Aviation LLC" for r in boarded)


def test_co_passengers_are_linked(env):
    url = "https://example.test/manifest.tsv"
    client = FakeFetchClient({url: ok_text(url, MANIFEST_TSV, content_type="text/tab-separated-values")})
    spec = flight_spec(flight_log_urls=[url])
    documents = harvest(FlightLogAdapter(spec, context_for(env, spec, client)), env)

    relations = [r for d in documents for r in d.relations]
    traveled = [r for r in relations if r.rel_type == RelationType.TRAVELED_WITH.value]
    assert len(traveled) == 1
    assert {traveled[0].subject.name, traveled[0].obj.name} == {"Igor Sechin", "Oleg Deripaska"}
    assert traveled[0].extra.get("craft") == "N707WA"
    assert len([r for r in relations if r.rel_type == RelationType.PASSENGER_ON.value]) == 2


def test_co_passenger_linking_can_be_switched_off(env):
    url = "https://example.test/manifest.tsv"
    client = FakeFetchClient({url: ok_text(url, MANIFEST_TSV, content_type="text/tab-separated-values")})
    spec = flight_spec(flight_log_urls=[url], link_copassengers=False)
    documents = harvest(FlightLogAdapter(spec, context_for(env, spec, client)), env)
    relations = [r for d in documents for r in d.relations]
    assert all(r.rel_type != RelationType.TRAVELED_WITH.value for r in relations)
    assert len(relations) == 2


def test_a_tail_inside_a_cell_is_still_attributed(env):
    url = "https://example.test/notes.csv"
    text = "Passenger,Details\nIVAN PETROV,Aboard N123AB from Nice\nMARIA GARCIA,flight cancelled\n"
    client = FakeFetchClient({url: ok_text(url, text)})
    spec = flight_spec(flight_log_urls=[url])
    documents = harvest(FlightLogAdapter(spec, context_for(env, spec, client)), env)

    boarded = [r for d in documents for r in d.relations if r.rel_type == RelationType.PASSENGER_ON.value]
    assert [(r.subject.name, r.obj.name) for r in boarded] == [("Ivan Petrov", "N123AB")]


def test_a_default_tail_rescues_rows_without_one(env):
    url = "https://example.test/simple.csv"
    client = FakeFetchClient({url: ok_text(url, "Passenger\nIGOR SECHIN\nOLEG DERIPASKA\n")})
    spec = flight_spec(flight_log_urls=[url], default_tail_numbers=["N707WA"])
    documents = harvest(FlightLogAdapter(spec, context_for(env, spec, client)), env)

    boarded = [r for d in documents for r in d.relations if r.rel_type == RelationType.PASSENGER_ON.value]
    assert {r.subject.name for r in boarded} == {"Igor Sechin", "Oleg Deripaska"}
    assert all(r.obj.name == "N707WA" for r in boarded)


def test_an_unstructured_log_is_handed_to_the_nlp_pass(env):
    url = "https://example.test/exhibit.txt"
    client = FakeFetchClient({url: ok_text(url, UNSTRUCTURED_LOG, content_type="text/plain")})
    spec = flight_spec(flight_log_urls=[url])
    documents = harvest(FlightLogAdapter(spec, context_for(env, spec, client)), env)

    assert len(documents) == 1
    document = documents[0]
    assert document.extra["unstructured"] is True
    assert document.relations == []
    assert document.entities == []
    assert "ninth of August" in document.text, "the prose must reach the NLP engine intact"


def test_a_log_that_names_its_aircraft_records_the_reference(env):
    url = "https://example.test/exhibit2.txt"
    text = (
        "The charter aircraft 9H-VUC departed Teterboro on Friday. Court staff confirmed the "
        "manifest had been sealed, and no passenger names were released with this exhibit."
    )
    client = FakeFetchClient({url: ok_text(url, text, content_type="text/plain")})
    spec = flight_spec(flight_log_urls=[url])
    documents = harvest(FlightLogAdapter(spec, context_for(env, spec, client)), env)

    assert documents[0].extra["unstructured"] is True
    assert "9H-VUC" in documents[0].extra["aircraft_references"]


def test_a_failed_log_fetch_counts_an_error(env):
    url = "https://example.test/missing.csv"
    client = FakeFetchClient({url: FetchResult(url=url, status=500, ok=False, error="boom")})
    spec = flight_spec(flight_log_urls=[url])
    adapter = FlightLogAdapter(spec, context_for(env, spec, client))
    assert harvest(adapter, env) == []
    assert adapter.ctx.stats.per_source[spec.id]["errors"] == 1


def test_no_configured_logs_means_no_requests(env):
    client = FakeFetchClient()
    spec = flight_spec()
    assert harvest(FlightLogAdapter(spec, context_for(env, spec, client)), env) == []
    assert client.urls() == []


def test_manifest_urls_come_from_settings(env, monkeypatch):
    url = "https://example.test/from-env.csv"
    monkeypatch.setattr(env, "flight_log_urls", [url])
    client = FakeFetchClient({url: ok_text(url, MANIFEST_TSV, content_type="text/tab-separated-values")})
    spec = flight_spec()
    documents = harvest(FlightLogAdapter(spec, context_for(env, spec, client)), env)
    assert documents
    assert client.urls() == [url]


@pytest.mark.parametrize(
    "name",
    ["Passenger", "Name", "TOTAL", "page 3", "Flight Department", "N/A", "", "  ", "123", "Sheet 1"],
)
def test_manifest_placeholders_never_become_people(env, name):
    url = "https://example.test/x.csv"
    text = f"Passenger,Tail\n{name},N707WA\n"
    client = FakeFetchClient({url: ok_text(url, text)})
    spec = flight_spec(flight_log_urls=[url])
    documents = harvest(FlightLogAdapter(spec, context_for(env, spec, client)), env)
    boarded = [r for d in documents for r in d.relations if r.rel_type == RelationType.PASSENGER_ON.value]
    assert boarded == [], f"{name!r} should not be a passenger"


# --------------------------------------------------------------------------- #
# Registry wiring
# --------------------------------------------------------------------------- #


def test_the_three_aviation_specs_are_registered():
    for source_id, adapter_name, weight in (("faa_registry", "faa_registry", 1.0), ("adsb_exchange", "adsb", 0.8), ("flight_logs", "flight_logs", 0.8)):
        spec = get_spec(source_id)
        assert spec is not None, source_id
        assert spec.adapter == adapter_name
        assert spec.kind is SourceType.STRUCTURED
        assert spec.confidence == pytest.approx(weight)


@pytest.mark.parametrize("source_id", ["faa_registry", "adsb_exchange", "flight_logs"])
def test_the_aviation_specs_instantiate_through_the_registry(env, source_id):
    spec = get_spec(source_id)
    adapter = create_adapter(spec, context_for(env, spec))
    assert isinstance(adapter, FaaRegistryAdapter | AdsbExchangeAdapter | FlightLogAdapter)
    assert adapter.source_weight == pytest.approx(spec.confidence)


def test_a_manifest_document_is_usable_by_the_pipeline(env):
    """The tabular path still emits text, so downstream text consumers see provenance."""
    url = "https://example.test/manifest.csv"
    client = FakeFetchClient({url: ok_text(url, MANIFEST_CSV)})
    spec = flight_spec(flight_log_urls=[url])
    documents = harvest(FlightLogAdapter(spec, context_for(env, spec, client)), env)
    document = documents[0]
    assert document.doc_id
    assert document.url == url
    assert "PASSENGER_ON" in document.text
    assert all(relation.doc_id == document.doc_id for relation in document.relations)
    assert all(document.doc_id in entity.doc_ids for entity in document.entities)


def test_aviation_entities_carry_doc_ids_and_source_provenance(env, faa_dump):
    spec = registry_spec(dump_url=faa_dump)
    documents = harvest(FaaRegistryAdapter(spec, context_for(env, spec)), env)
    for document in documents:
        for entity in document.entities:
            assert document.doc_id in entity.doc_ids, entity.name
            assert spec.id in entity.source_ids, entity.name
