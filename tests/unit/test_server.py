import pytest
from unittest.mock import Mock, AsyncMock
from pydantic import ValidationError

from kenning_continuity.server import create_mcp_server
from kenning_continuity.utils import format_namespace
from kenning_continuity.substrate import (
    Basis,
    TemporalKnowledgeSubstrate, KnowledgeEntity, KnowledgeType,
    KNOWLEDGE_TYPE_DESCRIPTIONS, PROCESS_TYPES,
    Connection, ConnectionType, CONNECTION_TYPE_DESCRIPTIONS,
    PROCESS_EDGES, DomainKnowledge,
    ONTOLOGICAL_TYPES, SUB_LABEL_TO_ONT_TYPE,
    _resolve_labels, _labels_string, _split_labels,
)


async def _tool_names(server):
    tools = await server.list_tools()
    return {t.name for t in tools}


async def _get_tool(server, name):
    return await server.get_tool(name)


# ── Helpers ──────────────────────────────────────────────────


class TestFormatNamespace:
    def test_format_namespace_empty_string(self):
        assert format_namespace("") == ""

    def test_format_namespace_no_hyphen(self):
        assert format_namespace("myapp") == "myapp-"

    def test_format_namespace_with_hyphen(self):
        assert format_namespace("myapp-") == "myapp-"

    def test_format_namespace_complex_name(self):
        assert format_namespace("company.product") == "company.product-"
        assert format_namespace("app_v2") == "app_v2-"


# ── Server Surface ───────────────────────────────────────────


class TestNamespacing:
    @pytest.fixture
    def mock_substrate(self):
        substrate = Mock(spec=TemporalKnowledgeSubstrate)
        domain_knowledge = DomainKnowledge(entities=[], connections=[])
        substrate.list_domains = AsyncMock(return_value=[])
        substrate.create_domain = AsyncMock(return_value={"name": "test", "description": "", "t_created": None})
        substrate.begin_session = AsyncMock(return_value={"session_id": "test-123", "domain": "test", "timestamp": None, "domain_state": {}})
        substrate.end_session = AsyncMock(return_value={"session_id": "test-123", "domain": "test", "timestamp": None, "purpose": "p", "summary": "done", "status": "complete", "entities_discovered": 0, "entities_confirmed": 0})
        substrate.create_knowledge = AsyncMock(return_value=[])
        substrate.evolve_knowledge = AsyncMock(return_value={"name": "test", "ont_type": "Actor", "labels": ["Actor", "PersonReferent"], "domain": "test", "description": "evolved", "evolved": True})
        substrate.confirm_knowledge = AsyncMock(return_value=[{"name": "test", "ont_type": "Structure", "labels": ["Structure", "System"], "domain": "test", "description": "test", "confirmed": True}])
        substrate.create_connections = AsyncMock(return_value=[])
        substrate.search_knowledge = AsyncMock(return_value=domain_knowledge)
        substrate.get_domain_state = AsyncMock(return_value=domain_knowledge)
        substrate.get_session_history = AsyncMock(return_value=[])
        substrate.get_chain_history = AsyncMock(return_value=[])
        substrate.driver = AsyncMock()
        substrate.driver.execute_query = AsyncMock(return_value=Mock(records=[]))
        return substrate

    @pytest.mark.asyncio
    async def test_namespace_tool_prefixes(self, mock_substrate):
        namespaced_names = await _tool_names(create_mcp_server(mock_substrate, namespace="test-ns"))

        expected_tools = [
            "test-ns-list_domains", "test-ns-create_domain",
            "test-ns-begin_session", "test-ns-end_session",
            "test-ns-create_knowledge", "test-ns-evolve_knowledge",
            "test-ns-confirm_knowledge",
            "test-ns-create_connections",
            "test-ns-search_knowledge", "test-ns-get_domain_state",
            "test-ns-get_session_history", "test-ns-get_chain_history",
            "test-ns-read_cypher",
            "test-ns-list_knowledge_types", "test-ns-list_connection_types",
            "test-ns-gds_create_projection", "test-ns-gds_drop_projection",
            "test-ns-gds_pagerank", "test-ns-gds_articlerank",
            "test-ns-gds_betweenness",
            "test-ns-gds_louvain", "test-ns-gds_leiden", "test-ns-gds_wcc",
        ]

        for expected_tool in expected_tools:
            assert expected_tool in namespaced_names, f"Tool {expected_tool} not found"

        default_names = await _tool_names(create_mcp_server(mock_substrate))
        for expected_tool in expected_tools:
            unprefixed = expected_tool.replace("test-ns-", "")
            assert unprefixed in default_names, f"Default tool {unprefixed} not found"

    @pytest.mark.asyncio
    async def test_removed_tools_absent(self, mock_substrate):
        """v0.7.0: update_knowledge and retype_knowledge are removed."""
        names = await _tool_names(create_mcp_server(mock_substrate))
        assert "update_knowledge" not in names
        assert "retype_knowledge" not in names
        assert "get_run_history" not in names  # renamed to get_session_history

    @pytest.mark.asyncio
    async def test_merge_knowledge_is_gone(self, mock_substrate):
        """v0.2.0: no tool compacts a chain.

        The human is the object here, and the system does not decide what
        they hold as true. Asserted on the substrate as well as the surface,
        so it cannot come back as a method some later tool reaches for.
        """
        names = await _tool_names(create_mcp_server(mock_substrate))
        assert "merge_knowledge" not in names
        assert not hasattr(TemporalKnowledgeSubstrate, "merge_knowledge")

    @pytest.mark.asyncio
    async def test_namespace_tool_functionality(self, mock_substrate):
        server = create_mcp_server(mock_substrate, namespace="test")
        tool = await _get_tool(server, "test-search_knowledge")
        assert tool is not None

        await tool.fn(query="test", limit=10)
        mock_substrate.search_knowledge.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_namespace_isolation(self, mock_substrate):
        names_a = await _tool_names(create_mcp_server(mock_substrate, namespace="app-a"))
        names_b = await _tool_names(create_mcp_server(mock_substrate, namespace="app-b"))

        assert "app-a-search_knowledge" in names_a
        assert "app-a-search_knowledge" not in names_b
        assert "app-b-search_knowledge" in names_b
        assert "app-b-search_knowledge" not in names_a
        assert len(names_a) == len(names_b)

    @pytest.mark.asyncio
    async def test_namespace_hyphen_handling(self, mock_substrate):
        names_with = await _tool_names(create_mcp_server(mock_substrate, namespace="myapp-"))
        names_without = await _tool_names(create_mcp_server(mock_substrate, namespace="myapp"))
        assert "myapp-search_knowledge" in names_with
        assert "myapp-search_knowledge" in names_without
        assert names_with == names_without

    @pytest.mark.asyncio
    async def test_complex_namespace_names(self, mock_substrate):
        for namespace in ["company.product", "app_v2", "client-123", "test.env.staging"]:
            names = await _tool_names(create_mcp_server(mock_substrate, namespace=namespace))
            expected = f"{namespace}-search_knowledge"
            assert expected in names

    def test_server_reports_its_own_version(self, mock_substrate):
        """The MCP handshake's serverInfo.version is this package's, not fastmcp's."""
        from importlib.metadata import version
        server = create_mcp_server(mock_substrate)
        assert server.version == version("kenning-continuity")
        assert server.version != version("fastmcp")

    @pytest.mark.asyncio
    async def test_tool_count(self, mock_substrate):
        """24 tools: 4 process + 5 knowledge + 5 query + 2 taxonomy + 8 GDS.

        v0.8.0 of the predecessor added claim_referent, co-reference as an
        authored claim, for 23. v0.2.0 removes merge_knowledge and adds
        gds_articlerank and gds_leiden, for 24.
        """
        default_names = await _tool_names(create_mcp_server(mock_substrate))
        assert len(default_names) == 24
        assert "claim_referent" in default_names


# ── Type System ──────────────────────────────────────────────


class TestKnowledgeTypeEnum:
    def test_enum_has_18_values(self):
        """10 ontological + 5 sub-labels = 15."""
        assert len(KnowledgeType) == 18

    def test_ontological_types_count(self):
        assert len(ONTOLOGICAL_TYPES) == 11

    def test_sub_label_count(self):
        assert len(SUB_LABEL_TO_ONT_TYPE) == 7

    def test_all_ontological_types_in_enum(self):
        enum_values = {kt.value for kt in KnowledgeType}
        for ont in ONTOLOGICAL_TYPES:
            assert ont in enum_values, f"Ontological type {ont} missing from enum"

    def test_all_sub_labels_in_enum(self):
        enum_values = {kt.value for kt in KnowledgeType}
        for sub in SUB_LABEL_TO_ONT_TYPE.keys():
            assert sub in enum_values, f"Sub-label {sub} missing from enum"

    def test_sub_label_targets_are_ontological(self):
        for sub, ont in SUB_LABEL_TO_ONT_TYPE.items():
            assert ont in ONTOLOGICAL_TYPES, f"Sub-label {sub} maps to non-ontological {ont}"

    def test_ontological_types_disjoint_from_sub_labels(self):
        assert ONTOLOGICAL_TYPES.isdisjoint(SUB_LABEL_TO_ONT_TYPE.keys())

    def test_no_ontological_type_collides_with_process_types(self):
        for ont in ONTOLOGICAL_TYPES:
            assert ont not in PROCESS_TYPES

    def test_v0_6_x_legacy_types_removed(self):
        """v0.7.0: Gotcha → Lesson, Change → Event, Dependency removed, Artifact kept."""
        enum_values = {kt.value for kt in KnowledgeType}
        assert "Gotcha" not in enum_values
        assert "Change" not in enum_values
        assert "Dependency" not in enum_values
        assert "Lesson" in enum_values
        assert "Event" in enum_values
        assert "Artifact" in enum_values


class TestConnectionTypeEnum:
    def test_enum_has_13_values(self):
        assert len(ConnectionType) == 13

    def test_knowledge_focus_edges_present(self):
        values = {ct.value for ct in ConnectionType}
        for new_edge in ["EXTENDING", "RECOGNITION", "VALIDATION", "INVALIDATING", "ASSOCIATED_WITH"]:
            assert new_edge in values

    def test_v0_6_x_legacy_edges_removed(self):
        values = {ct.value for ct in ConnectionType}
        assert "RELATES_TO" not in values

    def test_no_connection_type_collides_with_process_edges(self):
        for ct in ConnectionType:
            assert ct.value not in PROCESS_EDGES


class TestProcessLayerGuards:
    def test_process_types_set(self):
        assert PROCESS_TYPES == {"Domain", "Session", "Person"}

    def test_process_edges_set(self):
        assert PROCESS_EDGES == {"HAS_SESSION", "NEXT_SESSION", "DISCOVERED",
                                 "CONFIRMED", "DIRECTED"}

    def test_v0_6_x_legacy_process_labels_removed(self):
        assert "Run" not in PROCESS_TYPES
        assert "HAS_RUN" not in PROCESS_EDGES
        assert "NEXT_RUN" not in PROCESS_EDGES


# ── Label Resolution ─────────────────────────────────────────


class TestLabelResolution:
    def test_ontological_type_alone(self):
        ont, subs = _resolve_labels("Actor", None)
        assert ont == "Actor"
        assert subs == []

    def test_sub_label_auto_derives_ontological(self):
        ont, subs = _resolve_labels("PersonReferent", None)
        assert ont == "Actor"
        assert subs == ["PersonReferent"]

    def test_ontological_with_explicit_sub_labels(self):
        ont, subs = _resolve_labels("Actor", ["PersonReferent", "OperationalAgent"])
        assert ont == "Actor"
        assert subs == ["PersonReferent", "OperationalAgent"]

    def test_sub_label_with_extra_sub_labels(self):
        ont, subs = _resolve_labels("PersonReferent", ["OperationalAgent"])
        assert ont == "Actor"
        assert subs == ["PersonReferent", "OperationalAgent"]

    def test_dedup_when_extra_repeats_auto_sub_label(self):
        ont, subs = _resolve_labels("PersonReferent", ["PersonReferent", "OperationalAgent"])
        assert ont == "Actor"
        assert subs == ["PersonReferent", "OperationalAgent"]

    def test_unknown_type_errors(self):
        with pytest.raises(ValueError):
            _resolve_labels("Bogus", None)

    def test_labels_string_includes_backticks(self):
        s = _labels_string("Actor", ["PersonReferent"])
        assert s == "`Knowledge`:`Actor`:`PersonReferent`"

    def test_labels_string_no_sub_labels(self):
        s = _labels_string("Insight", [])
        assert s == "`Knowledge`:`Insight`"

    def test_split_labels_returns_ontological_and_subs(self):
        ont, subs = _split_labels(["Actor", "PersonReferent"])
        assert ont == "Actor"
        assert subs == ["PersonReferent"]

    def test_split_labels_handles_process_labels(self):
        ont, subs = _split_labels(["Session"])
        assert ont is None
        assert subs == []

    def test_split_labels_only_ontological(self):
        ont, subs = _split_labels(["Insight"])
        assert ont == "Insight"
        assert subs == []


# ── KnowledgeEntity Model ────────────────────────────────────


class TestKnowledgeEntityModel:
    def test_accepts_ontological_type(self):
        entity = KnowledgeEntity(name="X", type="Actor")
        assert entity.type == KnowledgeType.ACTOR

    def test_accepts_sub_label_as_type(self):
        entity = KnowledgeEntity(name="Alice Example", type="PersonReferent")
        assert entity.type == KnowledgeType.PERSON_REFERENT

    def test_accepts_event_type(self):
        entity = KnowledgeEntity(name="Q3 Earnings", type="Event")
        assert entity.type == KnowledgeType.EVENT

    def test_accepts_lesson_type(self):
        entity = KnowledgeEntity(name="Allocation Trap", type="Lesson")
        assert entity.type == KnowledgeType.LESSON

    def test_rejects_legacy_types(self):
        for legacy in ["Change", "Gotcha", "Dependency"]:
            with pytest.raises(ValidationError):
                KnowledgeEntity(name="X", type=legacy)

    def test_rejects_unknown_type(self):
        with pytest.raises(ValidationError):
            KnowledgeEntity(name="X", type="FakeType")

    def test_case_sensitive(self):
        with pytest.raises(ValidationError):
            KnowledgeEntity(name="X", type="actor")

    def test_sub_labels_field_optional(self):
        entity = KnowledgeEntity(name="X", type="Actor")
        assert entity.sub_labels is None

    def test_sub_labels_field_accepts_list(self):
        entity = KnowledgeEntity(name="X", type="Actor", sub_labels=["PersonReferent", "OperationalAgent"])
        assert entity.sub_labels == ["PersonReferent", "OperationalAgent"]

    def test_ont_type_field_default_none(self):
        entity = KnowledgeEntity(name="X", type="Actor")
        assert entity.ont_type is None

    def test_labels_field_default_none(self):
        entity = KnowledgeEntity(name="X", type="Actor")
        assert entity.labels is None

    def test_optional_basis(self):
        entity = KnowledgeEntity(name="X", type="Insight", basis="stated")
        assert entity.basis == Basis.STATED

    def test_basis_vocabulary_is_closed(self):
        with pytest.raises(ValidationError):
            KnowledgeEntity(name="X", type="Insight", basis="probably")

    def test_confidence_is_gone(self):
        """Removed in v0.8.0, not relocated.

        Someone's confidence on an occasion is a fact about their conscious
        experience at that moment — category-1, and it does not transmit: a
        later reader derives their own from the description. It was set on 2
        entities out of 2,629, valued 1.0 and 0.95, and nobody meant the
        difference. What it reached for lives in VALIDATION / INVALIDATING and
        in a Challenge with no paired Solution.
        """
        with pytest.raises(ValidationError):
            KnowledgeEntity(name="X", type="Insight", confidence=0.8)


# ── Connection Model ─────────────────────────────────────────


class TestConnectionModel:
    def test_accepts_existing_edges(self):
        for ct_value in ["EVOLVED_FROM", "ENABLING", "REQUIRING", "INFORMING", "CAUSING", "COMPOSING"]:
            c = Connection(source="A", target="B", type=ct_value)
            assert c.type.value == ct_value

    def test_accepts_new_edges(self):
        for ct_value in ["EXTENDING", "RECOGNITION", "VALIDATION", "INVALIDATING", "ASSOCIATED_WITH"]:
            c = Connection(source="A", target="B", type=ct_value)
            assert c.type.value == ct_value

    def test_rejects_relates_to(self):
        with pytest.raises(ValidationError):
            Connection(source="A", target="B", type="RELATES_TO")

    def test_requires_source_and_target(self):
        with pytest.raises(ValidationError):
            Connection(source="", target="B", type="INFORMING")
        with pytest.raises(ValidationError):
            Connection(source="A", target="", type="INFORMING")

    def test_t_created_optional(self):
        c = Connection(source="A", target="B", type="INFORMING")
        assert c.t_created is None


# ── Taxonomy Tools ───────────────────────────────────────────


class TestListKnowledgeTypes:
    @pytest.fixture
    def mock_substrate(self):
        substrate = Mock(spec=TemporalKnowledgeSubstrate)
        substrate.driver = AsyncMock()
        substrate.driver.execute_query = AsyncMock(return_value=Mock(records=[]))
        return substrate

    @pytest.mark.asyncio
    async def test_returns_three_layers(self, mock_substrate):
        server = create_mcp_server(mock_substrate)
        tool = await _get_tool(server, "list_knowledge_types")
        result = await tool.fn()
        import json
        data = json.loads(result.content[0].text)
        assert "process_types" in data
        assert set(data["process_types"]["types"]) == PROCESS_TYPES, (
            "list_knowledge_types must report every process type — Person was "
            "added to PROCESS_TYPES at v0.8.0 and the tool kept reporting two")
        assert "ontological_types" in data
        assert "sub_labels" in data

    @pytest.mark.asyncio
    async def test_ontological_count_11(self, mock_substrate):
        server = create_mcp_server(mock_substrate)
        tool = await _get_tool(server, "list_knowledge_types")
        result = await tool.fn()
        import json
        data = json.loads(result.content[0].text)
        assert data["ontological_types"]["count"] == 11
        assert len(data["ontological_types"]["types"]) == 11

    @pytest.mark.asyncio
    async def test_sub_labels_count_7(self, mock_substrate):
        server = create_mcp_server(mock_substrate)
        tool = await _get_tool(server, "list_knowledge_types")
        result = await tool.fn()
        import json
        data = json.loads(result.content[0].text)
        assert data["sub_labels"]["count"] == 7
        assert len(data["sub_labels"]["labels"]) == 7

    @pytest.mark.asyncio
    async def test_process_types_session_not_run(self, mock_substrate):
        server = create_mcp_server(mock_substrate)
        tool = await _get_tool(server, "list_knowledge_types")
        result = await tool.fn()
        import json
        data = json.loads(result.content[0].text)
        assert "Session" in data["process_types"]["types"]
        assert "Run" not in data["process_types"]["types"]

    @pytest.mark.asyncio
    async def test_sub_labels_carry_ontological_mapping(self, mock_substrate):
        server = create_mcp_server(mock_substrate)
        tool = await _get_tool(server, "list_knowledge_types")
        result = await tool.fn()
        import json
        data = json.loads(result.content[0].text)
        for sub_name, sub_info in data["sub_labels"]["labels"].items():
            assert "ontological_type" in sub_info
            assert sub_info["ontological_type"] in ONTOLOGICAL_TYPES


class TestListConnectionTypes:
    @pytest.fixture
    def mock_substrate(self):
        substrate = Mock(spec=TemporalKnowledgeSubstrate)
        substrate.driver = AsyncMock()
        substrate.driver.execute_query = AsyncMock(return_value=Mock(records=[]))
        return substrate

    @pytest.mark.asyncio
    async def test_total_16(self, mock_substrate):
        """11 knowledge + 5 process = 16. DIRECTED joined the process set at v0.8.0."""
        server = create_mcp_server(mock_substrate)
        tool = await _get_tool(server, "list_connection_types")
        result = await tool.fn()
        import json
        data = json.loads(result.content[0].text)
        assert data["total_types"] == 18
        assert len(data["layers"]["knowledge"]["types"]) == 13
        assert len(data["layers"]["process"]["types"]) == 5

    @pytest.mark.asyncio
    async def test_process_layer_session_edges(self, mock_substrate):
        server = create_mcp_server(mock_substrate)
        tool = await _get_tool(server, "list_connection_types")
        result = await tool.fn()
        import json
        data = json.loads(result.content[0].text)
        assert "HAS_SESSION" in data["layers"]["process"]["types"]
        assert "NEXT_SESSION" in data["layers"]["process"]["types"]
        assert "HAS_RUN" not in data["layers"]["process"]["types"]


# ── DomainKnowledge ──────────────────────────────────────────


class TestDomainKnowledge:
    def test_empty_default(self):
        dk = DomainKnowledge()
        assert dk.entities == []
        assert dk.connections == []

    def test_with_data(self):
        dk = DomainKnowledge(
            entities=[KnowledgeEntity(name="X", type="Actor", description="An actor")],
            connections=[Connection(source="A", target="B", type="EXTENDING")],
        )
        assert len(dk.entities) == 1
        assert len(dk.connections) == 1


class TestToolsForwardWhatTheyDeclare:
    """A tool that advertises a parameter and drops it is silent.

    `get_domain_state` declared `types`, `limit` and `descriptions`, then called
    `substrate.get_domain_state(domain=domain)`. Three parameters accepted from
    the caller and discarded. Every test passed, because the substrate is mocked
    and nothing compared what the tool received against what it forwarded. It
    was found by calling the running server with `limit=3` and getting 181
    entities back.
    """

    @pytest.fixture
    def mock_substrate(self):
        substrate = Mock(spec=TemporalKnowledgeSubstrate)
        substrate.get_domain_state = AsyncMock(
            return_value=DomainKnowledge(entities=[], connections=[]))
        return substrate

    @pytest.mark.asyncio
    async def test_get_domain_state_forwards_all_of_them(self, mock_substrate):
        server = create_mcp_server(mock_substrate)
        tool = await _get_tool(server, "get_domain_state")
        await tool.fn(domain="D1", types=["Challenge"], limit=7, descriptions=False)
        mock_substrate.get_domain_state.assert_awaited_once_with(
            domain="D1", types=["Challenge"], limit=7, descriptions=False)

    def test_no_tool_declares_a_parameter_it_never_forwards(self):
        """The general form of the same defect, swept statically."""
        import inspect
        import re as _re
        from kenning_continuity import server as srv

        src = inspect.getsource(srv)
        offenders = []
        pattern = (r"    async def (\w+)\(\n(.*?)\n    \) -> ToolResult:(.*?)"
                   r"(?=\n    @mcp\.tool|\n    # ──|\Z)")
        for match in _re.finditer(pattern, src, _re.S):
            name, params, body = match.groups()
            declared = set(_re.findall(r"^\s{8}(\w+):", params, _re.M))
            if not declared:
                continue
            call = _re.search(r"await substrate\.\w+\((.*?)\)\n", body, _re.S)
            if not call:
                continue
            # a parameter may be transformed before being passed, so any
            # mention anywhere in the body counts as forwarded
            unused = {d for d in declared if not _re.search(rf"\b{d}\b", body)}
            if unused:
                offenders.append((name, sorted(unused)))
        assert offenders == [], f"declared but never used: {offenders}"
