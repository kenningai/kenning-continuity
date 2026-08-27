"""The connection lookup, against a real Neo4j.

This defect cannot be proven fixed by a mock. It lived entirely in Cypher —
`search_connections.cypher` matched `source.name IN $names AND target.name IN
$names`, and the whole suite of 80 unit tests passed both before and after the
fix, because none of them executes a query.

Two defects hid in those three lines, and this file gates both. Each is proven
ADVERSARIALLY FIRST: the seed is shown to actually contain the failing shape by
running the old name-matching predicate against it, so the fix cannot pass
vacuously on a graph that never had the problem.

    1. No head-of-chain filter. Every head shares its name with its own
       predecessors, so a chain's EVOLVED_FROM edges surfaced as self-loops.
       Measured live: 8 spurious rows on one `Alice Example` search.

    2. No domain scoping. An edge authored between two referents in one domain
       was reported between their same-named counterparts in another. Measured
       live: 9 fabricable pairs, each an edge authored between two referents in
       one domain and fabricable between their counterparts in another.

Cross-domain edges are legitimate — 138 are deliberately authored in the
production graph — so `test_authored_cross_domain_edge_survives` guards against
over-correcting into same-domain confinement.
"""
import pytest
import pytest_asyncio

pytest.importorskip("testcontainers.neo4j")

from neo4j import AsyncGraphDatabase, RoutingControl  # noqa: E402
from testcontainers.neo4j import Neo4jContainer  # noqa: E402

from kenning_continuity.substrate import (  # noqa: E402
    TemporalKnowledgeSubstrate,
)

NEO4J_IMAGE = "neo4j:5.26-community"

# The adversarial seed. Every shape here exists in the production graph.
#
#   "Ada"  is head-of-chain in D1 AND in D2, and in D1 it has a predecessor
#          (so a chain exists whose EVOLVED_FROM must not surface)
#   "Bo"   is head-of-chain in D1 AND in D2
#   the only authored knowledge edge is Ada(D1) -> Bo(D1)
#   plus one deliberate cross-domain edge Bo(D2) -> Cy(D3)
SEED = """
CREATE (d1:Domain {name: 'D1'}), (d2:Domain {name: 'D2'}), (d3:Domain {name: 'D3'})
CREATE (s:Session {session_id: 'sess-1',
                   timestamp: datetime(), purpose: 'seed', summary: '', status: 'active'})
CREATE (s2:Session {session_id: 'sess-2',
                    timestamp: datetime(), purpose: 'seed d2', summary: '', status: 'active'})
CREATE (s3:Session {session_id: 'sess-3',
                    timestamp: datetime(), purpose: 'seed d3', summary: '', status: 'active'})
CREATE (d1)-[:HAS_SESSION]->(s)
CREATE (d2)-[:HAS_SESSION]->(s2)
CREATE (d3)-[:HAS_SESSION]->(s3)

CREATE (ada_old:Knowledge:Actor:PersonReferent {name: 'Ada', ont_type: 'Actor',
                              description: 'first take', t_created: datetime()})
CREATE (ada:Knowledge:Actor:PersonReferent {name: 'Ada', ont_type: 'Actor',
                              description: 'current', t_created: datetime()})
CREATE (ada)-[:EVOLVED_FROM]->(ada_old)

CREATE (ada2:Knowledge:Actor:PersonReferent {name: 'Ada', ont_type: 'Actor',
                           description: 'a different Ada', t_created: datetime()})

CREATE (bo_old:Knowledge:Structure:System {name: 'Bo', ont_type: 'Structure',
                                 description: 'bo, first take', t_created: datetime()})
CREATE (bo:Knowledge:Structure:System {name: 'Bo', ont_type: 'Structure',
                                 description: 'bo, current', t_created: datetime()})
CREATE (bo)-[:EVOLVED_FROM]->(bo_old)

CREATE (bo2:Knowledge:Structure:System {name: 'Bo', ont_type: 'Structure',
                              description: 'bo in two', t_created: datetime()})
CREATE (cy:Knowledge:Structure:System {name: 'Cy', ont_type: 'Structure',
                              description: 'cy in three', t_created: datetime()})

// head -> head. Visible before the fix and after.
CREATE (ada)-[:INFORMING {t_created: datetime()}]->(bo)
// authored on a PREDECESSOR: stranded when Ada evolved
CREATE (ada_old)-[:REQUIRING {t_created: datetime()}]->(bo)
// pointing AT a predecessor: stranded when Bo evolved
CREATE (ada)-[:COMPOSING {t_created: datetime()}]->(bo_old)
// both ends stranded, AND resolves to the same triple as the head->head
// INFORMING above — proves dedup is by (source, target, type)
CREATE (ada_old)-[:INFORMING {t_created: datetime()}]->(bo_old)
// two types between one resolved pair, both authored, both must survive
CREATE (ada)-[:RECOGNITION {t_created: datetime()}]->(bo)

CREATE (bo2)-[:ENABLING  {t_created: datetime()}]->(cy)

// every knowledge node is placed by a discovering session — including the
// chain predecessors, which were discovered by the session that authored them
CREATE (s)-[:DISCOVERED]->(ada_old)
CREATE (s)-[:DISCOVERED]->(ada)
CREATE (s)-[:DISCOVERED]->(bo_old)
CREATE (s)-[:DISCOVERED]->(bo)
CREATE (s2)-[:DISCOVERED]->(ada2)
CREATE (s2)-[:DISCOVERED]->(bo2)
CREATE (s3)-[:DISCOVERED]->(cy)
"""

# The predicate the old query used. Kept only to prove the seed is adversarial.
OLD_PREDICATE = """
MATCH (source)-[r]->(target)
WHERE source.name IN $names AND target.name IN $names
RETURN source.name AS source, source.domain AS source_domain,
       target.name AS target, target.domain AS target_domain,
       type(r) AS connectionType
"""


@pytest.fixture(scope="module")
def seeded_neo4j():
    """Start one container per module and seed it synchronously.

    The seed runs on a sync driver deliberately: an async driver bound to a
    module-scoped event loop breaks under pytest-asyncio's per-test loops, and
    the container start is the only expensive part.
    """
    with Neo4jContainer(NEO4J_IMAGE) as container:
        with container.get_driver() as sync_driver:
            sync_driver.execute_query(SEED)
            sync_driver.execute_query(
                "CREATE FULLTEXT INDEX knowledge_index IF NOT EXISTS "
                "FOR (n:Actor|Structure|Artifact|Event|Insight|Pattern|"
                "Challenge|Solution|Lesson|Rationale) ON EACH [n.name, n.description]"
            )
            sync_driver.execute_query(
                "CALL db.awaitIndex('knowledge_index')"
            )
        yield container.get_connection_url(), container.password


@pytest_asyncio.fixture
async def substrate(seeded_neo4j):
    uri, password = seeded_neo4j
    driver = AsyncGraphDatabase.driver(uri, auth=("neo4j", password))
    try:
        yield TemporalKnowledgeSubstrate(driver)
    finally:
        await driver.close()


def _pairs(names_domains):
    return [{"name": n, "domain": d} for n, d in names_domains]


class TestTheSeedIsAdversarial:
    """Prove the failing shape is present before proving it is handled.

    A fix that passes on a graph which never had the defect has proven nothing.
    """

    @pytest.mark.asyncio
    async def test_old_predicate_fabricates_a_chain_self_loop(self, substrate):
        r = await substrate.driver.execute_query(
            OLD_PREDICATE, {"names": ["Ada", "Bo"]},
            routing_control=RoutingControl.READ,
        )
        rows = [(x["source"], x["target"], x["connectionType"]) for x in r.records]
        assert ("Ada", "Ada", "EVOLVED_FROM") in rows, (
            "seed does not reproduce the self-loop defect"
        )

    @pytest.mark.asyncio
    async def test_old_predicate_answers_about_entities_nobody_asked_for(
        self, substrate
    ):
        """The cross-domain fabrication, stated exactly.

        A caller asking about Ada(D2) and Bo(D2) passed only the NAMES, so the
        old predicate answered with edges between the D1 entities — which the
        caller never asked about and could not identify, because the old code
        discarded the domains before returning. Nine such pairs are live in
        production — each authored in one domain and answerable for another.
        """
        r = await substrate.driver.execute_query(
            OLD_PREDICATE, {"names": ["Ada", "Bo"]},
            routing_control=RoutingControl.READ,
        )
        asked_about = {("Ada", "D2"), ("Bo", "D2")}
        answered_about = {
            (x["source"], x["source_domain"]) for x in r.records
        } | {(x["target"], x["target_domain"]) for x in r.records}
        assert answered_about - asked_about, (
            "seed does not reproduce the cross-domain defect"
        )
        assert not (answered_about & asked_about), (
            "the old predicate answered only about D2, so the seed is wrong"
        )


class TestConnectionsAreScopedByIdentity:
    @pytest.mark.asyncio
    async def test_chain_edges_do_not_surface_as_self_loops(self, substrate):
        conns = await substrate._connections_between(
            _pairs([("Ada", "D1"), ("Bo", "D1")])
        )
        assert not [c for c in conns if c.source == c.target], (
            f"self-loop returned: {conns}"
        )
        assert not [c for c in conns if c.type.value == "EVOLVED_FROM"]

    @pytest.mark.asyncio
    async def test_the_authored_edge_is_returned_with_both_domains(self, substrate):
        conns = await substrate._connections_between(
            _pairs([("Ada", "D1"), ("Bo", "D1")])
        )
        informing = [c for c in conns if c.type.value == "INFORMING"]
        assert len(informing) == 1, "INFORMING should dedup to one"
        (c,) = informing
        assert (c.source, c.target) == ("Ada", "Bo")
        assert (c.source_domain, c.target_domain) == ("D1", "D1")

    @pytest.mark.asyncio
    async def test_the_other_domains_pair_is_not_connected(self, substrate):
        """The fabrication the old predicate produced. Ada(D2) and Bo(D2) have
        no authored edge between them; only their D1 namesakes do."""
        conns = await substrate._connections_between(
            _pairs([("Ada", "D2"), ("Bo", "D2")])
        )
        assert conns == [], f"fabricated across domains: {conns}"

    @pytest.mark.asyncio
    async def test_an_edge_authored_on_a_predecessor_is_recovered(self, substrate):
        """Ada evolved after authoring REQUIRING -> Bo. Head-only reads lost it."""
        conns = await substrate._connections_between(
            _pairs([("Ada", "D1"), ("Bo", "D1")])
        )
        assert [c for c in conns if c.type.value == "REQUIRING"], (
            f"source-side stranding not recovered: {[c.type.value for c in conns]}"
        )

    @pytest.mark.asyncio
    async def test_an_edge_pointing_at_a_predecessor_is_recovered(self, substrate):
        """Bo evolved after Ada authored COMPOSING -> Bo. Stranding is symmetric:
        either endpoint evolving is enough to lose the edge."""
        conns = await substrate._connections_between(
            _pairs([("Ada", "D1"), ("Bo", "D1")])
        )
        assert [c for c in conns if c.type.value == "COMPOSING"], (
            f"target-side stranding not recovered: {[c.type.value for c in conns]}"
        )

    @pytest.mark.asyncio
    async def test_two_types_between_one_pair_both_survive(self, substrate):
        """Real in production: one entity is both INFORMING and RECOGNITION
        toward the same pattern. Two authored claims,
        so dedup is by (source, target, type) and never by pair."""
        conns = await substrate._connections_between(
            _pairs([("Ada", "D1"), ("Bo", "D1")])
        )
        types = sorted(c.type.value for c in conns)
        assert types == ["COMPOSING", "INFORMING", "RECOGNITION", "REQUIRING"], types

    @pytest.mark.asyncio
    async def test_chain_resolution_does_not_leak_across_domains(self, substrate):
        """Recovering stranded edges must not reintroduce the fabrication."""
        conns = await substrate._connections_between(
            _pairs([("Ada", "D2"), ("Bo", "D2")])
        )
        assert conns == [], f"chain resolution leaked across domains: {conns}"

    @pytest.mark.asyncio
    async def test_authored_cross_domain_edge_survives(self, substrate):
        """138 cross-domain edges are deliberate. The fix is identity, not
        same-domain confinement — this fails if we over-correct."""
        conns = await substrate._connections_between(
            _pairs([("Bo", "D2"), ("Cy", "D3")])
        )
        assert len(conns) == 1
        (c,) = conns
        assert (c.source_domain, c.target_domain) == ("D2", "D3")
        assert c.type.value == "ENABLING"

    @pytest.mark.asyncio
    async def test_process_edges_are_excluded_by_the_query(self, substrate):
        """Previously filtered by the Connection model raising ValueError —
        an accident that would leak if a process edge type ever joined
        ConnectionType."""
        r = await substrate.driver.execute_query(
            "MATCH (:Domain {name: 'D1'})-[:HAS_SESSION]->(:Session)"
            "-[:DISCOVERED]->(n {name: 'Ada'}) WHERE NOT (n)<-[:EVOLVED_FROM]-() "
            "MATCH (n)<-[r:DISCOVERED]-() RETURN count(DISTINCT r) AS n",
            routing_control=RoutingControl.READ,
        )
        assert r.records[0]["n"] == 1, "seed lacks the DISCOVERED edge to exclude"
        conns = await substrate._connections_between(_pairs([("Ada", "D1")]))
        assert conns == []


class TestSearchKnowledgeEndToEnd:
    @pytest.mark.asyncio
    async def test_search_returns_both_adas_and_no_fabricated_edge(self, substrate):
        result = await substrate.search_knowledge("Ada", limit=10)
        adas = [e for e in result.entities if e.name == "Ada"]
        assert {e.domain for e in adas} == {"D1", "D2"}, (
            "both referents should be returned — they are correctly distinct"
        )
        assert not [c for c in result.connections if c.source == c.target]


class TestDiscoveryCountIsNotAmbiguity:
    """The walk yields one row per discovering session. An entity found twice
    in ONE domain is one entity; the check must count nodes, not rows.

    Reported live as `'Telephony' is ambiguous — found in domains: ['Telephony',
    'Telephony']` — the same domain named twice, which is not an ambiguity at all.
    No fixture caught it because every seeded entity had exactly one
    discovering session.
    """

    @pytest_asyncio.fixture(autouse=True)
    async def ada_discovered_twice_in_d1(self, substrate):
        """A SECOND D1 session discovering the SAME D1 head. Scoped to that one
        node — matching every head named 'Ada' would drag D2's Ada into D1 and
        make it genuinely cross-domain, which is a different condition."""
        await substrate.driver.execute_query(
            "MATCH (d:Domain {name: 'D1'}) "
            "MERGE (d)-[:HAS_SESSION]->(s2:Session {session_id: 'sess-1b'}) "
            "ON CREATE SET s2.timestamp = datetime(), s2.purpose = 'second look', "
            "              s2.summary = '', s2.status = 'active' "
            "WITH s2 MATCH (n:Knowledge {name: 'Ada', description: 'current'}) "
            "MERGE (s2)-[:DISCOVERED]->(n)",
            routing_control=RoutingControl.WRITE)
        r = await substrate.driver.execute_query(
            "MATCH (n:Knowledge {name:'Ada', description:'current'}) "
            "MATCH (d:Domain)-[:HAS_SESSION]->(s:Session)-[:DISCOVERED]->(n) "
            "RETURN count(DISTINCT s) AS sessions, count(DISTINCT d) AS domains",
            routing_control=RoutingControl.READ)
        assert r.records[0]["sessions"] == 2, "the premise: two discovering sessions"
        assert r.records[0]["domains"] == 1, "in exactly one domain"
        yield

    @pytest.mark.asyncio
    async def test_two_discoveries_in_one_domain_are_not_ambiguous(self, substrate):
        from kenning_continuity.substrate import Connection
        await substrate.create_connections("sess-1", [Connection(
            source="Ada", source_domain="D1",
            target="Bo", target_domain="D1", type="EXTENDING")])
        r = await substrate.driver.execute_query(
            "MATCH (a:Knowledge {name:'Ada'})-[e:EXTENDING]->(b:Knowledge {name:'Bo'}) "
            "RETURN count(DISTINCT e) AS n", routing_control=RoutingControl.READ)
        assert r.records[0]["n"] == 1

    @pytest.mark.asyncio
    async def test_a_genuinely_cross_domain_entity_still_refuses(self, substrate):
        """The error is right when the domains actually differ. `ada2` is
        seeded in D2; give it a D1 discoverer and it belongs to both."""
        await substrate.driver.execute_query(
            "MATCH (d:Domain {name:'D1'})-[:HAS_SESSION]->(s:Session {session_id:'sess-1'}) "
            "MATCH (n:Knowledge {name:'Ada', description:'a different Ada'}) "
            "MERGE (s)-[:DISCOVERED]->(n)",
            routing_control=RoutingControl.WRITE)
        try:
            from kenning_continuity.substrate import Connection
            with pytest.raises(ValueError, match="ambiguous"):
                await substrate.create_connections("sess-1", [Connection(
                    source="Ada", source_domain="D1",
                    target="Bo", target_domain="D1", type="CAUSING")])
        finally:
            await substrate.driver.execute_query(
                "MATCH (:Session {session_id:'sess-1'})-[r:DISCOVERED]->"
                "(n:Knowledge {name:'Ada', description:'a different Ada'}) DELETE r",
                routing_control=RoutingControl.WRITE)
