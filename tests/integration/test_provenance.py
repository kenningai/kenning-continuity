"""Every read returns the provenance the writes record, against a real Neo4j.

`basis` rides the DISCOVERED edge and `sources` the node, and both had been
written since v0.1.0 and read by nothing. An agent that audited another
agent's ingest through get_domain_state concluded the provenance was missing
when it was not: the view could not show it, so it could only ever answer no.

So the gate here is two-sided. It must show provenance where it was recorded,
and show null where it was not — including on a revision that recorded none,
which must not inherit its predecessor's. A view that always says yes is as
blind as one that always says no.
"""
import pytest
import pytest_asyncio

pytest.importorskip("testcontainers.neo4j")

from neo4j import AsyncGraphDatabase  # noqa: E402
from testcontainers.neo4j import Neo4jContainer  # noqa: E402

from kenning_continuity.substrate import (  # noqa: E402
    Basis,
    KnowledgeEntity,
    TemporalKnowledgeSubstrate,
)

NEO4J_IMAGE = "neo4j:5.26-community"
DOMAIN = "Example Ops"

SEED = """
CREATE (d:Domain {name: 'Example Ops'})
CREATE (s:Session {session_id: 'sess-1', timestamp: datetime('2026-10-01T00:00:00Z'),
                   purpose: 'ingest', summary: '', status: 'active'})
CREATE (s2:Session {session_id: 'sess-2', timestamp: datetime('2026-10-02T00:00:00Z'),
                    purpose: 'audit', summary: '', status: 'active'})
CREATE (p:Person {name: 'Bob Example', source_key: 'bob.example@example.com'})
CREATE (q:Person {name: 'Carol Example', source_key: 'carol.example@example.com'})
CREATE (p)-[:DIRECTED {method: 'platform-stated'}]->(s)
CREATE (q)-[:DIRECTED {method: 'platform-stated'}]->(s2)
CREATE (d)-[:HAS_SESSION]->(s)
CREATE (d)-[:HAS_SESSION]->(s2)

// Written before basis existed: no basis on the edge, no sources on the node.
// 3,163 of 5,890 DISCOVERED edges on one production graph look like this
// (2026-10-21).
CREATE (u:Knowledge:Event {name: 'Unsourced Legacy', ont_type: 'Event',
        description: 'predates provenance', t_created: datetime('2026-09-01T00:00:00Z')})
CREATE (s)-[:DISCOVERED]->(u)

// merge_knowledge residue: two DISCOVERED edges on one version. On that
// graph all 11 such nodes agree (no basis on either); this one disagrees, so
// the reads are shown not to pick one.
CREATE (m:Knowledge:Event {name: 'Merged Legacy', ont_type: 'Event',
        description: 'merged', t_created: datetime('2026-09-02T00:00:00Z')})
CREATE (s)-[:DISCOVERED {basis: 'stated'}]->(m)
CREATE (s2)-[:DISCOVERED {basis: 'inferred'}]->(m)
"""

FIRST = ["TM: Example > 1. Announcements | Example Chat — Example, Bob, 2026-09-22 11:10 UTC"]
SECOND = ["TM: Example > 3. Bugs and errors | Example Chat — Example, Carol, 2026-10-21 13:12 UTC"]


@pytest.fixture(scope="module")
def seeded_neo4j():
    with Neo4jContainer(NEO4J_IMAGE) as container:
        with container.get_driver() as sync_driver:
            sync_driver.execute_query(SEED)
            sync_driver.execute_query(
                "CREATE FULLTEXT INDEX knowledge_index IF NOT EXISTS "
                "FOR (n:Actor|Structure|Artifact|Event|Insight|Pattern|"
                "Challenge|Solution|Lesson|Rationale) ON EACH [n.name, n.description]"
            )
            sync_driver.execute_query("CALL db.awaitIndex('knowledge_index')")
            seeded = sync_driver.execute_query(
                "MATCH (n:Knowledge) RETURN count(n) AS n").records[0]["n"]
        # A seed that did not land makes every null below a false pass.
        assert seeded == 2
        yield container.get_connection_url(), container.password


@pytest_asyncio.fixture
async def substrate(seeded_neo4j):
    uri, password = seeded_neo4j
    driver = AsyncGraphDatabase.driver(uri, auth=("neo4j", password))
    try:
        yield TemporalKnowledgeSubstrate(driver)
    finally:
        await driver.close()


async def _create(substrate, name, basis=None, sources=None):
    await substrate.create_knowledge("sess-1", [KnowledgeEntity(
        name=name, type="Event", domain=DOMAIN, description=f"{name} v1",
        basis=basis, sources=sources)])


async def _state(substrate, name):
    state = await substrate.get_domain_state(DOMAIN)
    [entity] = [e for e in state.entities if e.name == name]
    return entity


async def _found(substrate, name):
    found = await substrate.search_knowledge(f'"{name}"', domain=DOMAIN)
    [entity] = [e for e in found.entities if e.name == name]
    return entity


class TestTheCurrentViewShowsWhatWasRecorded:
    @pytest.mark.asyncio
    async def test_domain_state_returns_basis_and_sources(self, substrate):
        await _create(substrate, "Sourced Fact", Basis.STATED, FIRST)
        entity = await _state(substrate, "Sourced Fact")
        assert entity.basis == Basis.STATED
        assert entity.sources == FIRST

    @pytest.mark.asyncio
    async def test_search_returns_basis_and_sources(self, substrate):
        await _create(substrate, "Searched Fact", Basis.READ_FROM_DOCUMENT, FIRST)
        entity = await _found(substrate, "Searched Fact")
        assert entity.basis == Basis.READ_FROM_DOCUMENT
        assert entity.sources == FIRST


class TestTheCurrentViewSaysNoWhereNothingWasRecorded:
    @pytest.mark.asyncio
    async def test_domain_state_returns_null_for_legacy(self, substrate):
        entity = await _state(substrate, "Unsourced Legacy")
        assert entity.basis is None
        assert entity.sources is None

    @pytest.mark.asyncio
    async def test_search_returns_null_for_legacy(self, substrate):
        entity = await _found(substrate, "Unsourced Legacy")
        assert entity.basis is None
        assert entity.sources is None

    @pytest.mark.asyncio
    async def test_null_is_sent_not_dropped(self, substrate):
        """An absent key and a null read the same to a careless reader and
        differently to a careful one. The payload says null explicitly."""
        state = await substrate.get_domain_state(DOMAIN)
        payload = state.model_dump(mode="json")
        [legacy] = [e for e in payload["entities"] if e["name"] == "Unsourced Legacy"]
        assert "basis" in legacy and legacy["basis"] is None
        assert "sources" in legacy and legacy["sources"] is None

    @pytest.mark.asyncio
    async def test_conflicting_bases_are_not_resolved_by_picking_one(self, substrate):
        entity = await _state(substrate, "Merged Legacy")
        assert entity.basis is None


class TestARevisionShowsItsOwnProvenance:
    @pytest.mark.asyncio
    async def test_the_head_carries_the_revisions_provenance(self, substrate):
        await _create(substrate, "Revised Fact", Basis.READ_FROM_DOCUMENT, FIRST)
        await substrate.evolve_knowledge(
            "sess-2", "Revised Fact", "Revised Fact v2",
            revision_why="the outage was on the other provider",
            domain=DOMAIN, basis=Basis.INFERRED, sources=SECOND)
        entity = await _state(substrate, "Revised Fact")
        assert entity.basis == Basis.INFERRED
        assert entity.sources == SECOND

    @pytest.mark.asyncio
    async def test_a_revision_that_records_none_does_not_inherit(self, substrate):
        """Per-version, not inherited: a head showing its predecessor's sources
        would claim the revision drew on something it never cited."""
        await _create(substrate, "Bare Revision", Basis.STATED, FIRST)
        await substrate.evolve_knowledge(
            "sess-2", "Bare Revision", "Bare Revision v2",
            revision_why="reworded", domain=DOMAIN)
        entity = await _state(substrate, "Bare Revision")
        assert entity.basis is None
        assert entity.sources is None


class TestTheChainShowsEachVersionsProvenance:
    @pytest.mark.asyncio
    async def test_every_step_carries_its_own(self, substrate):
        await _create(substrate, "Chained Fact", Basis.READ_FROM_DOCUMENT, FIRST)
        await substrate.evolve_knowledge(
            "sess-2", "Chained Fact", "Chained Fact v2",
            revision_why="a later post corrected the date",
            domain=DOMAIN, basis=Basis.STATED, sources=SECOND)
        first, second = await substrate.get_chain_history("Chained Fact", DOMAIN)

        assert first.sources == FIRST
        assert [a.basis for a in first.discovered_by] == [Basis.READ_FROM_DOCUMENT]
        assert first.revision_why is None

        assert second.sources == SECOND
        assert [a.basis for a in second.discovered_by] == [Basis.STATED]
        assert second.revision_why == "a later post corrected the date"

    @pytest.mark.asyncio
    async def test_legacy_steps_read_as_null(self, substrate):
        [step] = await substrate.get_chain_history("Unsourced Legacy", DOMAIN)
        assert step.sources is None
        assert [a.basis for a in step.discovered_by] == [None]

    @pytest.mark.asyncio
    async def test_conflicting_bases_are_each_listed(self, substrate):
        [step] = await substrate.get_chain_history("Merged Legacy", DOMAIN)
        assert [a.basis for a in step.discovered_by] == [Basis.STATED, Basis.INFERRED]
