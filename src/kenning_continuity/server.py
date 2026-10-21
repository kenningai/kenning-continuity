import json
import logging
from importlib.metadata import version as _package_version
from contextlib import asynccontextmanager
from typing import Any, Literal

from neo4j import AsyncGraphDatabase, Query, RoutingControl
from pydantic import Field
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fastmcp.server import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import TextContent
from neo4j.exceptions import Neo4jError
from mcp.types import ToolAnnotations

from fastmcp.server import http as fastmcp_http
from starlette.routing import Route

from .substrate import (
    TemporalKnowledgeSubstrate, KnowledgeEntity, KnowledgeType, Basis,
    KNOWLEDGE_TYPE_DESCRIPTIONS, PROCESS_TYPES,
    Connection, ConnectionType, CONNECTION_TYPE_DESCRIPTIONS,
    PROCESS_EDGES, PROCESS_TYPE_DESCRIPTIONS, DomainKnowledge,
    ONTOLOGICAL_TYPES, SUB_LABEL_TO_ONT_TYPE,
)
from .utils import literal_cypher, format_namespace, _is_write_query, _value_sanitize

logger = logging.getLogger('kenning_continuity')
logger.setLevel(logging.INFO)


# ── FastMCP Monkey Patch (bug in 2.13.0.2) ───────────────────

_original_create_streamable_http_app = fastmcp_http.create_streamable_http_app


def patched_create_streamable_http_app(*args, **kwargs):
    app = _original_create_streamable_http_app(*args, **kwargs)
    streamable_http_path = kwargs.get('streamable_http_path', '/mcp')
    for i, route in enumerate(app.routes):
        if isinstance(route, Route) and route.path == streamable_http_path:
            if not route.methods or route.methods == ['GET']:
                app.routes[i] = Route(
                    route.path, endpoint=route.endpoint,
                    methods=["GET", "POST", "DELETE"],
                )
                logger.info(f"Patched route {route.path} to accept GET, POST, DELETE")
                break
    return app


fastmcp_http.create_streamable_http_app = patched_create_streamable_http_app


# ── Tool Helpers ─────────────────────────────────────────────

@asynccontextmanager
async def _tool_errors(operation: str):
    logger.info(f"MCP tool: {operation}")
    try:
        yield
    except ValueError as e:
        raise ToolError(str(e))
    except Neo4jError as e:
        logger.error(f"Neo4j error in {operation}: {e}")
        raise ToolError(f"Neo4j error in {operation}: {e}")
    except Exception as e:
        logger.error(f"Error in {operation}: {e}")
        raise ToolError(f"Error in {operation}: {e}")


def _json_result(data, structured=None) -> ToolResult:
    text = json.dumps(data, indent=2, default=str)
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured if structured is not None else {"result": json.loads(text)},
    )


def _text_result(text: str, structured=None) -> ToolResult:
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured if structured is not None else {"result": text},
    )


# ── Server Factory ───────────────────────────────────────────

def create_mcp_server(
    substrate: TemporalKnowledgeSubstrate,
    namespace: str = "",
    read_timeout: int = 30,
) -> FastMCP:
    """Create an MCP server instance for the temporal knowledge substrate."""

    namespace_prefix = format_namespace(namespace)
    # The version the MCP handshake reports. FastMCP falls back to its own
    # unless told; read from installed metadata, never restated, so it cannot drift.
    mcp: FastMCP = FastMCP("kenning-continuity", version=_package_version("kenning-continuity"))

    # ── Process Tools ────────────────────────────────────────

    @mcp.tool(
        name=namespace_prefix + "list_domains",
        annotations=ToolAnnotations(title="List Domains", readOnlyHint=True,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def list_domains() -> ToolResult:
        """List all knowledge domains with session count and last activity."""
        async with _tool_errors("list_domains"):
            result = await substrate.list_domains()
            return _json_result(result)

    @mcp.tool(
        name=namespace_prefix + "create_domain",
        annotations=ToolAnnotations(title="Create Domain", readOnlyHint=False,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def create_domain(
        name: str = Field(..., description="Domain name (e.g. 'Telephony', 'Example Corp', 'Example Ops')"),
        description: str = Field(..., description="What this knowledge area covers"),
    ) -> ToolResult:
        """Create or retrieve a knowledge domain (idempotent via MERGE).

        Process-layer idempotence is structural honesty — re-MERGE produces
        the same anchor.

        Example: {"name": "Telephony", "description": "Contact centre platform operations"}
        """
        async with _tool_errors("create_domain"):
            result = await substrate.create_domain(name=name, description=description)
            return _json_result(result)

    @mcp.tool(
        name=namespace_prefix + "begin_session",
        annotations=ToolAnnotations(title="Begin Session", readOnlyHint=False,
                                    destructiveHint=False, idempotentHint=False, openWorldHint=True))
    async def begin_session(
        domain: str = Field(..., description="Domain name to begin session in"),
        purpose: str = Field(..., description="What this session is directed toward (REQUIRED)"),
        director: str = Field(..., description="Full name of the human directing this session, as injected by the platform (REQUIRED)"),
        director_key: str = Field(..., description="Corporate email address of that same human, as injected by the platform (REQUIRED)"),
    ) -> ToolResult:
        """Begin a new knowledge session in a domain.

        Creates the director's Person node if it is new, links it by DIRECTED,
        creates the Session, links HAS_SESSION from Domain and NEXT_SESSION
        from the previous Session.

        Returns the session_id (needed for all subsequent knowledge tools) and
        a COUNT of what is in the domain by type — not the domain state itself.
        Call get_domain_state with `types` and `limit` for the part you need.

        purpose is REQUIRED — silently-empty purpose makes the session's
        existence claim vacuous.

        director and director_key are BOTH REQUIRED and both come from the
        platform's own injection. Pass them as given; do not derive one from
        the other, do not substitute a placeholder, and do not invent either.
        If you do not have both, the session does not begin — ask for them.
        The email is the person's identity in this graph; the name alone
        cannot serve, because one human writes their name several ways.

        Example: {"domain": "Telephony", "purpose": "Document campaign routing logic and peak-hour traps", "director": "Bob Example", "director_key": "bob.example@example.com"}
        """
        async with _tool_errors("begin_session"):
            result = await substrate.begin_session(
                domain=domain, purpose=purpose,
                director=director, director_key=director_key,
            )
            return _json_result(result)

    @mcp.tool(
        name=namespace_prefix + "end_session",
        annotations=ToolAnnotations(title="End Session", readOnlyHint=False,
                                    destructiveHint=False, idempotentHint=False, openWorldHint=True))
    async def end_session(
        session_id: str = Field(..., description="Session ID returned by begin_session"),
        summary: str = Field(..., description="Session narrative — what was actually learned/done"),
    ) -> ToolResult:
        """End a knowledge session by recording what was measured into the graph.

        Sets the Session's summary and status to 'complete'. If never called,
        the Session stays active — an honest record of an incomplete session.
        """
        async with _tool_errors("end_session"):
            result = await substrate.end_session(session_id=session_id, summary=summary)
            return _json_result(result)

    # ── Knowledge Tools ──────────────────────────────────────

    @mcp.tool(
        name=namespace_prefix + "create_knowledge",
        annotations=ToolAnnotations(title="Create Knowledge", readOnlyHint=False,
                                    destructiveHint=False, idempotentHint=False, openWorldHint=True))
    async def create_knowledge(
        session_id: str = Field(..., description="Session ID from begin_session"),
        entities: list[KnowledgeEntity] = Field(..., description="List of knowledge entities to create"),
    ) -> ToolResult:
        """Create knowledge entities. REFUSES on collision.

        If any entity name+domain already exists as a head-of-chain knowledge
        node, the entire batch is rejected. Options on collision:
        (1) evolve_knowledge to record new content,
        (2) confirm_knowledge to record unchanged-encounter,
        (3) use a different name if this is a genuinely distinct thing.

        The `type` field accepts either an ontological type ('Actor',
        'Structure', 'Artifact', 'Event', 'Insight', 'Pattern', 'Challenge',
        'Solution', 'Lesson', 'Rationale') or a known sub-label
        ('PersonReferent', 'Organization', 'System', 'Process',
        'Configuration'). When a sub-label is given, the substrate
        auto-applies BOTH labels via Neo4j multi-labeling.

        Hard-rejects process types (Domain, Session, Person). A person named
        in a session is a PersonReferent — a reference held by whoever made
        it, correctly distinct from the same name referred to elsewhere. The
        person THEMSELF is a process-layer Person and exists only by having
        directed a session; nothing in the knowledge layer can reach one.

        Example:
        {
            "session_id": "abc-123",
            "entities": [
                {"name": "Example Contact Center", "type": "System", "description": "Cloud contact centre platform"},
                {"name": "Alice Example", "type": "PersonReferent", "description": "CEO of Example Corp"},
                {"name": "Peak Hour Allocation Trap", "type": "Lesson", "description": "Breaks above 200 concurrent agents"}
            ]
        }
        """
        async with _tool_errors("create_knowledge"):
            entity_objects = [KnowledgeEntity.model_validate(entity) for entity in entities]
            result = await substrate.create_knowledge(session_id, entity_objects)
            return _json_result([e.model_dump() for e in result])

    @mcp.tool(
        name=namespace_prefix + "evolve_knowledge",
        annotations=ToolAnnotations(title="Evolve Knowledge", readOnlyHint=False,
                                    destructiveHint=False, idempotentHint=False, openWorldHint=True))
    async def evolve_knowledge(
        session_id: str = Field(..., description="Session ID from begin_session"),
        name: str = Field(..., description="Name of the entity to evolve (must be head of chain)"),
        description: str = Field(..., description="New description for the evolved entity"),
        revision_why: str = Field(..., description="Why the description is changing: what was learned, corrected or found missing. Required. Stored on the EVOLVED_FROM edge — keep it out of the description, which says what the entity is."),
        domain: str = Field(default="", description="Domain (uses Session's domain if omitted)"),
        sub_labels: list[str] | None = Field(default=None, description="Optional new sub-label set. REPLACES old sub-labels. Inherit from old when omitted. Ontological type is preserved across the chain."),
        basis: str | None = Field(default=None, description="How this reached the graph on THIS occasion: 'stated', 'inferred', or 'read-from-document'. Rides the DISCOVERED edge."),
        sources: list[str] | None = Field(default=None, description="What this revision drew on: the documents, channels or people actually consulted, citing the original rather than a copy of it. Never compose a plausible source. Not inherited: what the revision carries forward stays cited on the version that first said it."),
        t_valid: str | None = Field(default=None, description="Optional ISO datetime of external validity for the new chain node."),
    ) -> ToolResult:
        """Evolve a knowledge entity — create a new version linked via EVOLVED_FROM.

        Every description-content change goes through evolve. The new chain
        node preserves the ontological type of the old (chain invariant);
        sub-labels can be replaced via the sub_labels parameter.

        Use this for ANY change to an entity's description — there is no
        update_knowledge in v0.7.0. The accumulated chain IS the substrate;
        each chain node preserves a phenomenological moment.

        Sub-label change example: an entity originally typed PersonReferent turns
        out to be a Trust. Evolve with sub_labels=['Organization'] — same
        Actor ontological type, different sub-classification, chain
        preserved.

        The reason for the change goes in revision_why, never in the
        description. A description that says "CORRECTION: ..." describes the
        record, not the entity, and the next reader inherits the argument
        instead of the fact.

        Example: {"session_id": "abc-123", "name": "Alice Example", "description": "CEO of Example Corp. Announced a succession plan.", "revision_why": "The succession plan was announced in the Q3 FY2025 filing; the prior description predates it."}
        """
        async with _tool_errors("evolve_knowledge"):
            result = await substrate.evolve_knowledge(
                session_id=session_id, name=name, description=description,
                revision_why=revision_why, domain=domain, sub_labels=sub_labels,
                basis=Basis(basis) if basis else None, sources=sources, t_valid=t_valid,
            )
            return _json_result(result)

    @mcp.tool(
        name=namespace_prefix + "confirm_knowledge",
        annotations=ToolAnnotations(title="Confirm Knowledge", readOnlyHint=False,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def confirm_knowledge(
        session_id: str = Field(..., description="Session ID from begin_session"),
        names: list[str] = Field(..., description="Names of entities to confirm (must be head of chain)"),
        domain: str = Field(default="", description="Domain (uses Session's domain if omitted)"),
    ) -> ToolResult:
        """Confirm knowledge entities — 'I encountered this and nothing changed.'

        Creates CONFIRMED edges from the current Session to head-of-chain
        entities. No new nodes, no mutations. The Session's provenance trail
        records the observation.

        Example: {"session_id": "abc-123", "names": ["Alice Example", "Example Corp"]}
        """
        async with _tool_errors("confirm_knowledge"):
            result = await substrate.confirm_knowledge(
                session_id=session_id, names=names, domain=domain,
            )
            return _json_result(result)

    @mcp.tool(
        name=namespace_prefix + "create_connections",
        annotations=ToolAnnotations(title="Create Connections", readOnlyHint=False,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def create_connections(
        session_id: str = Field(..., description="Session ID from begin_session — the session asserting these connections"),
        connections: list[Connection] = Field(..., description="List of connections to create"),
    ) -> ToolResult:
        """Create typed connections between knowledge entities.

        Every edge records the session that authored it. An edge is an
        assertion — that these two things stand in this relation — and it
        reaches the person who made it the same way an entity does.

        Hard-rejects process edge types (HAS_SESSION, NEXT_SESSION,
        DISCOVERED, CONFIRMED) and knowledge edges between two process-type
        nodes. Cross-domain connections ARE allowed — specify different
        source_domain and target_domain for cross-domain edges.

        ALWAYS specify source_domain and target_domain to avoid ambiguity
        when entities with the same name exist in multiple domains.

        Only matches head-of-chain entities (not phantom EVOLVED_FROM nodes).

        Example:
        {
            "connections": [
                {"source": "Q3 FY2025 Earnings", "target": "Alice Example", "type": "INFORMING",
                 "source_domain": "Example Corp", "target_domain": "Example Corp"},
                {"source": "Example Corp Tariff Exposure", "target": "Example Retail Tariff Exposure", "type": "ASSOCIATED_WITH",
                 "source_domain": "Example Corp", "target_domain": "Example Retail"}
            ]
        }
        """
        async with _tool_errors("create_connections"):
            connection_objects = [Connection.model_validate(c) for c in connections]
            result = await substrate.create_connections(session_id, connection_objects)
            return _json_result([c.model_dump() for c in result])

    @mcp.tool(
        name=namespace_prefix + "claim_referent",
        annotations=ToolAnnotations(title="Claim Referent", readOnlyHint=False,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def claim_referent(
        session_id: str = Field(..., description="Session ID from begin_session"),
        source: str = Field(..., description="Name of the first referent"),
        source_domain: str = Field(..., description="Domain of the first referent"),
        target: str = Field(..., description="Name of the second referent"),
        target_domain: str = Field(..., description="Domain of the second referent"),
        resolution: str = Field(..., description="'proposed' (your read, unconfirmed), 'confirmed' (put to the director, affirmed), or 'distinct' (put to the director, denied)"),
    ) -> ToolResult:
        """Claim that two knowledge referents refer to the same thing.

        A CLAIM, not a structural identity. In the knowledge layer everything is
        a referent held in possibility space by whoever referred to it, so
        seventeen "Alice Example" nodes across eleven domains are seventeen
        acts of reference, correctly distinct. Asserting two of them are one
        person is something someone does, in a session, and it can be wrong.

        Use 'distinct' when you asked and were told no. It is the value that
        stops you re-asking the same question every session forever — without
        somewhere to record the answer NO, the denial evaporates.

        Both endpoints must be head-of-chain knowledge entities. Neither may be
        a process node: a referent has no frame from which to perform an act of
        reference.

        Example: {"session_id": "abc-123", "source": "Alice Example", "source_domain": "Example Ops",
                  "target": "Alice Example", "target_domain": "Telephony", "resolution": "proposed"}
        """
        async with _tool_errors("claim_referent"):
            result = await substrate.claim_referent(
                session_id=session_id, source=source, source_domain=source_domain,
                target=target, target_domain=target_domain, resolution=resolution,
            )
            return _json_result(result)

    # ── Query Tools ──────────────────────────────────────────

    @mcp.tool(
        name=namespace_prefix + "search_knowledge",
        annotations=ToolAnnotations(title="Search Knowledge", readOnlyHint=True,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def search_knowledge(
        query: str = Field(..., description="Fulltext search query"),
        domain: str | None = Field(default=None, description="Optional domain scope (searches all if omitted)"),
        limit: int = Field(default=10, description="Max results (default 10, max 50)", ge=1, le=50),
    ) -> ToolResult:
        """Search knowledge entities using fulltext search across names and descriptions.

        Returns head-of-chain entities (current versions) and connections
        between them. To see prior chain content, use get_chain_history.
        Each entity carries the current version's `basis` and `sources`, as
        get_domain_state describes.

        Example: {"query": "campaign routing", "domain": "Telephony", "limit": 20}
        """
        async with _tool_errors("search_knowledge"):
            result = await substrate.search_knowledge(query=query, domain=domain, limit=limit)
            return _text_result(result.model_dump_json(), structured=result)

    @mcp.tool(
        name=namespace_prefix + "get_domain_state",
        annotations=ToolAnnotations(title="Get Domain State", readOnlyHint=True,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def get_domain_state(
        domain: str = Field(..., description="Domain name"),
        types: list[str] | None = Field(default=None, description="Optional: only these ontological types or sub-labels (e.g. ['Challenge'] or ['PersonReferent','Organization'])"),
        limit: int | None = Field(default=None, description="Optional: at most this many head entities, most recent first"),
        descriptions: bool = Field(default=True, description="Set false for names, types and connections without descriptions — the cheap shape of the domain"),
    ) -> ToolResult:
        """Get current (head-of-chain) knowledge entities for a domain.

        Returns "what do we know about this domain right now?" — entities
        and connections. To see prior chain content for any entity, use
        get_chain_history.

        Each entity carries the current version's `basis` and `sources`. A
        null means none was recorded on that version, which is common for
        anything written before they existed; it is not evidence that the
        knowledge is unsourced. A revised entity's sources name what the
        revision drew on, so read the chain before concluding what an
        entity rests on.
        """
        async with _tool_errors("get_domain_state"):
            result = await substrate.get_domain_state(
                domain=domain, types=types, limit=limit, descriptions=descriptions)
            return _text_result(result.model_dump_json(), structured=result)

    @mcp.tool(
        name=namespace_prefix + "get_session_history",
        annotations=ToolAnnotations(title="Get Session History", readOnlyHint=True,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def get_session_history(
        domain: str = Field(..., description="Domain name"),
        limit: int = Field(default=20, description="Max sessions to return (default 20)", ge=1),
    ) -> ToolResult:
        """Get the session history for a domain — who worked on what, when, and what they found.

        Ordered by timestamp (oldest → newest).
        """
        async with _tool_errors("get_session_history"):
            result = await substrate.get_session_history(domain=domain, limit=limit)
            return _json_result(result)

    @mcp.tool(
        name=namespace_prefix + "get_chain_history",
        annotations=ToolAnnotations(title="Get Chain History", readOnlyHint=True,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def get_chain_history(
        name: str = Field(..., description="Entity name"),
        domain: str = Field(..., description="Domain (required — same-named entities may exist in multiple domains)"),
    ) -> ToolResult:
        """Walk EVOLVED_FROM backward from head-of-chain. Returns chain ordered oldest-first.

        The accumulated chain IS the substrate. Each node preserves a
        phenomenological moment. Use this when search_knowledge's head-only
        view isn't enough — when you want to see what was previously
        understood about an entity.

        Each step carries `revision_why`, the reason it replaced the step
        before, and `discovered_by`, the session and director that wrote it.
        A null revision_why on the first step means it replaced nothing; on a
        later step it means the revision predates v0.2.0 and no reason was
        recorded. Do not infer one.

        Each step also carries its own `sources`, and each `discovered_by`
        entry the `basis` of that occasion. Provenance is per version: a
        revision cites what it drew on, and the claims it carried forward are
        cited on the step that first made them. Null means none was recorded.

        A claim an earlier version made and no later version contradicts was
        true as of when it was stated. When answering for someone new to a
        domain, read the chain on anything central that has been revised.

        Example: {"name": "Alice Example", "domain": "Example Corp"}
        """
        async with _tool_errors("get_chain_history"):
            result = await substrate.get_chain_history(name=name, domain=domain)
            return _json_result([e.model_dump() for e in result])

    @mcp.tool(
        name=namespace_prefix + "read_cypher",
        annotations=ToolAnnotations(title="Read Cypher", readOnlyHint=True,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def read_cypher(
        query: str = Field(..., description="The Cypher query to execute."),
        params: dict[str, Any] = Field(default_factory=dict, description="Parameters for the query."),
    ) -> ToolResult:
        """Execute a read-only Cypher query. Write queries are rejected."""
        if _is_write_query(query):
            raise ToolError(
                "Write queries are not allowed via read_cypher. "
                "Use the bounded mutation tools instead."
            )
        async with _tool_errors("read_cypher"):
            result = await substrate.driver.execute_query(
                # This is the one place caller text reaches the driver: the
                # read-only escape hatch. `_is_write_query` above has already
                # refused anything that mutates, and routing_ is READ. The
                # type error is CORRECT here — it is annotated, not dismissed.
                Query(literal_cypher(query), timeout=read_timeout),
                parameters_=params,
                routing_=RoutingControl.READ,
            )
            records = [r for r in (_value_sanitize(dict(r)) for r in result.records) if r is not None]
            text = json.dumps(records, indent=2, default=str)
            if len(text) > 200_000:
                text = text[:200_000] + "\n... (truncated — add LIMIT to your query)"
            return _text_result(text, structured={"result": records})

    # ── Taxonomy Tools ───────────────────────────────────────

    @mcp.tool(
        name=namespace_prefix + "list_knowledge_types",
        annotations=ToolAnnotations(title="List Knowledge Types", readOnlyHint=True,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=False))
    async def list_knowledge_types() -> ToolResult:
        """List allowed knowledge types: 10 ontological types + 5 sub-labels.

        Process types (Domain, Session) are created exclusively by process tools.

        Every knowledge node has exactly one ontological label and zero or
        more sub-labels via Neo4j multi-labeling. The `ont_type` property
        on each node names the ontological label deterministically.

        When create_knowledge is called with a sub-label as `type` (e.g.,
        'PersonReferent'), the substrate auto-applies BOTH the ontological
        label ('Actor') and the sub-label ('PersonReferent').
        """
        async with _tool_errors("list_knowledge_types"):
            ontological = {
                kt.value: KNOWLEDGE_TYPE_DESCRIPTIONS[kt]
                for kt in KnowledgeType if kt.value in ONTOLOGICAL_TYPES
            }
            sub_labels = {
                kt.value: {
                    "description": KNOWLEDGE_TYPE_DESCRIPTIONS[kt],
                    "ontological_type": SUB_LABEL_TO_ONT_TYPE[kt.value],
                }
                for kt in KnowledgeType if kt.value in SUB_LABEL_TO_ONT_TYPE
            }

            return _json_result({
                "process_types": {
                    "description": "Created exclusively by process tools. Nothing here is a referent — membership is the capacity to hold possible futures and measure them toward actualization.",
                    "types": PROCESS_TYPE_DESCRIPTIONS,
                },
                "ontological_types": {
                    "description": "Exactly one of these is the ontological type for any knowledge node.",
                    "count": len(ontological),
                    "types": ontological,
                },
                "sub_labels": {
                    "description": "Operational sub-classifications. Coexist with the ontological type via Neo4j multi-labeling.",
                    "count": len(sub_labels),
                    "labels": sub_labels,
                },
            })

    @mcp.tool(
        name=namespace_prefix + "list_connection_types",
        annotations=ToolAnnotations(title="List Connection Types", readOnlyHint=True,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=False))
    async def list_connection_types() -> ToolResult:
        """List all allowed connection types — 11 knowledge + 5 process edge types."""
        async with _tool_errors("list_connection_types"):
            knowledge_types = {
                ct.value: CONNECTION_TYPE_DESCRIPTIONS[ct] for ct in ConnectionType
            }

            process_types = {
                "HAS_SESSION": "Domain owns its Sessions (created by begin_session)",
                "NEXT_SESSION": "Session → Session, forward in time (created by begin_session)",
                "DIRECTED": "Person → Session, the noetic source of the act (created by begin_session)",
                "DISCOVERED": "Session → KnowledgeEntity (created by create_knowledge / evolve_knowledge)",
                "CONFIRMED": "Session → KnowledgeEntity, reviewed and still valid (created by confirm_knowledge)",
            }

            return _json_result({
                "total_types": len(ConnectionType) + len(process_types),
                "layers": {
                    "process": {
                        "description": "Created exclusively by process tools.",
                        "types": process_types,
                    },
                    "knowledge": {
                        "description": "Created via create_connections.",
                        "types": knowledge_types,
                    },
                },
            })

    # ── GDS Analytics Tools ──────────────────────────────────

    @mcp.tool(
        name=namespace_prefix + "gds_create_projection",
        annotations=ToolAnnotations(title="GDS Create Projection", readOnlyHint=False,
                                    destructiveHint=False, idempotentHint=False, openWorldHint=False))
    async def gds_create_projection(
        name: str = Field(..., description="Name for the graph projection"),
        domain: str | None = Field(default=None, description="Optional domain scope. When set, only projects entities touching this domain."),
        include_cross_domain: bool = Field(default=True, description="When domain is set, include cross-domain edges (default true)."),
        rel_types: list[str] | None = Field(default=None, description="Relationship types to include (defaults to all)"),
        undirected: bool = Field(default=True, description="Treat relationships as undirected (default true)"),
        memory_gb: str = Field(default="2GB", description="Memory allocation for the projection."),
    ) -> ToolResult:
        """Create a GDS graph projection for analytics — of what is known NOW.

        Each entity enters once, as its current version. Earlier versions in an
        EVOLVED_FROM chain are history, read with get_chain_history; they do not
        enter the projection, so an entity revised five times is one node, not
        five. An edge authored on an earlier version is resolved through the
        chain to the current version at both ends, as get_domain_state does, so
        no connection is lost by an entity having been revised. Duplicates are
        collapsed by (source, target, type). EVOLVED_FROM itself is never
        projected.

        Process-layer nodes (Domain, Session) are always excluded. Entities with
        no knowledge edges do not appear: the projection is built from edges.

        With a domain, an entity belongs to it when a session in that domain
        discovered it — the same walk every other tool uses. With
        include_cross_domain, an edge is projected when either end belongs;
        without, only when both do. A projection with nothing in it is
        refused rather than returned as null.
        """
        async with _tool_errors("gds_create_projection"):
            if rel_types:
                rel_filter = "|".join(f"`{rt}`" for rt in rel_types)
                match_clause = f"MATCH (a:Knowledge)-[r:{rel_filter}]->(b:Knowledge)"
            else:
                match_clause = "MATCH (a:Knowledge)-[r]->(b:Knowledge)"

            # Resolve both ends through their chains to the current version, so
            # an edge authored before either end was evolved is still projected,
            # and between the entities as they are now.
            match_clause += """
                WHERE type(r) <> 'EVOLVED_FROM'
                MATCH (source:Knowledge)-[:EVOLVED_FROM*0..]->(a)
                WHERE NOT (source)<-[:EVOLVED_FROM]-()
                MATCH (target:Knowledge)-[:EVOLVED_FROM*0..]->(b)
                WHERE NOT (target)<-[:EVOLVED_FROM]-()
                WITH DISTINCT source, target, type(r) AS rel_type"""

            where_parts = ["source <> target"]

            if domain:
                # Membership by walk, as everywhere else: knowledge nodes carry
                # no domain property, so a domain is reached, never read off.
                in_domain = (
                    "EXISTS {{ (:Domain {{name: $domain}})-[:HAS_SESSION]->(:Session)"
                    "-[:DISCOVERED]->({node}) }}"
                )
                joiner = " OR " if include_cross_domain else " AND "
                where_parts.append(
                    "(" + in_domain.format(node="source") + joiner
                    + in_domain.format(node="target") + ")"
                )

            where_clause = "WHERE " + " AND ".join(where_parts)

            config_parts = [f"memory: '{memory_gb}'"]
            if undirected:
                config_parts.append("undirectedRelationshipTypes: ['*']")

            query = f"""
                {match_clause}
                {where_clause}
                RETURN gds.graph.project($name, source, target, {{}}, {{{", ".join(config_parts)}}})
            """
            params: dict[str, Any] = {"name": name}
            if domain:
                params["domain"] = domain

            # built from config_parts, assembled above from typed arguments;
            # name and domain travel as $params
            result = await substrate.driver.execute_query(
                literal_cypher(query), parameters_=params, routing_=RoutingControl.READ)
            projected = [r for r in (_value_sanitize(dict(r)) for r in result.records) if r is not None]

            # gds.graph.project aggregates over the matched edges; over none it
            # returns null and creates nothing, which reads as success.
            if not any(v is not None for row in projected for v in row.values()):
                scope = f"domain '{domain}'" if domain else "the graph"
                hint = (" Check the domain name with list_domains, or set "
                        "include_cross_domain." if domain else "")
                raise ValueError(
                    f"Nothing to project: no knowledge edges in {scope} match "
                    f"the requested relationship types. No projection named "
                    f"'{name}' was created.{hint}"
                )
            return _json_result(projected)

    @mcp.tool(
        name=namespace_prefix + "gds_drop_projection",
        annotations=ToolAnnotations(title="GDS Drop Projection", readOnlyHint=False,
                                    destructiveHint=True, idempotentHint=True, openWorldHint=False))
    async def gds_drop_projection(
        name: str = Field(..., description="Name of the graph projection to drop"),
    ) -> ToolResult:
        """Drop a GDS graph projection to free memory."""
        async with _tool_errors("gds_drop_projection"):
            result = await substrate.driver.execute_query(
                "CALL gds.graph.drop($name)", name=name, routing_=RoutingControl.WRITE)
            return _json_result([dict(r) for r in result.records])

    @mcp.tool(
        name=namespace_prefix + "gds_pagerank",
        annotations=ToolAnnotations(title="GDS PageRank", readOnlyHint=True,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=False))
    async def gds_pagerank(
        projection: str = Field(..., description="Name of the graph projection"),
        result_limit: int = Field(default=20, ge=1, le=1000, description="Max results (default 20)"),
    ) -> ToolResult:
        """Run PageRank on a GDS graph projection. Requires gds_create_projection first."""
        async with _tool_errors("gds_pagerank"):
            result = await substrate.driver.execute_query(
                "CALL gds.pageRank.stream($projection) YIELD nodeId, score "
                "RETURN gds.util.asNode(nodeId).name AS node, "
                "gds.util.asNode(nodeId).ont_type AS type, "
                "score "
                "ORDER BY score DESC LIMIT $result_limit",
                projection=projection, result_limit=result_limit, routing_=RoutingControl.READ,
            )
            return _json_result([dict(r) for r in result.records])

    @mcp.tool(
        name=namespace_prefix + "gds_articlerank",
        annotations=ToolAnnotations(title="GDS ArticleRank", readOnlyHint=True,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=False))
    async def gds_articlerank(
        projection: str = Field(..., description="Name of the graph projection"),
        result_limit: int = Field(default=20, ge=1, le=1000, description="Max results (default 20)"),
    ) -> ToolResult:
        """Run ArticleRank on a GDS graph projection — PageRank with hub transmission damped.

        A node linked from an entity that links to everything gains less
        here than under PageRank. An entity that ranks high under
        gds_pagerank and falls under this one owes its centrality to hubs,
        not to being widely relied on. Run both on the same projection and
        read the gap.
        """
        async with _tool_errors("gds_articlerank"):
            result = await substrate.driver.execute_query(
                "CALL gds.articleRank.stream($projection) YIELD nodeId, score "
                "RETURN gds.util.asNode(nodeId).name AS node, "
                "gds.util.asNode(nodeId).ont_type AS type, "
                "score "
                "ORDER BY score DESC LIMIT $result_limit",
                projection=projection, result_limit=result_limit, routing_=RoutingControl.READ,
            )
            return _json_result([dict(r) for r in result.records])

    @mcp.tool(
        name=namespace_prefix + "gds_betweenness",
        annotations=ToolAnnotations(title="GDS Betweenness Centrality", readOnlyHint=True,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=False))
    async def gds_betweenness(
        projection: str = Field(..., description="Name of the graph projection"),
        result_limit: int = Field(default=20, ge=1, le=1000, description="Max results (default 20)"),
    ) -> ToolResult:
        """Run Betweenness Centrality on a GDS graph projection."""
        async with _tool_errors("gds_betweenness"):
            result = await substrate.driver.execute_query(
                "CALL gds.betweenness.stream($projection) YIELD nodeId, score "
                "RETURN gds.util.asNode(nodeId).name AS node, "
                "gds.util.asNode(nodeId).ont_type AS type, "
                "score "
                "ORDER BY score DESC LIMIT $result_limit",
                projection=projection, result_limit=result_limit, routing_=RoutingControl.READ,
            )
            return _json_result([dict(r) for r in result.records])

    @mcp.tool(
        name=namespace_prefix + "gds_louvain",
        annotations=ToolAnnotations(title="GDS Louvain", readOnlyHint=True,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=False))
    async def gds_louvain(
        projection: str = Field(..., description="Name of the graph projection"),
        result_limit: int = Field(default=20, ge=1, le=1000, description="Max results (default 20)"),
    ) -> ToolResult:
        """Run Louvain community detection on a GDS graph projection."""
        async with _tool_errors("gds_louvain"):
            result = await substrate.driver.execute_query(
                "CALL gds.louvain.stream($projection) YIELD nodeId, communityId "
                "RETURN gds.util.asNode(nodeId).name AS node, "
                "gds.util.asNode(nodeId).ont_type AS type, "
                "communityId "
                "ORDER BY communityId, node LIMIT $result_limit",
                projection=projection, result_limit=result_limit, routing_=RoutingControl.READ,
            )
            return _json_result([dict(r) for r in result.records])

    @mcp.tool(
        name=namespace_prefix + "gds_leiden",
        annotations=ToolAnnotations(title="GDS Leiden", readOnlyHint=True,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=False))
    async def gds_leiden(
        projection: str = Field(..., description="Name of the graph projection (must be undirected, the default)"),
        result_limit: int = Field(default=20, ge=1, le=1000, description="Max results (default 20)"),
    ) -> ToolResult:
        """Run Leiden community detection on a GDS graph projection.

        Leiden guarantees every community it returns is internally
        connected; Louvain can return a community whose members are joined
        only through nodes outside it. Use Leiden when the communities will
        be read as real groupings. Requires an undirected projection.
        """
        async with _tool_errors("gds_leiden"):
            result = await substrate.driver.execute_query(
                "CALL gds.leiden.stream($projection) YIELD nodeId, communityId "
                "RETURN gds.util.asNode(nodeId).name AS node, "
                "gds.util.asNode(nodeId).ont_type AS type, "
                "communityId "
                "ORDER BY communityId, node LIMIT $result_limit",
                projection=projection, result_limit=result_limit, routing_=RoutingControl.READ,
            )
            return _json_result([dict(r) for r in result.records])

    @mcp.tool(
        name=namespace_prefix + "gds_wcc",
        annotations=ToolAnnotations(title="GDS Weakly Connected Components", readOnlyHint=True,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=False))
    async def gds_wcc(
        projection: str = Field(..., description="Name of the graph projection"),
        result_limit: int = Field(default=20, ge=1, le=1000, description="Max results (default 20)"),
    ) -> ToolResult:
        """Run Weakly Connected Components on a GDS graph projection."""
        async with _tool_errors("gds_wcc"):
            result = await substrate.driver.execute_query(
                "CALL gds.wcc.stream($projection) YIELD nodeId, componentId "
                "RETURN gds.util.asNode(nodeId).name AS node, "
                "gds.util.asNode(nodeId).ont_type AS type, "
                "componentId "
                "ORDER BY componentId, node LIMIT $result_limit",
                projection=projection, result_limit=result_limit, routing_=RoutingControl.READ,
            )
            return _json_result([dict(r) for r in result.records])

    return mcp


# ── Server Entry Point ───────────────────────────────────────

async def main(
    neo4j_uri: str,
    neo4j_user: str,
    neo4j_password: str,
    neo4j_database: str,
    transport: Literal["stdio", "sse", "streamable-http"] = "stdio",
    namespace: str = "",
    # None under stdio, where a host/port/path is meaningless. process_config
    # emits None for exactly that case; the signature says so rather than
    # claiming a str it will not always receive.
    host: str | None = "127.0.0.1",
    port: int | None = 8000,
    path: str | None = "/mcp/",
    allow_origins: list[str] | None = None,
    allowed_hosts: list[str] | None = None,
    read_timeout: int = 30,
) -> None:
    allow_origins = allow_origins if allow_origins is not None else []
    allowed_hosts = allowed_hosts if allowed_hosts is not None else []

    logger.info("Starting Kenning Continuity MCP Server")
    logger.info(f"Connecting to Neo4j with DB URL: {neo4j_uri}")

    # UNRECOGNIZED is suppressed and DEPRECATION deliberately is not.
    # UNRECOGNIZED is the 01N51 "label/relationship type does not exist" class,
    # which fires on every query naming a label the graph has no nodes for yet.
    # It carries no information about this server — a guard querying for a thing
    # that is legitimately absent is the guard working — and at one warning per
    # query it buries the notifications that do carry information.
    # DEPRECATION does carry information: it names Cypher that a future Neo4j
    # will refuse, and silencing it means finding out on an upgrade instead.
    neo4j_driver = AsyncGraphDatabase.driver(
        neo4j_uri, auth=(neo4j_user, neo4j_password), database=neo4j_database,
        notifications_disabled_classifications=["UNRECOGNIZED"])

    try:
        await neo4j_driver.verify_connectivity()
        logger.info(f"Connected to Neo4j at {neo4j_uri}")
    except Exception as e:
        logger.error(f"Failed to connect to Neo4j: {e}")
        exit(1)

    substrate = TemporalKnowledgeSubstrate(neo4j_driver)
    await substrate.create_fulltext_index()
    await substrate.ensure_schema()

    custom_middleware = [
        Middleware(CORSMiddleware, allow_origins=allow_origins,
                   allow_methods=["GET", "POST"], allow_headers=["*"]),
        Middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts),
    ]

    mcp = create_mcp_server(substrate, namespace, read_timeout=read_timeout)

    match transport:
        case "streamable-http":
            await mcp.run_http_async(host=host, port=port, path=path, middleware=custom_middleware,
                                     transport="streamable-http", stateless_http=True)
        case "stdio":
            await mcp.run_stdio_async()
        case "sse":
            await mcp.run_http_async(host=host, port=port, path=path, middleware=custom_middleware, transport="sse")
        case _:
            raise ValueError(f"Unsupported transport: {transport}")
