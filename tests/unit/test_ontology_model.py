"""The ontology model is the source of truth for the type surface.

`docs/design/ontology/*.tql` is authored first and the implementation is gated
against it. This test parses the TypeQL and asserts that substrate.py agrees —
in both directions, so a type added to the code fails as loudly as one dropped
from the model.

WHAT THIS GATE CHECKS
    · the ontological types (direct subtypes of `knowledge`)
    · the sub-labels, AND the ontological root each resolves to by walking the
      declared chain — the check that catches a three-deep chain being
      flattened by a sub-label -> root map
    · the knowledge connection types
    · the process-layer entity and edge names

WHAT THIS GATE DELIBERATELY DOES NOT CHECK — the residue
    Not everything a production graph enforces is expressible in a declared
    schema, and pretending otherwise is how the next recall gets built. Left to
    the tools, on purpose:

    · EVOLVED_FROM acyclicity. Neither TypeQL nor OWL 2 DL can state it — the
      DL profile forbids irreflexivity on transitive properties, i.e. it
      refuses exactly the axiom that would catch a cycle. Cypher enforces it
      trivially and that is where it stays.
    · Head-of-chain semantics. `WHERE NOT (e)<-[:EVOLVED_FROM]-()` is a query
      pattern, not a schema fact. The entire notion of "current state" is
      invisible to the model.
    · Indexes, payload bounds, GDS projections. Not ontology at all.

    The discipline is to name the checkable surface correctly, not maximally.
    A model presented as complete when it is partial trains people to trust the
    part it never covered.
"""
import re
from pathlib import Path

import pytest

from kenning_continuity.substrate import (
    ONTOLOGICAL_TYPES,
    PROCESS_EDGES,
    PROCESS_TYPES,
    SUB_LABEL_TO_ONT_TYPE,
    ConnectionType,
    KnowledgeType,
)

# Resolved by version, never hardcoded. A gate that names one filename stops
# gating the moment the model is versioned — it fails loudly if you are lucky
# and silently if the old file is still lying around. The model is whichever
# `vN.N.N.tql` sorts highest.
_ONTOLOGY_DIR = Path(__file__).parents[2] / "docs" / "design" / "ontology"


def _model_path() -> Path:
    candidates = sorted(
        _ONTOLOGY_DIR.glob("v*.tql"),
        key=lambda p: tuple(int(n) for n in p.stem.lstrip("v").split(".")),
    )
    if not candidates:
        raise AssertionError(f"no versioned .tql model found in {_ONTOLOGY_DIR}")
    return candidates[-1]


MODEL = _model_path()

KNOWLEDGE_ROOT = "knowledge"
LINK_ROOT = "knowledge-link"
CHAIN_EDGE = "evolved-from"

# ── the declared-but-unbuilt surface ─────────────────────────────────────
# The model is authored ahead of the code, so at any moment part of it is
# declared and not yet built. That gap is named here rather than hidden,
# and every assertion below checks it is EXACTLY this — so a type leaving
# these sets fails just as loudly as one dropped from the code.
#
# Nothing is deferred. Inquiry and its five edges were struck from the model
# rather than held back: an intention a person has not acted on is category-1,
# theirs, and not collective knowledge until they choose to actualize it — at
# which point it arrives here as knowledge, discovered in a session. The tql
# header carries the full reasoning.
DEFERRED_ENTITIES: set[str] = set()
DEFERRED_PROCESS_EDGES: set[str] = set()

# NOT YET BUILT. Empty: the code carries the whole declared type surface.
# These stay as named, asserted sets rather than being deleted — the assertions
# below check the gap is EXACTLY this, so a type slipping into one of them
# fails as loudly as one dropped from the code.
UNBUILT_ONTOLOGICAL_TYPES: set[str] = set()
UNBUILT_SUB_LABELS: set[str] = set()
UNBUILT_KNOWLEDGE_EDGES: set[str] = set()


# ── parsing ──────────────────────────────────────────────────────────────

def _strip_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _declarations(text: str, kind: str) -> dict[str, str | None]:
    """Map every declared `kind` (entity|relation) to its supertype, or None.

    A declaration opens with `kind name` and runs to the next `;` at statement
    level, so `owns` / `plays` / `relates` clauses are skipped wholly.
    """
    found: dict[str, str | None] = {}
    for stmt in _strip_comments(text).split(";"):
        m = re.match(rf"\s*{kind}\s+([a-z][a-z0-9-]*)\s*(.*)", stmt, re.S)
        if not m:
            continue
        name, rest = m.group(1), m.group(2)
        sup = re.match(r"\s*sub\s+([a-z][a-z0-9-]*)", rest, re.S)
        found[name] = sup.group(1) if sup else None
    return found


def _root_of(name: str, tree: dict[str, str | None]) -> str:
    seen = {name}
    while tree.get(name):
        name = tree[name]  # type: ignore[assignment]
        assert name not in seen, f"cycle in the type lattice at {name!r}"
        seen.add(name)
    return name


def _to_type(tql: str) -> str:
    """`associated-with` -> `AssociatedWith`-style PascalCase for entity names."""
    return "".join(p.capitalize() for p in tql.split("-"))


def _to_edge(tql: str) -> str:
    return tql.replace("-", "_").upper()


@pytest.fixture(scope="module")
def model() -> str:
    assert MODEL.exists(), f"ontology model missing at {MODEL}"
    return MODEL.read_text()


@pytest.fixture(scope="module")
def entities(model: str) -> dict[str, str | None]:
    return _declarations(model, "entity")


@pytest.fixture(scope="module")
def relations(model: str) -> dict[str, str | None]:
    return _declarations(model, "relation")


# ── the gate ─────────────────────────────────────────────────────────────

class TestOntologicalTypes:
    def test_model_and_code_declare_the_same_ontological_types(self, entities):
        modelled = {
            _to_type(n) for n, sup in entities.items() if sup == KNOWLEDGE_ROOT
        }
        assert modelled - ONTOLOGICAL_TYPES == UNBUILT_ONTOLOGICAL_TYPES
        assert ONTOLOGICAL_TYPES - modelled == set(), "a type the model never declared"
        assert modelled - UNBUILT_ONTOLOGICAL_TYPES == ONTOLOGICAL_TYPES

    def test_every_ontological_type_is_a_knowledge_type(self, entities):
        modelled = {
            _to_type(n) for n, sup in entities.items() if sup == KNOWLEDGE_ROOT
        }
        assert modelled - UNBUILT_ONTOLOGICAL_TYPES <= {t.value for t in KnowledgeType}


class TestSubLabels:
    def test_model_and_code_declare_the_same_sub_labels(self, entities):
        modelled = {
            _to_type(n)
            for n, sup in entities.items()
            if sup is not None
            and sup != KNOWLEDGE_ROOT
            and _root_of(n, entities) == KNOWLEDGE_ROOT
        }
        assert modelled - set(SUB_LABEL_TO_ONT_TYPE) == UNBUILT_SUB_LABELS
        assert set(SUB_LABEL_TO_ONT_TYPE) - modelled == set()
        assert modelled - UNBUILT_SUB_LABELS == set(SUB_LABEL_TO_ONT_TYPE)

    def test_each_sub_label_resolves_to_the_same_ontological_root(self, entities):
        """The check a flat sub-label -> root map cannot survive.

        The model declares `person sub actor`; a three-deep chain would declare
        `team sub organization sub actor`. Walking the chain and comparing the
        ROOT catches a registry that maps Team straight to Actor and silently
        drops Organization on the way.
        """
        for tql, sup in entities.items():
            if sup is None or _root_of(tql, entities) != KNOWLEDGE_ROOT:
                continue
            if sup == KNOWLEDGE_ROOT:
                continue
            label = _to_type(tql)
            if label in UNBUILT_SUB_LABELS:
                continue
            walked = tql
            while entities.get(walked) not in (KNOWLEDGE_ROOT, None):
                walked = entities[walked]  # type: ignore[assignment]
            assert SUB_LABEL_TO_ONT_TYPE[label] == _to_type(walked), (
                f"{label} resolves to {_to_type(walked)} in the model but to "
                f"{SUB_LABEL_TO_ONT_TYPE[label]} in SUB_LABEL_TO_ONT_TYPE"
            )


class TestConnectionTypes:
    def test_model_and_code_declare_the_same_knowledge_edges(self, relations):
        modelled = {
            _to_edge(n) for n, sup in relations.items() if sup == LINK_ROOT
        }
        modelled.add(_to_edge(CHAIN_EDGE))
        assert modelled - {c.value for c in ConnectionType} == UNBUILT_KNOWLEDGE_EDGES
        assert {c.value for c in ConnectionType} - modelled == set()
        assert modelled - UNBUILT_KNOWLEDGE_EDGES == {c.value for c in ConnectionType}


class TestProcessLayer:
    def test_model_and_code_declare_the_same_process_entities(self, entities):
        modelled = {
            _to_type(n)
            for n in entities
            if _root_of(n, entities) != KNOWLEDGE_ROOT
        }
        assert modelled - PROCESS_TYPES == DEFERRED_ENTITIES
        assert PROCESS_TYPES - modelled == set()
        assert modelled - DEFERRED_ENTITIES == PROCESS_TYPES

    def test_model_and_code_declare_the_same_process_edges(self, relations):
        modelled = {
            _to_edge(n)
            for n, sup in relations.items()
            if sup is None and n not in (LINK_ROOT, CHAIN_EDGE)
        }
        assert modelled - PROCESS_EDGES == DEFERRED_PROCESS_EDGES
        assert PROCESS_EDGES - modelled == set(), "a process edge the model never declared"
        assert modelled - DEFERRED_PROCESS_EDGES == PROCESS_EDGES


class TestNoOrphanedAttributes:
    """Every declared attribute must be owned by something.

    An unowned attribute is legal TypeQL — the schema is not inconsistent, it
    just carries a type nothing uses, so it loads green. A declared schema
    refuses bad WRITES; it does not refuse a declaration with no referent.

    Found the hard way: v0.8.0 declared `attribution-method` with its full
    value vocabulary and never bound it to `directed`. The migration's Phase 0
    gate — separate the 15 confirmed attributions from the 50 inferred — reads
    that property, so the gate had nothing to read, and both the release notes
    and the design artifact claimed a property the model did not carry.
    """

    @pytest.mark.parametrize(
        "path", sorted((MODEL.parent).glob("*.tql")), ids=lambda p: p.name
    )
    def test_no_declared_attribute_is_orphaned(self, path: Path):
        text = _strip_comments(path.read_text())
        declared = set(re.findall(r"\battribute\s+([a-z][a-z0-9-]*)", text))
        owned = set(re.findall(r"\bowns\s+([a-z][a-z0-9-]*)", text))
        assert declared, f"{path.name} declares no attributes"
        assert not (declared - owned), (
            f"{path.name} declares attributes nothing owns: "
            f"{sorted(declared - owned)}"
        )

    @pytest.mark.parametrize(
        "path", sorted((MODEL.parent).glob("*.tql")), ids=lambda p: p.name
    )
    def test_no_owns_references_an_undeclared_attribute(self, path: Path):
        text = _strip_comments(path.read_text())
        declared = set(re.findall(r"\battribute\s+([a-z][a-z0-9-]*)", text))
        owned = set(re.findall(r"\bowns\s+([a-z][a-z0-9-]*)", text))
        assert not (owned - declared), (
            f"{path.name} owns attributes it never declares: "
            f"{sorted(owned - declared)}"
        )


class TestGateIsTwoSided:
    """A gate that can only say yes proves nothing.

    Both directions of every assertion above are set equality, so these prove
    the parser actually reacts — a dropped type and an added one each fail.
    """

    def test_a_dropped_type_fails(self, model):
        mutated = model.replace("entity rationale  sub knowledge;", "")
        entities = _declarations(mutated, "entity")
        modelled = {
            _to_type(n) for n, sup in entities.items() if sup == KNOWLEDGE_ROOT
        }
        assert modelled != ONTOLOGICAL_TYPES

    def test_an_added_type_fails(self, model):
        mutated = model + "\nentity intention sub knowledge;\n"
        entities = _declarations(mutated, "entity")
        modelled = {
            _to_type(n) for n, sup in entities.items() if sup == KNOWLEDGE_ROOT
        }
        assert modelled != ONTOLOGICAL_TYPES

    def test_a_flattened_sub_label_chain_fails(self, model):
        """Team declared straight under actor instead of under organization.

        The failure this catches is silent by construction: a flat map emits
        :Actor:Team, dropping Organization, so a query for organizations stops
        finding teams and nothing anywhere reports it. Both the model and the
        code must carry the middle level, and here they are compared.
        """
        from kenning_continuity.substrate import _sub_label_chain

        # the code carries the middle level
        assert _sub_label_chain("Team") == ["Organization"]

        # a model that flattens it disagrees, and the disagreement is visible
        mutated = model.replace("entity team          sub organization;",
                                "entity team          sub actor;")
        assert mutated != model, "the declaration under test moved"
        entities = _declarations(mutated, "entity")
        assert entities["team"] == "actor"

        modelled_chain = []
        walked = "team"
        while entities.get(walked) not in (KNOWLEDGE_ROOT, None):
            walked = entities[walked]
            if _to_type(walked) in ONTOLOGICAL_TYPES:
                break
            modelled_chain.append(_to_type(walked))
        assert modelled_chain == [], "the flattened model has no middle level"
        assert modelled_chain != _sub_label_chain("Team")

    def test_an_orphaned_attribute_fails(self, model):
        """The exact defect this gate was added for."""
        mutated = model + '\nattribute never-owned value string;\n'
        text = _strip_comments(mutated)
        declared = set(re.findall(r"\battribute\s+([a-z][a-z0-9-]*)", text))
        owned = set(re.findall(r"\bowns\s+([a-z][a-z0-9-]*)", text))
        assert declared - owned == {"never-owned"}
