"""Co-reference as an authored claim, against a real Neo4j.

The knowledge layer holds referents, not people. Seventeen "Alice Example"
nodes across eleven domains are seventeen acts of reference, correctly distinct
— so saying two of them are one person is a CLAIM someone makes in a session,
and it can be wrong.

Not mockable. Every guard here is a property of the MATCH: whether the endpoints
are head-of-chain, whether (name, domain) scoping stops a name living in eleven
domains from producing a cross product, and whether a process node can be an
endpoint. A mocked substrate would exercise the mock.
"""
import pytest
import pytest_asyncio

pytest.importorskip("testcontainers.neo4j")

from neo4j import AsyncGraphDatabase, RoutingControl  # noqa: E402
from testcontainers.neo4j import Neo4jContainer  # noqa: E402

from kenning_continuity.substrate import (  # noqa: E402
    Connection, ConnectionType, TemporalKnowledgeSubstrate,
)

NEO4J_IMAGE = "neo4j:5.26-community"

SEED = """
CREATE (d1:Domain {name: 'Example Ops'})
CREATE (d2:Domain {name: 'Telephony'})
CREATE (p:Person {name: 'Bob Example', source_key: 'bob.example@example.com'})
CREATE (:Person {name: 'Drifted Director', domain: 'Example Ops',
                 source_key: 'drifted@example.com'})
CREATE (s:Session {session_id: 'sess-1', timestamp: datetime(),
                   purpose: 'seed', summary: '', status: 'active'})
CREATE (s2:Session {session_id: 'sess-2', timestamp: datetime(),
                    purpose: 'seed telephony', summary: '', status: 'active'})
CREATE (p)-[:DIRECTED {method: 'platform-stated'}]->(s)
CREATE (p)-[:DIRECTED {method: 'platform-stated'}]->(s2)
CREATE (d1)-[:HAS_SESSION]->(s)
CREATE (d2)-[:HAS_SESSION]->(s2)

// Placed in a domain by the session that discovered them — no domain property.
// Three referents, and they must stay three. One name in two domains is the
// same-name-different-referent case; a second name in one domain is the
// two-referents-one-domain case. Collapsing any two of these into one name
// makes (name, domain) resolve to two head-of-chain nodes — a state
// create_knowledge refuses to produce, so only raw Cypher can reach it, and
// every claim test then resolves an endpoint it did not ask for.
CREATE (alice_ops:Knowledge:Actor:PersonReferent {name: 'Alice Example',
    ont_type: 'Actor', description: 'named in an Example Ops session', t_created: datetime()})
CREATE (alice_tel:Knowledge:Actor:PersonReferent {name: 'Alice Example',
    ont_type: 'Actor', description: 'named in a Telephony session', t_created: datetime()})
CREATE (carol_ops:Knowledge:Actor:PersonReferent {name: 'Carol Example',
    ont_type: 'Actor', description: 'a second referent in the same domain', t_created: datetime()})
CREATE (s)-[:DISCOVERED]->(alice_ops)
CREATE (s2)-[:DISCOVERED]->(alice_tel)
CREATE (s)-[:DISCOVERED]->(carol_ops)

// an evolved pair: the old node is not head-of-chain
CREATE (old:Knowledge:Structure:System {name: 'Ancient', ont_type: 'Structure',
    description: 'superseded', t_created: datetime()})
CREATE (new:Knowledge:Structure:System {name: 'Current', ont_type: 'Structure',
    description: 'the head', t_created: datetime()})
CREATE (new)-[:EVOLVED_FROM]->(old)
CREATE (s)-[:DISCOVERED]->(old)
CREATE (s)-[:DISCOVERED]->(new)
"""


@pytest.fixture(scope="module")
def seeded_neo4j():
    with Neo4jContainer(NEO4J_IMAGE) as container:
        with container.get_driver() as sync_driver:
            sync_driver.execute_query(SEED)
        yield container.get_connection_url(), container.password


@pytest_asyncio.fixture
async def substrate(seeded_neo4j):
    uri, password = seeded_neo4j
    driver = AsyncGraphDatabase.driver(uri, auth=("neo4j", password))
    try:
        yield TemporalKnowledgeSubstrate(driver)
    finally:
        await driver.close()


@pytest_asyncio.fixture(autouse=True)
async def no_leftover_claims(substrate):
    """Each test starts with no claims. They share one container, and a claim
    left by a neighbour would make a scoping assertion pass or fail for the
    wrong reason."""
    await substrate.driver.execute_query(
        "MATCH ()-[c:REFERENT_CLAIM]->() DELETE c",
        routing_control=RoutingControl.WRITE)
    yield


async def _count(substrate, query):
    r = await substrate.driver.execute_query(query, routing_control=RoutingControl.READ)
    return r.records[0][0]


async def _claim(substrate, source="Alice Example", source_domain="Example Ops",
                 target="Alice Example", target_domain="Telephony",
                 resolution="proposed"):
    return await substrate.claim_referent(
        session_id="sess-1", source=source, source_domain=source_domain,
        target=target, target_domain=target_domain, resolution=resolution)


class TestTheClaimIsAuthored:
    @pytest.mark.asyncio
    async def test_a_claim_carries_its_standing_and_its_author(self, substrate):
        r = await _claim(substrate)
        assert r["resolution"] == "proposed"
        assert r["session_id"] == "sess-1"

        edges = await _count(
            substrate,
            "MATCH (:Domain {name:'Example Ops'})-[:HAS_SESSION]->(:Session)"
            "-[:DISCOVERED]->(a:Knowledge:Actor {name:'Alice Example'}) "
            "MATCH (:Domain {name:'Telephony'})-[:HAS_SESSION]->(:Session)"
            "-[:DISCOVERED]->(b:Knowledge:Actor {name:'Alice Example'}) "
            "MATCH (a)-[c:REFERENT_CLAIM]->(b) RETURN count(DISTINCT c)")
        assert edges == 1

    @pytest.mark.asyncio
    async def test_the_claim_reaches_the_person_who_made_it(self, substrate):
        """A claim with no vector behind it is what this edge exists to prevent."""
        await _claim(substrate)
        director = await _count(
            substrate,
            "MATCH ()-[c:REFERENT_CLAIM]->() "
            "MATCH (p:Person)-[:DIRECTED]->(s:Session {session_id: c.session_id}) "
            "RETURN p.source_key")
        assert director == "bob.example@example.com"

    @pytest.mark.asyncio
    async def test_the_claims_time_is_not_stored(self, substrate):
        """It is the session's timestamp, one hop away."""
        await _claim(substrate)
        stray = await _count(
            substrate,
            "MATCH ()-[c:REFERENT_CLAIM]->() WHERE c.t_created IS NOT NULL "
            "RETURN count(c)")
        assert stray == 0

    @pytest.mark.asyncio
    async def test_re_adjudicating_updates_rather_than_duplicates(self, substrate):
        await _claim(substrate, resolution="proposed")
        r = await _claim(substrate, resolution="distinct")
        assert r["resolution"] == "distinct"
        assert await _count(
            substrate, "MATCH ()-[c:REFERENT_CLAIM]->() RETURN count(c)") == 1


class TestTheDenialSurvives:
    @pytest.mark.asyncio
    async def test_distinct_is_recordable(self, substrate):
        """Without a slot for NO, the denial evaporates and the agent re-asks."""
        r = await _claim(substrate, target="Carol Example", target_domain="Example Ops",
                         resolution="distinct")
        assert r["resolution"] == "distinct"

    @pytest.mark.asyncio
    async def test_an_unknown_resolution_is_refused(self, substrate):
        with pytest.raises(ValueError, match="Unknown resolution"):
            await _claim(substrate, resolution="maybe")

    @pytest.mark.asyncio
    async def test_a_blank_resolution_is_refused(self, substrate):
        with pytest.raises(ValueError, match="Unknown resolution"):
            await _claim(substrate, resolution="")


class TestTheClaimIsScoped:
    @pytest.mark.asyncio
    async def test_a_name_in_two_domains_does_not_cross_product(self, substrate):
        """'Alice Example' exists in Telephony AND Example Ops. The claim names one."""
        await _claim(substrate, target="Alice Example", target_domain="Telephony")
        telephony = await _count(
            substrate,
            "MATCH (:Domain {name: 'Telephony'})-[:HAS_SESSION]->(:Session)"
            "-[:DISCOVERED]->(t) MATCH ()-[c:REFERENT_CLAIM]->(t) "
            "RETURN count(DISTINCT c)")
        platops = await _count(
            substrate,
            "MATCH (:Domain {name: 'Example Ops'})-[:HAS_SESSION]->(:Session)"
            "-[:DISCOVERED]->(t {name: 'Alice Example'}) "
            "MATCH ()-[c:REFERENT_CLAIM]->(t) RETURN count(DISTINCT c)")
        assert (telephony, platops) == (1, 0)

    @pytest.mark.asyncio
    async def test_a_non_head_endpoint_is_refused(self, substrate):
        with pytest.raises(ValueError, match="head-of-chain"):
            await _claim(substrate, source="Ancient", source_domain="Example Ops",
                         target="Current", target_domain="Example Ops")

    @pytest.mark.asyncio
    async def test_an_unknown_endpoint_is_refused(self, substrate):
        with pytest.raises(ValueError, match="head-of-chain"):
            await _claim(substrate, target="Nobody At All", target_domain="Telephony")

    @pytest.mark.asyncio
    async def test_a_claim_against_itself_is_refused(self, substrate):
        with pytest.raises(ValueError, match="co-refers with itself"):
            await _claim(substrate, target="Alice Example", target_domain="Example Ops")

    @pytest.mark.asyncio
    async def test_a_process_node_cannot_be_an_endpoint(self, substrate):
        """A :Person is a vector. A referent cannot refer to it.

        The endpoint here carries a `domain` property, which a process Person
        should never have. That is deliberate: without it, domain scoping alone
        misses the node and the label exclusion is never exercised — the test
        would pass for a reason other than the guard it names.
        """
        with pytest.raises(ValueError, match="head-of-chain"):
            await _claim(substrate, target="Drifted Director", target_domain="Example Ops")
        assert await _count(
            substrate,
            "MATCH (:Person)-[c:REFERENT_CLAIM]-() RETURN count(c)") == 0


class TestCreateConnectionsRefusesIt:
    @pytest.mark.asyncio
    async def test_referent_claim_is_not_writable_without_a_resolution(self, substrate):
        with pytest.raises(ValueError, match="Use claim_referent"):
            await substrate.create_connections("sess-1", [Connection(
                source="Alice Example", source_domain="Example Ops",
                target="Alice Example", target_domain="Telephony",
                type=ConnectionType.REFERENT_CLAIM)])


class TestBasisRidesTheOccasion:
    """`basis` is a fact about how a discovery reached the graph, not about
    anyone's certainty. It belongs on DISCOVERED — the occasion — because the
    same entity discovered twice can arrive two different ways."""

    @pytest.mark.asyncio
    async def test_basis_lands_on_the_edge_not_the_node(self, substrate):
        from kenning_continuity.substrate import Basis, KnowledgeEntity
        await substrate.create_knowledge("sess-1", [KnowledgeEntity(
            name="Read From A Whitepaper", type="Insight", domain="Example Ops",
            description="ingested", basis=Basis.READ_FROM_DOCUMENT)])

        on_edge = await _count(
            substrate,
            "MATCH (:Session)-[d:DISCOVERED]->(:Knowledge:Insight {name:'Read From A Whitepaper'}) "
            "RETURN d.basis")
        assert on_edge == "read-from-document"

        on_node = await _count(
            substrate,
            "MATCH (n:Knowledge:Insight {name:'Read From A Whitepaper'}) "
            "WHERE n.basis IS NOT NULL RETURN count(n)")
        assert on_node == 0, "basis is a property of the occasion, not the entity"

    @pytest.mark.asyncio
    async def test_basis_is_optional(self, substrate):
        from kenning_continuity.substrate import KnowledgeEntity
        await substrate.create_knowledge("sess-1", [KnowledgeEntity(
            name="No Basis Given", type="Insight", domain="Example Ops", description="x")])
        unset = await _count(
            substrate,
            "MATCH (:Session)-[d:DISCOVERED]->(:Knowledge:Insight {name:'No Basis Given'}) "
            "WHERE d.basis IS NULL RETURN count(d)")
        assert unset == 1

    @pytest.mark.asyncio
    async def test_the_evolved_node_carries_its_own_basis(self, substrate):
        """A later encounter can be grounded differently than the first."""
        from kenning_continuity.substrate import Basis, KnowledgeEntity
        await substrate.create_knowledge("sess-1", [KnowledgeEntity(
            name="Grew Up", type="Insight", domain="Example Ops",
            description="first heard in the room", basis=Basis.STATED)])
        await substrate.evolve_knowledge(
            "sess-1", "Grew Up", "later confirmed against the doc",
            domain="Example Ops", basis=Basis.READ_FROM_DOCUMENT)

        bases = await _count(
            substrate,
            "MATCH (:Session)-[d:DISCOVERED]->(n:Knowledge:Insight {name:'Grew Up'}) "
            "RETURN apoc.coll.sort(collect(d.basis))"
            if False else
            "MATCH (:Session)-[d:DISCOVERED]->(n:Knowledge:Insight {name:'Grew Up'}) "
            "WITH d.basis AS b ORDER BY b RETURN collect(b)")
        assert bases == ["read-from-document", "stated"]

    @pytest.mark.asyncio
    async def test_confidence_cannot_be_written(self, substrate):
        """Removed in v0.8.0. A caller still sending it finds out."""
        from kenning_continuity.substrate import KnowledgeEntity
        with pytest.raises(Exception):
            KnowledgeEntity(name="Certain", type="Insight", domain="Example Ops",
                            description="x", confidence=0.95)
