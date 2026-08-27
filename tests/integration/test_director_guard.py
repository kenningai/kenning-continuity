"""begin_session writes the person layer, against a real Neo4j.

A Session is an act and an act has a director. There is no default and no
fallback identity: a shared "refused" node would collect every refusal from
every human into one Person and assert they are one intentional vector, which
is identity-by-string — the exact error this layer exists to remove. No person,
no vector, no act.

Not mockable. The guard's whole substance is a MERGE on source_key inside the
session-creation transaction: whether one human writing their name two ways
lands on one node, whether a knowledge-layer :Actor:PersonReferent is matched by
it, and whether DIRECTED comes out exactly-one-per-session. A mocked substrate
would exercise the mock.
"""
import pytest
import pytest_asyncio

pytest.importorskip("testcontainers.neo4j")

from neo4j import AsyncGraphDatabase, RoutingControl  # noqa: E402
from testcontainers.neo4j import Neo4jContainer  # noqa: E402

from kenning_continuity.substrate import TemporalKnowledgeSubstrate  # noqa: E402

NEO4J_IMAGE = "neo4j:5.26-community"

SEED = """
CREATE (d1:Domain {name: 'Telephony'})
CREATE (:Domain {name: 'Example CRM'})

// a knowledge-layer person referent, post-cutover. Same NAME as the director,
// deliberately: the two are constitutively different and must not collide.
CREATE (seed_s:Session {session_id: 'seed-telephony', timestamp: datetime(),
                        purpose: 'seed', summary: '', status: 'active'})
CREATE (seed_p:Person {name: 'Seed Director', source_key: 'seed@example.com'})
CREATE (seed_p)-[:DIRECTED {method: 'platform-stated'}]->(seed_s)
CREATE (d1)-[:HAS_SESSION]->(seed_s)
CREATE (ref:Knowledge:Actor:PersonReferent {name: 'Bob Example',
                       ont_type: 'Actor',
                       description: 'someone a session talked about',
                       t_created: datetime()})
CREATE (seed_s)-[:DISCOVERED]->(ref)
"""

DIRECTOR_A = ("Bob Example", "bob.example@example.com")


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


async def _count(substrate, query):
    r = await substrate.driver.execute_query(
        query, routing_control=RoutingControl.READ)
    return r.records[0][0]


async def _begin(substrate, domain="Telephony", purpose="p", who=DIRECTOR_A):
    return await substrate.begin_session(
        domain=domain, purpose=purpose, director=who[0], director_key=who[1])


class TestTheActHasADirector:
    @pytest.mark.asyncio
    async def test_a_session_arrives_with_its_person_and_edge(self, substrate):
        r = await _begin(substrate, purpose="document campaign routing")
        assert r["director"] == "Bob Example"
        assert r["director_key"] == "bob.example@example.com"

        edges = await _count(
            substrate,
            "MATCH (p:Person {source_key: 'bob.example@example.com'})"
            "-[d:DIRECTED]->(s:Session {session_id: '%s'}) "
            "RETURN count(d)" % r["session_id"])
        assert edges == 1

    @pytest.mark.asyncio
    async def test_the_edge_carries_how_the_source_was_established(self, substrate):
        r = await _begin(substrate)
        method = await _count(
            substrate,
            "MATCH ()-[d:DIRECTED]->(:Session {session_id: '%s'}) "
            "RETURN d.method" % r["session_id"])
        assert method == "platform-stated"

    @pytest.mark.asyncio
    async def test_every_session_has_exactly_one_director(self, substrate):
        await _begin(substrate)
        await _begin(substrate, domain="Example CRM")
        over = await _count(
            substrate,
            "MATCH (s:Session) OPTIONAL MATCH (:Person)-[d:DIRECTED]->(s) "
            "WITH s, count(d) AS n WHERE n <> 1 RETURN count(s)")
        assert over == 0

    @pytest.mark.asyncio
    async def test_session_no_longer_carries_the_denormalized_user(self, substrate):
        r = await _begin(substrate)
        has_user = await _count(
            substrate,
            "MATCH (s:Session {session_id: '%s'}) WHERE s.user IS NOT NULL "
            "RETURN count(s)" % r["session_id"])
        assert has_user == 0


class TestOneHumanIsOneNode:
    @pytest.mark.asyncio
    async def test_the_key_is_the_email_not_the_name(self, substrate):
        """The three-spellings failure, prevented rather than repaired."""
        await _begin(substrate, who=("Bob Example", "bob.example@example.com"))
        await _begin(substrate, who=("Bob Example", "bob.example@example.com"))
        await _begin(substrate, who=("bob.example", "Bob.Example@example.com"))

        nodes = await _count(
            substrate,
            "MATCH (p:Person {source_key: 'bob.example@example.com'}) "
            "RETURN count(p)")
        assert nodes == 1, "one human, one process-layer node"

    @pytest.mark.asyncio
    async def test_the_anchor_is_not_rewritten_by_a_later_session(self, substrate):
        await _begin(substrate, who=("Erin Example", "erin.example@example.com"))
        await _begin(substrate, who=("Erin Example", "erin.example@example.com"))
        name = await _count(
            substrate,
            "MATCH (p:Person {source_key: 'erin.example@example.com'}) "
            "RETURN p.name")
        assert name == "Erin Example"

    @pytest.mark.asyncio
    async def test_the_knowledge_layer_referent_is_never_matched(self, substrate):
        """The referent shares the director's NAME and must stay a referent."""
        await _begin(substrate)
        referents = await _count(
            substrate,
            "MATCH (p:Knowledge:Actor:PersonReferent {name: 'Bob Example'}) "
            "WHERE p.source_key IS NULL AND NOT (p)-[:DIRECTED]->() "
            "RETURN count(p)")
        assert referents == 1

        directors = await _count(
            substrate,
            "MATCH (p:Person)-[:DIRECTED]->() WHERE p:Knowledge RETURN count(p)")
        assert directors == 0, "no referent ever directs"


class TestRefusalMeansNoSession:
    @pytest.mark.parametrize("name,key,fragment", [
        ("", "bob.example@example.com", "full name"),
        ("agent", "bob.example@example.com", "placeholder, not a person"),
        ("Person Refused", "refused@example.com", "placeholder, not a person"),
        ("Unknown", "bob.example@example.com", "placeholder, not a person"),
        ("bob.example@example.com", "bob.example@example.com",
         "address, not a name"),
        ("Bob Example", "", "requires the director's email"),
        ("Bob Example", "agent", "not an email address"),
        ("Bob Example", "bob.example", "not an email address"),
        ("Bob Example", "bob example@example.com", "not an email address"),
        ("Bob Example", "refused@example.com", "placeholder address"),
        ("Bob Example", "unknown@example.com", "placeholder address"),
    ])
    @pytest.mark.asyncio
    async def test_the_gate_refuses(self, substrate, name, key, fragment):
        before = await _count(substrate, "MATCH (s:Session) RETURN count(s)")
        with pytest.raises(ValueError, match=fragment):
            await substrate.begin_session(
                domain="Telephony", purpose="p", director=name, director_key=key)
        after = await _count(substrate, "MATCH (s:Session) RETURN count(s)")
        assert after == before, "a refused director must leave no session behind"

    @pytest.mark.asyncio
    async def test_no_placeholder_person_is_ever_created(self, substrate):
        for name, key in [("agent", "agent@example.com"), ("Refused", "refused@example.com")]:
            with pytest.raises(ValueError):
                await substrate.begin_session(
                    domain="Telephony", purpose="p", director=name, director_key=key)
        stray = await _count(
            substrate,
            "MATCH (p:Person) WHERE p.source_key IN ['agent@example.com', 'refused@example.com'] "
            "RETURN count(p)")
        assert stray == 0


class TestTheLayersNeverTouch:
    """v0.8.0 cutover: :Person names the vector, :PersonReferent the referent.

    The knowledge layer never reaches the process layer in either direction —
    a referent has no frame from which to perform an act of reference. Enforced
    where every other invariant is, in the tool.
    """

    @pytest.mark.asyncio
    async def test_person_cannot_be_created_as_knowledge(self, substrate):
        from kenning_continuity.substrate import KnowledgeEntity
        with pytest.raises(Exception):
            KnowledgeEntity(name="Bob Example", type="Person", domain="Telephony",
                            description="x")

    @pytest.mark.asyncio
    async def test_person_cannot_reach_a_node_through_sub_labels(self, substrate):
        from kenning_continuity.substrate import _resolve_labels
        with pytest.raises(ValueError, match="process-layer label"):
            _resolve_labels("Actor", ["Person"])

    @pytest.mark.asyncio
    async def test_a_sub_label_cannot_smuggle_cypher(self, substrate):
        from kenning_continuity.substrate import _resolve_labels
        with pytest.raises(ValueError, match="Invalid sub-label"):
            _resolve_labels("Actor", ["Evil` REMOVE n:Actor //"])

    @pytest.mark.asyncio
    async def test_an_ontological_type_cannot_be_a_sub_label(self, substrate):
        from kenning_continuity.substrate import _resolve_labels
        with pytest.raises(ValueError, match="ontological type, not a sub-label"):
            _resolve_labels("Actor", ["Structure"])

    @pytest.mark.asyncio
    async def test_an_edge_reaches_the_referent_and_never_the_director(self, substrate):
        """Both are named 'Bob Example'. Only one is edgeable.

        The :Knowledge scoping settles this structurally rather than by a check
        after the match: create_connections cannot reach a process node at all,
        so the edge lands on the referent — which is correct, that is what a
        knowledge edge about a person points at.
        """
        from kenning_continuity.substrate import Connection, KnowledgeEntity
        r = await _begin(substrate)
        await substrate.create_knowledge(r["session_id"], [KnowledgeEntity(
            name="Telephony Platform", type="System", domain="Telephony",
            description="the telephony platform")])
        await substrate.create_connections(r["session_id"], [Connection(
            source="Telephony Platform", target="Bob Example", type="INFORMING",
            source_domain="Telephony", target_domain="Telephony")])

        on_referent = await _count(
            substrate,
            "MATCH (:Knowledge {name:'Telephony Platform'})-[:INFORMING]->"
            "(t:Knowledge:PersonReferent {name:'Bob Example'}) RETURN count(t)")
        assert on_referent == 1

        on_director = await _count(
            substrate,
            "MATCH (:Person {source_key:'bob.example@example.com'})"
            "-[r]-() WHERE type(r) <> 'DIRECTED' RETURN count(r)")
        assert on_director == 0, "the director carries nothing but DIRECTED"

    @pytest.mark.asyncio
    async def test_the_backstop_fires_if_the_layers_ever_merge(self, substrate):
        """Unreachable in a healthy graph — migration step 0g refuses the state
        that would produce it. Kept because a guard whose precondition is
        checked elsewhere is still the thing that holds if the check is wrong."""
        from kenning_continuity.substrate import Connection, KnowledgeEntity
        r = await _begin(substrate)
        await substrate.create_knowledge(r["session_id"], [KnowledgeEntity(
            name="Merged Layers", type="System", domain="Telephony", description="x")])
        await substrate.driver.execute_query(
            "MATCH (n:Knowledge {name: 'Merged Layers'}) SET n:Session",
            routing_control=RoutingControl.WRITE)
        try:
            with pytest.raises(ValueError, match="cannot touch the process layer"):
                await substrate.create_connections(r["session_id"], [Connection(
                    source="Telephony Platform", target="Merged Layers",
                    type="INFORMING", source_domain="Telephony", target_domain="Telephony")])
        finally:
            await substrate.driver.execute_query(
                "MATCH (n:Knowledge {name: 'Merged Layers'}) REMOVE n:Session",
                routing_control=RoutingControl.WRITE)


class TestTheKnowledgeMarker:
    """One positive assertion in place of ten exclusion lists.

    Before the marker, every label-less MATCH carried `AND NOT x:Domain AND NOT
    x:Session`. When Person joined the process layer, only two of the ten sites
    gained `AND NOT x:Person`; the other eight were saved by an accident —
    process nodes carry no `domain`, so a (name, domain) match misses them.
    An exclusion list must be extended every time the process layer grows.
    """

    @pytest.mark.asyncio
    async def test_a_created_node_carries_the_marker(self, substrate):
        from kenning_continuity.substrate import KnowledgeEntity
        r = await _begin(substrate)
        await substrate.create_knowledge(r["session_id"], [KnowledgeEntity(
            name="Marked On Create", type="System", domain="Telephony", description="x")])
        assert await _count(
            substrate,
            "MATCH (n:Knowledge:Structure:System {name:'Marked On Create'}) "
            "RETURN count(n)") == 1

    @pytest.mark.asyncio
    async def test_an_evolved_node_carries_the_marker(self, substrate):
        from kenning_continuity.substrate import KnowledgeEntity
        r = await _begin(substrate)
        await substrate.create_knowledge(r["session_id"], [KnowledgeEntity(
            name="Marked On Evolve", type="System", domain="Telephony", description="first")])
        await substrate.evolve_knowledge(
            r["session_id"], "Marked On Evolve", "second", domain="Telephony")
        unmarked = await _count(
            substrate,
            "MATCH (n {name: 'Marked On Evolve'}) WHERE NOT n:Knowledge RETURN count(n)")
        assert unmarked == 0, "every node in the chain carries the marker"

    @pytest.mark.asyncio
    async def test_no_process_node_carries_the_marker(self, substrate):
        await _begin(substrate)
        assert await _count(
            substrate,
            "MATCH (n:Knowledge) WHERE n:Domain OR n:Session OR n:Person "
            "RETURN count(n)") == 0

    @pytest.mark.asyncio
    async def test_the_marker_cannot_be_claimed_as_a_sub_label(self, substrate):
        from kenning_continuity.substrate import _resolve_labels
        with pytest.raises(ValueError, match="applied by the substrate"):
            _resolve_labels("Actor", ["Knowledge"])

    @pytest.mark.asyncio
    async def test_the_marker_is_not_reported_as_a_sub_label(self, substrate):
        from kenning_continuity.substrate import _split_labels
        ont, subs = _split_labels(["Knowledge", "Actor", "PersonReferent"])
        assert (ont, subs) == ("Actor", ["PersonReferent"])


class TestEveryEdgeHasAnAuthor:
    """An edge is an assertion — that these two things stand in this relation —
    and an assertion nobody made is the one thing this ontology has no room for.

    Neo4j cannot make a relationship the endpoint of another relationship, so
    the tql's `knowledge-link plays discovered:found` is represented as a
    `session_id` property. See docs/design/ontology/README.md.
    """

    @pytest.mark.asyncio
    async def test_an_edge_records_the_session_that_asserted_it(self, substrate):
        from kenning_continuity.substrate import Connection, KnowledgeEntity
        r = await _begin(substrate, purpose="assert a relation")
        await substrate.create_knowledge(r["session_id"], [
            KnowledgeEntity(name="Edge Src", type="System", domain="Telephony", description="a"),
            KnowledgeEntity(name="Edge Tgt", type="System", domain="Telephony", description="b")])
        await substrate.create_connections(r["session_id"], [Connection(
            source="Edge Src", target="Edge Tgt", type="ENABLING",
            source_domain="Telephony", target_domain="Telephony")])

        assert await _count(
            substrate,
            "MATCH (:Knowledge {name:'Edge Src'})-[e:ENABLING]->(:Knowledge {name:'Edge Tgt'}) "
            "RETURN e.session_id") == r["session_id"]

    @pytest.mark.asyncio
    async def test_the_edge_reaches_the_person_in_two_hops(self, substrate):
        """The whole point: an assertion is adjudicable because someone made it."""
        from kenning_continuity.substrate import Connection, KnowledgeEntity
        r = await _begin(substrate)
        await substrate.create_knowledge(r["session_id"], [
            KnowledgeEntity(name="Hop Src", type="System", domain="Telephony", description="a"),
            KnowledgeEntity(name="Hop Tgt", type="System", domain="Telephony", description="b")])
        await substrate.create_connections(r["session_id"], [Connection(
            source="Hop Src", target="Hop Tgt", type="REQUIRING",
            source_domain="Telephony", target_domain="Telephony")])

        assert await _count(
            substrate,
            "MATCH (:Knowledge {name:'Hop Src'})-[e:REQUIRING]->() "
            "MATCH (p:Person)-[:DIRECTED]->(s:Session {session_id: e.session_id}) "
            "RETURN p.source_key") == "bob.example@example.com"

    @pytest.mark.asyncio
    async def test_an_edge_without_a_real_session_is_refused(self, substrate):
        from kenning_continuity.substrate import Connection
        with pytest.raises(ValueError, match="needs the session that made it"):
            await substrate.create_connections("no-such-session", [Connection(
                source="Hop Src", target="Hop Tgt", type="INFORMING",
                source_domain="Telephony", target_domain="Telephony")])

    @pytest.mark.asyncio
    async def test_re_asserting_records_the_later_session(self, substrate):
        """The edge is one relation; the author is whoever last asserted it."""
        from kenning_continuity.substrate import Connection, KnowledgeEntity
        r1 = await _begin(substrate)
        await substrate.create_knowledge(r1["session_id"], [
            KnowledgeEntity(name="Again Src", type="System", domain="Telephony", description="a"),
            KnowledgeEntity(name="Again Tgt", type="System", domain="Telephony", description="b")])
        conn = [Connection(source="Again Src", target="Again Tgt", type="CAUSING",
                           source_domain="Telephony", target_domain="Telephony")]
        await substrate.create_connections(r1["session_id"], conn)
        r2 = await _begin(substrate, who=("Carol Example", "carol.example@example.com"))
        await substrate.create_connections(r2["session_id"], conn)

        assert await _count(
            substrate,
            "MATCH ()-[e:CAUSING]->(:Knowledge {name:'Again Tgt'}) RETURN count(e)") == 1
        assert await _count(
            substrate,
            "MATCH ()-[e:CAUSING]->(:Knowledge {name:'Again Tgt'}) RETURN e.session_id"
        ) == r2["session_id"]


class TestBoundedReEntry:
    """begin_session returned 600,820 characters on Service Docs — roughly
    150k tokens, before the session had a question. Truncating descriptions
    would not have helped: they are uniformly long. Selection is the remedy."""

    @pytest_asyncio.fixture(autouse=True)
    async def a_populated_domain(self, substrate):
        from kenning_continuity.substrate import KnowledgeEntity
        r = await _begin(substrate, domain="Example CRM")
        existing = await _count(
            substrate,
            "MATCH (:Domain {name:'Example CRM'})-[:HAS_SESSION]->(:Session)"
            "-[:DISCOVERED]->(e:Knowledge) RETURN count(DISTINCT e)")
        if not existing:
            await substrate.create_knowledge(r["session_id"], [
                KnowledgeEntity(name=f"Sys {i}", type="System", domain="Example CRM",
                                description="x" * 700) for i in range(4)]
                + [KnowledgeEntity(name=f"Snag {i}", type="Challenge",
                                   domain="Example CRM", description="y" * 700)
                   for i in range(2)])
        yield

    @pytest.mark.asyncio
    async def test_begin_session_returns_a_count_not_the_corpus(self, substrate):
        r = await _begin(substrate, domain="Example CRM")
        assert "domain_state" not in r
        assert r["domain_summary"]["head_entities"] == 6
        assert r["domain_summary"]["by_type"] == {"Structure": 4, "Challenge": 2}

    @pytest.mark.asyncio
    async def test_types_selects(self, substrate):
        state = await substrate.get_domain_state("Example CRM", types=["Challenge"])
        assert {e.name for e in state.entities} == {"Snag 0", "Snag 1"}

    @pytest.mark.asyncio
    async def test_a_sub_label_selects_too(self, substrate):
        state = await substrate.get_domain_state("Example CRM", types=["System"])
        assert len(state.entities) == 4

    @pytest.mark.asyncio
    async def test_limit_bounds_the_payload(self, substrate):
        state = await substrate.get_domain_state("Example CRM", limit=2)
        assert len(state.entities) == 2

    @pytest.mark.asyncio
    async def test_descriptions_false_drops_the_weight(self, substrate):
        full = await substrate.get_domain_state("Example CRM")
        cheap = await substrate.get_domain_state("Example CRM", descriptions=False)
        assert len(full.entities) == len(cheap.entities)
        assert sum(len(e.description) for e in full.entities) > 4000
        assert sum(len(e.description) for e in cheap.entities) == 0
        assert {e.name for e in cheap.entities} == {e.name for e in full.entities}

    @pytest.mark.asyncio
    async def test_the_full_dump_is_still_available_deliberately(self, substrate):
        state = await substrate.get_domain_state("Example CRM")
        assert len(state.entities) == 6
        assert all(e.description for e in state.entities)

    @pytest.mark.asyncio
    async def test_an_unknown_type_is_refused_rather_than_returning_nothing(self, substrate):
        with pytest.raises(ValueError, match="Unknown type"):
            await substrate.get_domain_state("Example CRM", types=["Gotcha"])

    @pytest.mark.asyncio
    async def test_a_nonsense_limit_is_refused(self, substrate):
        with pytest.raises(ValueError, match="limit must be at least 1"):
            await substrate.get_domain_state("Example CRM", limit=0)
