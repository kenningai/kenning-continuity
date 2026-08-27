"""evolve_knowledge records a reason, and a chain stays a line, against a real Neo4j.

Two guards, both living in Cypher and so invisible to the mocked unit tests.

The reason. A description says what the entity is; why it changed belongs
elsewhere, or the next reader inherits the argument instead of the fact.
`revision_why` is required, and it lands on the EVOLVED_FROM edge, where every
reader of the chain finds it.

The line. The model says a node has at most one successor (`v0.2.0.tql`). The
tool keeps that by locking the head in the transaction that writes, and
re-checking it under the lock. The race below is staged, not hoped for: one
transaction holds the head while the second is shown to be blocked.
"""
import asyncio

import pytest
import pytest_asyncio

pytest.importorskip("testcontainers.neo4j")

from neo4j import AsyncGraphDatabase, RoutingControl  # noqa: E402
from testcontainers.neo4j import Neo4jContainer  # noqa: E402

from kenning_continuity.substrate import (  # noqa: E402
    KnowledgeEntity,
    TemporalKnowledgeSubstrate,
)
from kenning_continuity.utils import load_cypher  # noqa: E402

NEO4J_IMAGE = "neo4j:5.26-community"

SEED = """
CREATE (d:Domain {name: 'Example Ops'})
CREATE (s:Session {session_id: 'sess-1', timestamp: datetime('2026-10-01T00:00:00Z'),
                   purpose: 'seed', summary: '', status: 'active'})
CREATE (s2:Session {session_id: 'sess-2', timestamp: datetime('2026-10-02T00:00:00Z'),
                    purpose: 'second look', summary: '', status: 'active'})
CREATE (p:Person {name: 'Bob Example', source_key: 'bob.example@example.com'})
CREATE (q:Person {name: 'Carol Example', source_key: 'carol.example@example.com'})
CREATE (p)-[:DIRECTED {method: 'platform-stated'}]->(s)
CREATE (q)-[:DIRECTED {method: 'platform-stated'}]->(s2)
CREATE (d)-[:HAS_SESSION]->(s)
CREATE (d)-[:HAS_SESSION]->(s2)

// A chain revised before v0.2.0: the edge exists and carries no reason.
CREATE (old1:Knowledge:Event {name: 'Legacy Outage', ont_type: 'Event',
        description: 'first account', t_created: datetime('2026-09-01T00:00:00Z')})
CREATE (old2:Knowledge:Event {name: 'Legacy Outage', ont_type: 'Event',
        description: 'second account', t_created: datetime('2026-09-02T00:00:00Z')})
CREATE (old2)-[:EVOLVED_FROM]->(old1)
CREATE (s)-[:DISCOVERED]->(old1)
CREATE (s)-[:DISCOVERED]->(old2)

// Two separate chains whose heads share a name in one domain.
CREATE (t1:Knowledge:Insight {name: 'Twin', ont_type: 'Insight', description: 'one'})
CREATE (t2:Knowledge:Insight {name: 'Twin', ont_type: 'Insight', description: 'two'})
CREATE (s)-[:DISCOVERED]->(t1)
CREATE (s)-[:DISCOVERED]->(t2)
"""


@pytest.fixture(scope="module")
def seeded_neo4j():
    with Neo4jContainer(NEO4J_IMAGE) as container:
        with container.get_driver() as sync_driver:
            sync_driver.execute_query(SEED)
            seeded = sync_driver.execute_query(
                "MATCH (n:Knowledge) RETURN count(n) AS n").records[0]["n"]
        # A seed that did not land makes every refusal below a false pass.
        assert seeded == 4
        yield container.get_connection_url(), container.password


@pytest_asyncio.fixture
async def substrate(seeded_neo4j):
    uri, password = seeded_neo4j
    driver = AsyncGraphDatabase.driver(uri, auth=("neo4j", password))
    try:
        yield TemporalKnowledgeSubstrate(driver)
    finally:
        await driver.close()


async def _scalar(substrate, query, **params):
    r = await substrate.driver.execute_query(
        query, params, routing_control=RoutingControl.READ)
    return r.records[0][0]


async def _create(substrate, name, description="v1", session="sess-1"):
    await substrate.create_knowledge(session, [KnowledgeEntity(
        name=name, type="Event", domain="Example Ops", description=description)])


async def _chain_length(substrate, name):
    return await _scalar(
        substrate, "MATCH (n:Knowledge {name: $name}) RETURN count(n)", name=name)


class TestTheReasonIsRequired:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("why", ["", "   ", "\n\t"])
    async def test_an_evolve_without_a_reason_is_refused(self, substrate, why):
        name = f"Unexplained {len(why)}"
        await _create(substrate, name)
        with pytest.raises(ValueError, match="revision_why is required"):
            await substrate.evolve_knowledge(
                "sess-1", name, "v2", revision_why=why, domain="Example Ops")
        assert await _chain_length(substrate, name) == 1, "nothing was written"

    @pytest.mark.asyncio
    async def test_an_evolve_with_a_reason_is_accepted(self, substrate):
        await _create(substrate, "Explained")
        r = await substrate.evolve_knowledge(
            "sess-2", "Explained", "v2",
            revision_why="the outage lasted 9h 15m, not the 2h first reported",
            domain="Example Ops")
        assert r["evolved"] is True
        assert await _chain_length(substrate, "Explained") == 2


class TestTheReasonLivesOnTheEdge:
    @pytest.mark.asyncio
    async def test_the_edge_carries_the_reason_and_not_the_session(self, substrate):
        await _create(substrate, "Edge Reason")
        await substrate.evolve_knowledge(
            "sess-2", "Edge Reason", "v2", revision_why="ticket number corrected",
            domain="Example Ops")
        edge = await _scalar(
            substrate,
            "MATCH (:Knowledge {name: $name})-[e:EVOLVED_FROM]->() RETURN properties(e)",
            name="Edge Reason")
        assert edge == {"revision_why": "ticket number corrected"}, (
            "the revising session is one hop away through DISCOVERED; "
            "a copy on the edge is a denormalization")

    @pytest.mark.asyncio
    async def test_the_revising_session_is_reached_by_walk(self, substrate):
        await _create(substrate, "Walked")
        await substrate.evolve_knowledge(
            "sess-2", "Walked", "v2", revision_why="r", domain="Example Ops")
        director = await _scalar(
            substrate,
            "MATCH (p:Person)-[:DIRECTED]->(:Session)-[:DISCOVERED]->(new:Knowledge {name: $name})"
            "-[:EVOLVED_FROM]->() RETURN p.name",
            name="Walked")
        assert director == "Carol Example"

    @pytest.mark.asyncio
    async def test_the_predecessor_is_left_as_it_was(self, substrate):
        await _create(substrate, "Untouched", description="as first said")
        before = await _scalar(
            substrate, "MATCH (n:Knowledge {name: $name}) RETURN properties(n)",
            name="Untouched")
        await substrate.evolve_knowledge(
            "sess-2", "Untouched", "v2", revision_why="r", domain="Example Ops")
        after = await _scalar(
            substrate,
            "MATCH (n:Knowledge {name: $name}) WHERE (n)<-[:EVOLVED_FROM]-() "
            "RETURN properties(n)",
            name="Untouched")
        assert after == before, "the lock property must not survive the transaction"


class TestChainHistoryReturnsTheReasons:
    @pytest.mark.asyncio
    async def test_each_step_carries_its_reason_and_author(self, substrate):
        await _create(substrate, "Storied", description="first")
        await substrate.evolve_knowledge(
            "sess-2", "Storied", "second", revision_why="why two",
            domain="Example Ops")
        await substrate.evolve_knowledge(
            "sess-1", "Storied", "third", revision_why="why three",
            domain="Example Ops")

        steps = await substrate.get_chain_history("Storied", "Example Ops")

        assert [s.description for s in steps] == ["first", "second", "third"]
        assert [s.revision_why for s in steps] == [None, "why two", "why three"]
        assert [[a.director for a in s.discovered_by] for s in steps] == [
            ["Bob Example"], ["Carol Example"], ["Bob Example"]]

    @pytest.mark.asyncio
    async def test_a_pre_v0_2_0_revision_comes_back_without_a_reason(self, substrate):
        """No reason was recorded, and none is supplied."""
        steps = await substrate.get_chain_history("Legacy Outage", "Example Ops")
        assert [s.description for s in steps] == ["first account", "second account"]
        assert [s.revision_why for s in steps] == [None, None]


class TestAChainIsALine:
    @pytest.mark.asyncio
    async def test_a_non_head_cannot_be_evolved(self, substrate):
        await _create(substrate, "Moved On")
        await substrate.evolve_knowledge(
            "sess-1", "Moved On", "v2", revision_why="r", domain="Example Ops")
        await substrate.evolve_knowledge(
            "sess-1", "Moved On", "v3", revision_why="r", domain="Example Ops")
        forks = await _scalar(
            substrate,
            "MATCH (o:Knowledge {name: $name})<-[:EVOLVED_FROM]-(n) "
            "WITH o, count(n) AS c WHERE c > 1 RETURN count(o)",
            name="Moved On")
        assert forks == 0
        assert await _chain_length(substrate, "Moved On") == 3

    @pytest.mark.asyncio
    async def test_two_heads_by_one_name_are_refused_not_picked(self, substrate):
        with pytest.raises(ValueError, match="Refusing to pick one"):
            await substrate.evolve_knowledge(
                "sess-1", "Twin", "three", revision_why="r", domain="Example Ops")
        assert await _chain_length(substrate, "Twin") == 2

    @pytest.mark.asyncio
    async def test_a_concurrent_evolve_waits_for_the_lock_then_refuses(self, substrate):
        """Staged, not hoped for.

        Transaction A takes the head and writes its successor but does not
        commit. Evolve B starts and must be BLOCKED — that is the lock. When A
        commits, B must find the head gone and refuse.

        Both halves are needed, and this test was run against each removed:
        without the lock, B runs past A and the `not b.done()` assertion fails;
        with the lock but without the head re-check under it, B waits, then
        writes a second successor anyway, and `pytest.raises` fails.
        """
        name = "Contended"
        await _create(substrate, name)

        async with substrate.driver.session() as session:
            tx_a = await session.begin_transaction()
            head = await tx_a.run(load_cypher("knowledge_evolve_head"),
                                  {"name": name, "domain": "Example Ops"})
            eid = (await head.single())["eid"]
            await tx_a.run(
                load_cypher("knowledge_evolve", ont_label="Event",
                            new_labels="Knowledge:Event", basis_clause="",
                            sources_clause="", t_valid_clause=""),
                {"old_eid": eid, "domain": "Example Ops", "new_description": "A's",
                 "revision_why": "A", "ont_type": "Event", "session_id": "sess-1"})

            b = asyncio.create_task(substrate.evolve_knowledge(
                "sess-2", name, "B's", revision_why="B", domain="Example Ops"))
            await asyncio.sleep(1.0)
            assert not b.done(), "B must wait on A's lock, not run past it"

            await tx_a.commit()

        with pytest.raises(ValueError, match="another session evolved it first"):
            await b

        assert await _chain_length(substrate, name) == 2
        assert await _scalar(
            substrate,
            "MATCH (n:Knowledge {name: $name}) WHERE n._evolve_lock IS NOT NULL "
            "RETURN count(n)", name=name) == 0
        steps = await substrate.get_chain_history(name, "Example Ops")
        assert [s.revision_why for s in steps] == [None, "A"]
