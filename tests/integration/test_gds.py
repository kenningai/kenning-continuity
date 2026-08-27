"""The graph-analytics tools, run against Neo4j with the GDS plugin loaded.

The unit tests confirm these tools are registered. They cannot confirm that a
procedure name is spelled right, that it yields the columns the query reads,
or that the projection leaves the process layer out — all of which live in
Cypher, which the mocked substrate never executes.

The graph has a known answer. Two four-node cliques joined by one bridge edge:
any community detection worth reading returns exactly those two groups, and
both bridge ends are the most central nodes. A Domain and a Session sit beside
them, discovering the knowledge, and must not appear in any result.

A second domain holds a linked pair, joined to the first domain by one
cross-domain edge. That is what tells a domain-scoped projection apart from
an unscoped one, and cross-domain from domain-only. Domain membership is a
walk from the Domain, never a property on the node.
"""
import json

import pytest
import pytest_asyncio

pytest.importorskip("testcontainers.neo4j")

from neo4j import AsyncGraphDatabase  # noqa: E402
from testcontainers.neo4j import Neo4jContainer  # noqa: E402

from kenning_continuity.server import create_mcp_server  # noqa: E402
from kenning_continuity.substrate import TemporalKnowledgeSubstrate  # noqa: E402

NEO4J_IMAGE = "neo4j:5.26-community"

LEFT = ["Left A", "Left B", "Left C", "Left D"]
RIGHT = ["Right A", "Right B", "Right C", "Right D"]

SEED = """
CREATE (d:Domain {name: 'Example Ops'})
CREATE (s:Session {session_id: 'sess-1', purpose: 'seed', status: 'active'})
CREATE (d)-[:HAS_SESSION]->(s)
WITH s
UNWIND $names AS name
CREATE (k:Knowledge:Structure {name: name, ont_type: 'Structure', description: name})
CREATE (s)-[:DISCOVERED]->(k)
WITH collect(k) AS ks
UNWIND ks AS a
UNWIND ks AS b
WITH a, b
WHERE elementId(a) < elementId(b)
  AND left(a.name, 4) = left(b.name, 4)
CREATE (a)-[:COMPOSING {session_id: 'sess-1'}]->(b)
"""

BRIDGE = """
MATCH (a:Knowledge {name: 'Left A'}), (b:Knowledge {name: 'Right A'})
CREATE (a)-[:ENABLING {session_id: 'sess-1'}]->(b)
"""

CRM = ["CRM X", "CRM Y"]

# A third domain, alone, for chains. Hub was revised twice (h1 <- h2 <- h3) and
# Spoke A once (a1 <- a2). One edge was authored on Hub's oldest version, to
# Spoke A's oldest; the same Hub-to-Spoke-B edge was authored twice, on h2 and
# on h3. As known now: three entities, two edges.
CHAINS = """
CREATE (d:Domain {name: 'Example Chains'})
CREATE (s:Session {session_id: 'sess-3', purpose: 'seed', status: 'active'})
CREATE (d)-[:HAS_SESSION]->(s)
CREATE (h1:Knowledge:Structure {name: 'Hub', ont_type: 'Structure', description: 'v1'})
CREATE (h2:Knowledge:Structure {name: 'Hub', ont_type: 'Structure', description: 'v2'})
CREATE (h3:Knowledge:Structure {name: 'Hub', ont_type: 'Structure', description: 'v3'})
CREATE (a1:Knowledge:Structure {name: 'Spoke A', ont_type: 'Structure', description: 'v1'})
CREATE (a2:Knowledge:Structure {name: 'Spoke A', ont_type: 'Structure', description: 'v2'})
CREATE (b:Knowledge:Structure {name: 'Spoke B', ont_type: 'Structure', description: 'v1'})
CREATE (h2)-[:EVOLVED_FROM]->(h1), (h3)-[:EVOLVED_FROM]->(h2), (a2)-[:EVOLVED_FROM]->(a1)
CREATE (h1)-[:INFORMING {session_id: 'sess-3'}]->(a1)
CREATE (h2)-[:INFORMING {session_id: 'sess-3'}]->(b)
CREATE (h3)-[:INFORMING {session_id: 'sess-3'}]->(b)
FOREACH (n IN [h1, h2, h3, a1, a2, b] | CREATE (s)-[:DISCOVERED]->(n))
"""

SECOND_DOMAIN = """
CREATE (d:Domain {name: 'Example CRM'})
CREATE (s:Session {session_id: 'sess-2', purpose: 'seed', status: 'active'})
CREATE (d)-[:HAS_SESSION]->(s)
CREATE (x:Knowledge:Structure {name: 'CRM X', ont_type: 'Structure', description: 'x'})
CREATE (y:Knowledge:Structure {name: 'CRM Y', ont_type: 'Structure', description: 'y'})
CREATE (s)-[:DISCOVERED]->(x)
CREATE (s)-[:DISCOVERED]->(y)
CREATE (x)-[:COMPOSING {session_id: 'sess-2'}]->(y)
WITH x
MATCH (b:Knowledge {name: 'Left B'})
CREATE (b)-[:INFORMING {session_id: 'sess-2'}]->(x)
"""


@pytest.fixture(scope="module")
def gds_neo4j():
    container = Neo4jContainer(NEO4J_IMAGE).with_env(
        "NEO4J_PLUGINS", '["graph-data-science"]')
    with container:
        with container.get_driver() as driver:
            driver.execute_query(SEED, names=LEFT + RIGHT)
            driver.execute_query(BRIDGE)
            driver.execute_query(SECOND_DOMAIN)
            driver.execute_query(CHAINS)
            edges = driver.execute_query(
                "MATCH (:Knowledge)-[r]->(:Knowledge) RETURN count(r) AS n"
            ).records[0]["n"]
        # 6 edges per clique, one bridge, one CRM pair, one cross-domain edge,
        # and the chains domain's 3 EVOLVED_FROM and 3 INFORMING. A seed that
        # did not land would make every empty result below look like an
        # exclusion working.
        assert edges == 21
        yield container.get_connection_url(), container.password


@pytest_asyncio.fixture
async def server(gds_neo4j):
    uri, password = gds_neo4j
    driver = AsyncGraphDatabase.driver(uri, auth=("neo4j", password))
    try:
        yield create_mcp_server(TemporalKnowledgeSubstrate(driver))
    finally:
        await driver.close()


async def _call(server, tool_name, /, **kwargs):
    tool = await server.get_tool(tool_name)
    result = await tool.fn(**kwargs)
    return json.loads(result.content[0].text)


async def _project(server, name, domain=None, include_cross_domain=True):
    return await _call(server, "gds_create_projection", name=name, domain=domain,
                       include_cross_domain=include_cross_domain, rel_types=None,
                       undirected=True, memory_gb="1GB")


async def _nodes(server, name):
    rows = await _call(server, "gds_pagerank", projection=name, result_limit=1000)
    return {r["node"] for r in rows}


@pytest_asyncio.fixture
async def projection(server):
    """The first domain alone: the two cliques and nothing across the boundary."""
    await _project(server, "two-cliques", domain="Example Ops",
                   include_cross_domain=False)
    yield "two-cliques"
    await _call(server, "gds_drop_projection", name="two-cliques")


def _communities(rows, key):
    groups: dict[int, set[str]] = {}
    for r in rows:
        groups.setdefault(r[key], set()).add(r["node"])
    return sorted(groups.values(), key=sorted)


class TestTheProjectionHoldsOnlyKnowledge:
    @pytest.mark.asyncio
    async def test_no_process_node_is_ranked(self, server, projection):
        rows = await _call(server, "gds_pagerank", projection=projection, result_limit=100)
        assert {r["node"] for r in rows} == set(LEFT + RIGHT)
        assert {r["type"] for r in rows} == {"Structure"}


class TestDomainScope:
    @pytest.mark.asyncio
    async def test_domain_only_holds_that_domains_knowledge(self, server):
        await _project(server, "ops-only", domain="Example Ops",
                       include_cross_domain=False)
        try:
            assert await _nodes(server, "ops-only") == set(LEFT + RIGHT)
        finally:
            await _call(server, "gds_drop_projection", name="ops-only")

    @pytest.mark.asyncio
    async def test_cross_domain_adds_only_the_far_end_of_a_crossing_edge(self, server):
        await _project(server, "ops-cross", domain="Example Ops",
                       include_cross_domain=True)
        try:
            assert await _nodes(server, "ops-cross") == set(LEFT + RIGHT) | {"CRM X"}
        finally:
            await _call(server, "gds_drop_projection", name="ops-cross")

    @pytest.mark.asyncio
    async def test_the_other_domain_is_scoped_the_same_way(self, server):
        await _project(server, "crm-only", domain="Example CRM",
                       include_cross_domain=False)
        try:
            assert await _nodes(server, "crm-only") == set(CRM)
        finally:
            await _call(server, "gds_drop_projection", name="crm-only")

    @pytest.mark.asyncio
    async def test_unscoped_holds_every_domain(self, server):
        await _project(server, "everything")
        try:
            assert await _nodes(server, "everything") == set(LEFT + RIGHT + CRM) | {
                "Hub", "Spoke A", "Spoke B"}
        finally:
            await _call(server, "gds_drop_projection", name="everything")

    @pytest.mark.asyncio
    async def test_a_domain_with_nothing_to_project_is_refused_not_null(self, server):
        from fastmcp.exceptions import ToolError
        with pytest.raises(ToolError, match="Nothing to project"):
            await _project(server, "nowhere", domain="No Such Domain")
        exists = await _call(server, "read_cypher",
                             query="CALL gds.graph.exists('nowhere') YIELD exists "
                                   "RETURN exists", params={})
        assert exists == [{"exists": False}]


class TestTheProjectionIsWhatIsKnownNow:
    """Each entity once, as its current version, with every edge it ever had."""

    @pytest.mark.asyncio
    async def test_each_entity_enters_once_with_its_resolved_edges(self, server):
        created = await _call(server, "gds_create_projection", name="chains",
                              domain="Example Chains", include_cross_domain=False,
                              rel_types=None, undirected=False, memory_gb="1GB")
        try:
            summary = next(iter(created[0].values()))
            # Three entities, not six versions. Two edges: Hub-Spoke A resolved
            # from the oldest versions, Hub-Spoke B collapsed from two.
            assert summary["nodeCount"] == 3, summary
            assert summary["relationshipCount"] == 2, summary
            rows = await _call(server, "gds_pagerank", projection="chains", result_limit=100)
            assert sorted(r["node"] for r in rows) == ["Hub", "Spoke A", "Spoke B"], (
                "a revised entity must appear once; Spoke A must survive, since "
                "its only edge was authored on versions that are no longer current")
        finally:
            await _call(server, "gds_drop_projection", name="chains")

    @pytest.mark.asyncio
    async def test_the_projected_nodes_are_the_current_versions(self, server):
        await _project(server, "chains-heads", domain="Example Chains",
                       include_cross_domain=False)
        try:
            descs = await _call(server, "read_cypher", query=(
                "CALL gds.pageRank.stream('chains-heads') YIELD nodeId "
                "RETURN gds.util.asNode(nodeId).name AS name, "
                "gds.util.asNode(nodeId).description AS description "
                "ORDER BY name"), params={})
            assert descs == [{"name": "Hub", "description": "v3"},
                             {"name": "Spoke A", "description": "v2"},
                             {"name": "Spoke B", "description": "v1"}]
        finally:
            await _call(server, "gds_drop_projection", name="chains-heads")


class TestArticleRank:
    @pytest.mark.asyncio
    async def test_it_ranks_every_knowledge_node(self, server, projection):
        rows = await _call(server, "gds_articlerank", projection=projection, result_limit=100)
        assert {r["node"] for r in rows} == set(LEFT + RIGHT)
        assert all(r["score"] > 0 for r in rows)
        assert [r["score"] for r in rows] == sorted((r["score"] for r in rows), reverse=True)

    @pytest.mark.asyncio
    async def test_the_bridge_ends_rank_highest(self, server, projection):
        rows = await _call(server, "gds_articlerank", projection=projection, result_limit=2)
        assert {r["node"] for r in rows} == {"Left A", "Right A"}


class TestLeiden:
    @pytest.mark.asyncio
    async def test_it_finds_the_two_cliques(self, server, projection):
        rows = await _call(server, "gds_leiden", projection=projection, result_limit=100)
        assert _communities(rows, "communityId") == [set(LEFT), set(RIGHT)]

    @pytest.mark.asyncio
    async def test_louvain_is_kept_and_agrees_here(self, server, projection):
        """On a graph this clean the two agree. They part on graphs where
        Louvain returns a community connected only through outsiders, which
        is the case Leiden exists for."""
        rows = await _call(server, "gds_louvain", projection=projection, result_limit=100)
        assert _communities(rows, "communityId") == [set(LEFT), set(RIGHT)]
