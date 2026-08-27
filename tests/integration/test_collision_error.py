"""The collision refusal, against a real Neo4j.

`create_knowledge` refuses when a (name, domain) is already occupied and names
three options — evolve, confirm, rename. Choosing between them required knowing
what was there, which the refusal did not say, so the agent had to search, read,
decide, then act. Four calls to make one decision.

89% of the production graph has never been evolved and 87% has never been
confirmed. That is what a four-call decision produces: the cheap path is a
differently-named node, and 365 sessions took it.

The refusal itself is deliberate and stays. A silent upsert would hide the
ontological decision the design exists to surface. What was wrong was the
blindness, not the refusal.

Not mockable: the collision path opens a second query inside the same
transaction, so a mocked version would exercise the mock rather than the
behaviour.
"""
import pytest
import pytest_asyncio

pytest.importorskip("testcontainers.neo4j")

from neo4j import AsyncGraphDatabase  # noqa: E402
from testcontainers.neo4j import Neo4jContainer  # noqa: E402

from kenning_continuity.substrate import (  # noqa: E402
    COLLISION_DESCRIPTION_CAP,
    KnowledgeEntity,
    KnowledgeType,
    TemporalKnowledgeSubstrate,
)

NEO4J_IMAGE = "neo4j:5.26-community"

LONG_DESCRIPTION = "x" * (COLLISION_DESCRIPTION_CAP + 500)

SEED = f"""
CREATE (d1:Domain {{name: 'D1'}}), (d2:Domain {{name: 'D2'}})
CREATE (s:Session {{session_id: 'sess-1',
                    timestamp: datetime(), purpose: 'seed', summary: '',
                    status: 'active'}})
CREATE (s2:Session {{session_id: 'sess-2',
                     timestamp: datetime(), purpose: 'seed d2', summary: '',
                     status: 'active'}})
CREATE (d1)-[:HAS_SESSION]->(s)
CREATE (d2)-[:HAS_SESSION]->(s2)

// An entity is placed in a domain by the session that discovers it — there is
// no domain property. See _cypher/_resolve.md.
CREATE (occ:Knowledge:Structure:System {{name: 'Occupied',
    ont_type: 'Structure', description: 'the description already on record',
    t_created: datetime()}})
CREATE (verb:Knowledge:Structure:System {{name: 'Verbose',
    ont_type: 'Structure', description: '{LONG_DESCRIPTION}',
    t_created: datetime()}})
CREATE (free:Knowledge:Structure:System {{name: 'Free',
    ont_type: 'Structure', description: 'same name, other domain',
    t_created: datetime()}})
CREATE (s)-[:DISCOVERED]->(occ)
CREATE (s)-[:DISCOVERED]->(verb)
CREATE (s2)-[:DISCOVERED]->(free)
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


def _entity(name, description, domain="D1"):
    return KnowledgeEntity(
        name=name,
        type=KnowledgeType.SYSTEM,
        domain=domain,
        description=description,
    )


async def _collide(substrate, entity):
    with pytest.raises(ValueError) as exc:
        await substrate.create_knowledge("sess-1", [entity])
    return str(exc.value)


class TestTheRefusalStillRefuses:
    """The blindness is fixed; the refusal is not softened."""

    @pytest.mark.asyncio
    async def test_collision_still_raises(self, substrate):
        msg = await _collide(substrate, _entity("Occupied", "anything"))
        assert "Refusing to create 'Occupied'" in msg

    @pytest.mark.asyncio
    async def test_nothing_is_written_on_collision(self, substrate):
        await _collide(substrate, _entity("Occupied", "a new take"))
        r = await substrate.driver.execute_query(
            "MATCH (:Domain {name: 'D1'})-[:HAS_SESSION]->(:Session)"
            "-[:DISCOVERED]->(n {name: 'Occupied'}) RETURN count(DISTINCT n) AS n"
        )
        assert r.records[0]["n"] == 1, "the refused entity was created anyway"

    @pytest.mark.asyncio
    async def test_the_batch_is_atomic_and_says_so(self, substrate):
        """A partial batch would leave the agent guessing what landed."""
        msg = await _collide(substrate, _entity("Occupied", "x"))
        assert "Nothing in this batch was created" in msg

        await substrate.create_knowledge("sess-1", [_entity("Solo", "fine")])
        with pytest.raises(ValueError):
            await substrate.create_knowledge(
                "sess-1",
                [_entity("AlsoNew", "fine"), _entity("Occupied", "collides")],
            )
        r = await substrate.driver.execute_query(
            "MATCH (:Domain {name: 'D1'})-[:HAS_SESSION]->(:Session)"
            "-[:DISCOVERED]->(n {name: 'AlsoNew'}) RETURN count(DISTINCT n) AS n"
        )
        assert r.records[0]["n"] == 0, "the batch was not rolled back"


class TestTheRefusalSaysWhatIsThere:
    @pytest.mark.asyncio
    async def test_the_existing_description_is_in_the_error(self, substrate):
        msg = await _collide(substrate, _entity("Occupied", "something else"))
        assert "the description already on record" in msg

    @pytest.mark.asyncio
    async def test_type_and_creation_time_are_in_the_error(self, substrate):
        msg = await _collide(substrate, _entity("Occupied", "something else"))
        assert "Structure" in msg
        assert "created" in msg

    @pytest.mark.asyncio
    async def test_three_options_are_still_named(self, substrate):
        msg = await _collide(substrate, _entity("Occupied", "something else"))
        for option in ("evolve_knowledge", "confirm_knowledge", "different name"):
            assert option in msg


class TestTheIdenticalVerdict:
    """A measurement, not a judgement: it settles confirm-vs-evolve without
    the tool making the ontological call."""

    @pytest.mark.asyncio
    async def test_identical_description_is_named_as_such(self, substrate):
        msg = await _collide(
            substrate, _entity("Occupied", "the description already on record")
        )
        assert "IDENTICAL" in msg
        assert "confirm_knowledge" in msg

    @pytest.mark.asyncio
    async def test_different_description_is_named_as_such(self, substrate):
        msg = await _collide(substrate, _entity("Occupied", "genuinely new content"))
        assert "DIFFERS" in msg
        assert "IDENTICAL" not in msg

    @pytest.mark.asyncio
    async def test_whitespace_difference_is_not_identical(self, substrate):
        """Byte-identical, deliberately. A near-match is the agent's call."""
        msg = await _collide(
            substrate, _entity("Occupied", " the description already on record ")
        )
        assert "DIFFERS" in msg


class TestTheDescriptionIsBounded:
    @pytest.mark.asyncio
    async def test_a_long_description_is_truncated_legibly(self, substrate):
        msg = await _collide(substrate, _entity("Verbose", "new"))
        assert "truncated at" in msg
        assert "get_chain_history" in msg
        assert len(msg) < COLLISION_DESCRIPTION_CAP + 1500

    @pytest.mark.asyncio
    async def test_a_short_description_is_not_truncated(self, substrate):
        msg = await _collide(substrate, _entity("Occupied", "new"))
        assert "truncated at" not in msg


class TestScoping:
    @pytest.mark.asyncio
    async def test_the_same_name_in_another_domain_does_not_collide(self, substrate):
        """69 names exist in more than one domain in production. A name is not
        an identity; (name, domain) is."""
        created = await substrate.create_knowledge(
            "sess-1", [_entity("Free", "a distinct referent", domain="D1")]
        )
        assert len(created) == 1
