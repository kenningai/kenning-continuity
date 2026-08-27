import logging
import re
import uuid
from enum import Enum
from typing import Any, Dict, List

from neo4j import AsyncDriver, RoutingControl
from pydantic import BaseModel, ConfigDict, Field

from .utils import load_cypher, literal_cypher


logger = logging.getLogger('kenning_continuity')
logger.setLevel(logging.INFO)


def _neo4j_datetime_to_str(val) -> str | None:
    if val is None:
        return None
    return val.iso_format()


# ── Enums ────────────────────────────────────────────────────

class KnowledgeType(str, Enum):
    """Allowed knowledge entity type labels.

    10 ontological types (the primary type of any node) + 5 sub-labels
    (operational sub-classifications that coexist with an ontological
    type via Neo4j multi-labeling).

    Process types (Domain, Session) are created exclusively by process
    tools and are NOT in this enum.
    """
    # Ontological types — referents
    ACTOR = "Actor"
    STRUCTURE = "Structure"
    ARTIFACT = "Artifact"
    PLACE = "Place"
    # Ontological types — events
    EVENT = "Event"
    # Ontological types — emergences
    INSIGHT = "Insight"
    PATTERN = "Pattern"
    CHALLENGE = "Challenge"
    SOLUTION = "Solution"
    LESSON = "Lesson"
    RATIONALE = "Rationale"
    # Sub-labels — operational sub-classifications
    PERSON_REFERENT = "PersonReferent"
    ORGANIZATION = "Organization"
    TEAM = "Team"
    SYSTEM = "System"
    SERVICE = "Service"
    PROCESS = "Process"
    CONFIGURATION = "Configuration"


class SessionStatus(str, Enum):
    """The two states a session is ever in. Written only by the tools —
    `begin_session` sets ACTIVE, `end_session` sets COMPLETE — so the vocabulary
    is closed by construction rather than by validation. Named here because a
    value set that exists only as two string literals in two files is a
    convention, and conventions drift.
    """
    ACTIVE = "active"
    COMPLETE = "complete"


class Basis(str, Enum):
    """How a discovery reached the graph — a fact about the occasion.

    Not a mental state and not anyone's certainty. Alice describing an Example Corp
    outage from memory and Alice ingesting a CSDM whitepaper produce
    differently-grounded knowledge, and a reader needs to know which. Whoever
    reads it derives their own confidence from that, and from the description.

    The same shape as `method` on DIRECTED: it separates what was measured from
    what was inferred, without asserting anything about a state of mind.
    """
    STATED = "stated"                          # a person said it, in the session
    INFERRED = "inferred"                      # the agent concluded it
    READ_FROM_DOCUMENT = "read-from-document"  # ingested from a source


class Resolution(str, Enum):
    """Standing of a co-reference claim.

    `distinct` is the one that matters. The meaning-maker is stochastic and will
    sometimes be wrong — someone will refer to Alice as "Ali Exmpl" — and the
    correction is the agent asking the person directing the session. Without a
    slot for the answer NO, the denial evaporates and the agent re-asks it every
    session thereafter.
    """
    PROPOSED = "proposed"      # the agent's read, unconfirmed
    CONFIRMED = "confirmed"    # put to the director, affirmed
    DISTINCT = "distinct"      # put to the director, denied


class ConnectionType(str, Enum):
    """Allowed knowledge connection types.

    13 knowledge edge types. Process edges (HAS_SESSION, NEXT_SESSION,
    DISCOVERED, CONFIRMED) are created exclusively by process tools and
    are NOT in this enum.
    """
    EVOLVED_FROM = "EVOLVED_FROM"
    ENABLING = "ENABLING"
    REQUIRING = "REQUIRING"
    INFORMING = "INFORMING"
    CAUSING = "CAUSING"
    COMPOSING = "COMPOSING"
    EXTENDING = "EXTENDING"
    RECOGNITION = "RECOGNITION"
    VALIDATION = "VALIDATION"
    INVALIDATING = "INVALIDATING"
    ASSOCIATED_WITH = "ASSOCIATED_WITH"
    SITUATED_AT = "SITUATED_AT"
    REFERENT_CLAIM = "REFERENT_CLAIM"


# ── the director guard ─────────────────────────────────────────────────────
# A Session is an act, and an act has a director. There is no default and no
# fallback identity: a shared "refused" node would collect every refusal from
# every human into one Person and assert they are one intentional vector —
# identity-by-string, the exact error this layer exists to remove, and the
# cheapest path through the gate besides. No person, no vector, no act.
#
# The tool sees only what the agent passes; it has no view of the system prompt
# and cannot check the two against each other. So what is enforced here is
# format, and the Person node is the audit trail — a fabricated director leaves
# a node corresponding to nobody, which is detectable and traceable.

# Placeholders that discharge the requirement without naming anyone.
NON_IDENTITIES = {
    "agent", "user", "unknown", "anonymous", "none", "null", "n/a", "na",
    "test", "refused", "person refused", "nobody", "noreply", "no-reply",
    "system", "assistant", "claude", "ai", "bot",
}


def _validate_director(name: str, source_key: str) -> tuple[str, str]:
    """Return the (name, source_key) to write, or raise.

    source_key is lowercased: a key that forks on capitalisation is not a key,
    and Alice.Example@example.com and alice.example@example.com are one human.
    """
    name = (name or "").strip()
    key = (source_key or "").strip().lower()

    if not name:
        raise ValueError(
            "begin_session requires the director's full name. A session is an "
            "act and an act has a director; there is no default."
        )
    if name.lower() in NON_IDENTITIES:
        raise ValueError(
            f"'{name}' is a placeholder, not a person. begin_session requires "
            f"the name of the human directing this session."
        )
    if "@" in name:
        raise ValueError(
            f"director looks like an address, not a name: {name!r}. Pass the "
            f"full name as director and the email as director_key."
        )
    if len(name) < 2:
        raise ValueError(f"director name is too short to identify anyone: {name!r}")

    if not key:
        raise ValueError(
            "begin_session requires the director's email as director_key. It "
            "is the constitutive key of the person layer and cannot be derived "
            "from the name — three spellings of one name is what that produces."
        )
    local, _, domain = key.partition("@")
    if not local or not domain or "." not in domain or any(c.isspace() for c in key):
        raise ValueError(
            f"director_key is not an email address: {source_key!r}. It must be "
            f"the director's corporate address."
        )
    if local in NON_IDENTITIES:
        raise ValueError(
            f"'{source_key}' is a placeholder address, not a person's. "
            f"begin_session requires the address of the human directing this session."
        )
    return name, key


# Process-layer label strings for guard checks.
# `Person` joined this set at v0.8.0. It is the only name that collides across
# the two layers, because a person is the only thing that is both an intentional
# vector and something people talk about. The vector is here; the referent is
# `PersonReferent` in the knowledge layer.
PROCESS_TYPES = {"Domain", "Session", "Person"}

# Described here rather than as a literal in server.py, so a process type added
# to PROCESS_TYPES cannot go unreported by list_knowledge_types. `Person` was
# added at v0.8.0 and the tool kept reporting two.
PROCESS_TYPE_DESCRIPTIONS = {
    "Domain": "Permanent knowledge domain anchor.",
    "Session": "One session of knowledge work, directed by exactly one Person.",
    "Person": "The human directing a session — the intentional vector, and the "
              "only thing in the graph that has one. Exists iff it directed a "
              "session; created by begin_session and by nothing else. A "
              "reference TO a person in the knowledge layer is a PersonReferent, "
              "which is a different kind of thing.",
}

# Process-layer edge type strings for guard checks
PROCESS_EDGES = {"HAS_SESSION", "NEXT_SESSION", "DISCOVERED", "CONFIRMED",
                 "DIRECTED"}

# The 11 ontological types — exactly one of these is the ont_type for any
# knowledge node. Sub-labels coexist as additional labels but are not
# ontological claims at this level.
# A shared marker on every knowledge node.
#
# It replaces ten separately-maintained exclusion lists. Before this, every
# label-less MATCH carried `AND NOT x:Domain AND NOT x:Session` — and when
# `Person` joined the process layer, only the two sites touched at the time
# gained `AND NOT x:Person`. The other eight were saved by an accident (process
# nodes carry no `domain`, so a (name, domain) match misses them), which is
# exactly a guard passing for a reason other than the guard.
#
# An exclusion list has to be extended every time the process layer grows. A
# positive assertion does not.
KNOWLEDGE_MARKER = "Knowledge"

ONTOLOGICAL_TYPES = {
    "Actor", "Structure", "Artifact", "Place", "Event",
    "Insight", "Pattern", "Challenge", "Solution", "Lesson", "Rationale",
}

# Sub-labels, as PARENT pointers rather than as a flat map to the root.
#
# The model declares `team sub organization sub actor` — three deep. A flat
# sub-label -> ontological-type map cannot hold that: `{"Team": "Actor"}` emits
# :Actor:Team and silently deletes Organization from the chain, so a query for
# organizations stops finding teams and nothing anywhere says so. The parent
# pointer keeps the middle level, and `_resolve_labels` emits the whole chain.
#
# When a user specifies type="Team", the substrate walks Team -> Organization
# -> Actor and applies ALL of them (n:Actor:Organization:Team).
SUB_LABEL_PARENT = {
    "PersonReferent": "Actor",
    "Organization": "Actor",
    "Team": "Organization",
    "System": "Structure",
    "Process": "Structure",
    "Configuration": "Structure",
    "Service": "Structure",
}


def _sub_label_chain(label: str) -> list[str]:
    """Walk a sub-label to its ontological root, nearest ancestor first.

    'Team' -> ['Organization'], because Team's own label is added by the
    caller. Raises on a cycle rather than looping.
    """
    chain: list[str] = []
    seen = {label}
    parent = SUB_LABEL_PARENT[label]
    while parent not in ONTOLOGICAL_TYPES:
        if parent in seen:
            raise ValueError(f"cycle in the sub-label lattice at {parent!r}")
        seen.add(parent)
        chain.append(parent)
        parent = SUB_LABEL_PARENT[parent]
    return chain


def _ont_type_of(label: str) -> str:
    """The ontological root a sub-label resolves to, by walking the chain."""
    parent = SUB_LABEL_PARENT[label]
    seen = {label}
    while parent not in ONTOLOGICAL_TYPES:
        if parent in seen:
            raise ValueError(f"cycle in the sub-label lattice at {parent!r}")
        seen.add(parent)
        parent = SUB_LABEL_PARENT[parent]
    return parent


# Derived, never authored: the root each sub-label resolves to. Kept because
# callers ask "what ontological type is this?" far more than they ask for the
# chain, but it is computed from the lattice so the two cannot disagree.
SUB_LABEL_TO_ONT_TYPE = {sl: _ont_type_of(sl) for sl in SUB_LABEL_PARENT}


KNOWLEDGE_TYPE_DESCRIPTIONS: Dict[KnowledgeType, str] = {
    # Ontological types
    KnowledgeType.ACTOR: "That which acts. Has agency. Sub-labels: PersonReferent, Organization.",
    KnowledgeType.STRUCTURE: "Arrangement without agency. Persistent shape. Sub-labels: System, Process, Configuration.",
    KnowledgeType.ARTIFACT: "Produced thing that persists and can be referenced — document, code, dataset, report.",
    KnowledgeType.PLACE: "A location: a site, campus, region, or extent. Always a referent — Portland holds no possible futures, so a place can never be the thing pointing. Named Place and not Location because Location names the relation and Place names the thing. Containment is COMPOSING; a thing being at a place is SITUATED_AT.",
    KnowledgeType.EVENT: "Temporally-located happening. Done when it's done. Causation expressed via outgoing CAUSING edges, not via the type itself.",
    KnowledgeType.INSIGHT: "A realization, discovery, breakthrough.",
    KnowledgeType.PATTERN: "Recurring structure recognized across instances.",
    KnowledgeType.CHALLENGE: "Present-tense obstacle. Active resistance.",
    KnowledgeType.SOLUTION: "Resolution that worked. Pairs with Challenge.",
    KnowledgeType.LESSON: "Rule derived from experience.",
    KnowledgeType.RATIONALE: "Reason behind something. The why came to be understood.",
    # Sub-labels (auto-applied alongside their ontological type)
    KnowledgeType.PERSON_REFERENT: "A reference to an individual, held in possibility space by whoever referred to them. Sub-label of Actor. NOT the person — a referent cannot refer. The person themself is a process-layer Person, reachable only by having directed a session.",
    KnowledgeType.ORGANIZATION: "A collective entity operating through shared structure. Sub-label of Actor.",
    KnowledgeType.TEAM: "A collective inside an organization. Sub-label of Organization, which is itself a sub-label of Actor — a Team node carries :Actor:Organization:Team, so a query for organizations finds teams.",
    KnowledgeType.SYSTEM: "A persistent measurable structure. Sub-label of Structure.",
    KnowledgeType.PROCESS: "An ongoing procedure or workflow. Sub-label of Structure.",
    KnowledgeType.CONFIGURATION: "A boundary condition or setting. Sub-label of Structure.",
    KnowledgeType.SERVICE: "An offered capability with consumers. Sub-label of Structure.",
}


CONNECTION_TYPE_DESCRIPTIONS: Dict[ConnectionType, str] = {
    ConnectionType.EVOLVED_FROM: "Successor → predecessor in phenomenological evolution chain. Tool-managed.",
    ConnectionType.ENABLING: "Source makes target possible. Generative prerequisite.",
    ConnectionType.REQUIRING: "Source depends on target for coherence.",
    ConnectionType.INFORMING: "Source shaped target without directly causing it.",
    ConnectionType.CAUSING: "Source directly produced target.",
    ConnectionType.COMPOSING: "Source is a building block of target.",
    ConnectionType.EXTENDING: "Same trajectory, deeper application.",
    ConnectionType.RECOGNITION: "Same pattern in different clothes — instance recognized as a known pattern.",
    ConnectionType.VALIDATION: "Theory meeting evidence — confirmation.",
    ConnectionType.INVALIDATING: "Failed validation demanding theoretical response.",
    ConnectionType.REFERENT_CLAIM: "Two referents are claimed to refer to the same thing. A CLAIM, not an identity — authored by a stochastic meaning-maker and adjudicated by the person directing the session. Carries `resolution`; write it with claim_referent, not create_connections.",
    ConnectionType.SITUATED_AT: "Source is located at target. Target is a Place. Distinct from COMPOSING, which is containment between two extents.",
    ConnectionType.ASSOCIATED_WITH: "Weak coupling, catch-all. Use only when no specific edge applies; document why in a paired note.",
}


# ── Pydantic Models ──────────────────────────────────────────

class KnowledgeEntity(BaseModel):
    """A knowledge entity in the temporal knowledge substrate.

    The `type` field accepts either an ontological type ('Actor',
    'Structure', etc.) or a known sub-label ('PersonReferent',
    'Organization', 'Team', 'System', 'Process', 'Configuration',
    'Service'). When a sub-label
    is given, the substrate derives the ontological type via
    SUB_LABEL_TO_ONT_TYPE and applies BOTH labels (e.g.,
    :Actor:PersonReferent).

    The `sub_labels` field provides a way to specify additional sub-labels
    explicitly — useful when the operator wants multiple sub-labels on
    the same ontological type (e.g., type='Actor', sub_labels=['PersonReferent',
    'OperationalAgent']). When `type` is itself a known sub-label and
    `sub_labels` is None, the substrate auto-applies the type as a
    sub-label.

    An unknown field is REFUSED, not ignored. Pydantic's default is to drop
    it silently, which would let a caller keep sending a field this release
    removed — `confidence` — and never learn it stopped being recorded. A
    caller that is wrong should find out.
    """
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Unique identifier within domain.", min_length=1)
    type: KnowledgeType = Field(description="Either an ontological type (one of 11) or a known sub-label (one of 7). The substrate handles both.")
    domain: str = Field(default="", description="Which domain to file this in. A QUERY parameter, never stored on the node — the entity is placed by the session that discovers it. Inherits from the session when empty.")
    description: str = Field(default="", description="What this entity IS at this phenomenological moment.")
    sub_labels: list[str] | None = Field(default=None, description="Optional additional sub-labels. Coexist with the ontological type via Neo4j multi-labeling.")
    basis: Basis | None = Field(default=None, description="How this reached the graph on THIS occasion: 'stated' (a person said it), 'inferred' (you concluded it), or 'read-from-document' (ingested from a source). Rides the DISCOVERED edge, not the entity.")
    sources: list[str] | None = Field(default=None, description="Optional provenance list. Human-set ONLY.")
    t_created: str | None = Field(default=None, description="ISO datetime when created. Auto-set.")
    t_valid: str | None = Field(default=None, description="ISO datetime of external validity. Optional.")
    score: float | None = Field(default=None, description="Relevance score from fulltext search.")
    ont_type: str | None = Field(default=None, description="The ontological type label on the node. Populated by reads.")
    labels: list[str] | None = Field(default=None, description="All labels on the node. Populated by reads.")


class Connection(BaseModel):
    """A connection (relationship) between two knowledge entities."""
    source: str = Field(description="Name of the source entity.", min_length=1)
    target: str = Field(description="Name of the target entity.", min_length=1)
    type: ConnectionType = Field(description="One of the 11 allowed knowledge ConnectionType values.")
    source_domain: str = Field(default="", description="Domain of the source entity. Required to avoid cross-domain ambiguity.")
    target_domain: str = Field(default="", description="Domain of the target entity. Required to avoid cross-domain ambiguity.")
    t_created: str | None = Field(default=None, description="ISO datetime when created. Auto-set.")


class ChainAuthor(BaseModel):
    """A session that discovered a chain node, and the person who directed it."""
    session_id: str
    director: str | None = None
    timestamp: str | None = None
    purpose: str | None = None


class ChainStep(BaseModel):
    """One version in an EVOLVED_FROM chain, as get_chain_history returns it.

    `revision_why` is the reason this version replaced the one before it, read
    from the EVOLVED_FROM edge. It is None for the first version, which
    replaced nothing, and for revisions made before v0.2.0, which recorded no
    reason — and none is supplied for them.

    `discovered_by` is a list, not one author, and is returned as found. A
    version is written by exactly one session, but merge_knowledge (removed in
    v0.2.0) re-pointed predecessors' DISCOVERED edges onto heads, so a node it
    touched can carry several. Picking one would hide that it happened.
    """
    name: str
    type: str
    labels: list[str] | None = None
    domain: str
    description: str
    t_created: str | None = None
    revision_why: str | None = None
    discovered_by: list[ChainAuthor] = Field(default_factory=list)


class DomainKnowledge(BaseModel):
    """Knowledge graph slice containing entities and their connections."""
    entities: List[KnowledgeEntity] = Field(default=[], description="List of knowledge entities")
    connections: List[Connection] = Field(default=[], description="List of connections between entities")


# ── Helpers ──────────────────────────────────────────────────

def _resolve_labels(type_value: str, extra_sub_labels: list[str] | None) -> tuple[str, list[str]]:
    """Resolve user-supplied type into (ont_type, full_sub_labels_list).

    type_value may be an ontological type or a known sub-label. The
    full sub-labels list includes the type-as-sub-label (when type is a
    sub-label) plus any extra_sub_labels supplied.
    """
    if type_value in ONTOLOGICAL_TYPES:
        ont_type = type_value
        sub_labels: list[str] = []
    elif type_value in SUB_LABEL_PARENT:
        ont_type = SUB_LABEL_TO_ONT_TYPE[type_value]
        # the whole constitutive chain, root-ward first, then the type itself:
        # Team -> [Organization, Team], so the node carries :Actor:Organization:Team
        sub_labels = list(reversed(_sub_label_chain(type_value))) + [type_value]
    else:
        raise ValueError(
            f"Unknown type '{type_value}'. Must be one of the 10 ontological types "
            f"({sorted(ONTOLOGICAL_TYPES)}) or a known sub-label "
            f"({sorted(SUB_LABEL_TO_ONT_TYPE.keys())})."
        )
    if extra_sub_labels:
        for sl in extra_sub_labels:
            _validate_sub_label(sl)
            if sl not in sub_labels:
                sub_labels.append(sl)
    return ont_type, sub_labels


def _validate_sub_label(label: str) -> None:
    """Refuse anything that must not reach a knowledge node's label list.

    Two holes, one function. `_labels_string` interpolates sub-labels into a
    Cypher label list with backtick quoting and no escaping, so the pattern
    closes an injection. And `sub_labels` was a second route to a label the
    `type` field refuses — after v0.8.0 that includes `Person`, which now names
    the intentional vector and never a referent.
    """
    if not SUB_LABEL_PATTERN.match(label or ""):
        raise ValueError(
            f"Invalid sub-label {label!r}. Must match {SUB_LABEL_PATTERN.pattern}."
        )
    if label in PROCESS_TYPES:
        raise ValueError(
            f"'{label}' is a process-layer label and cannot be a sub-label on a "
            f"knowledge node. The knowledge layer never touches the process "
            f"layer, in either direction. For a reference to a person, use "
            f"type='PersonReferent'."
        )
    if label in ONTOLOGICAL_TYPES:
        raise ValueError(
            f"'{label}' is an ontological type, not a sub-label. A knowledge "
            f"node carries exactly one ontological label; pass it as `type`."
        )
    if label == KNOWLEDGE_MARKER:
        raise ValueError(
            f"'{label}' is the knowledge-layer marker and is applied by the "
            f"substrate, not by callers."
        )


SUB_LABEL_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _labels_string(ont_type: str, sub_labels: list[str]) -> str:
    """Build a Cypher label string like 'Actor:PersonReferent'.

    Sub-labels reaching here are already validated by `_validate_sub_label`."""
    parts = [KNOWLEDGE_MARKER, ont_type] + sub_labels
    return ":".join(f"`{p}`" for p in parts)


def _split_labels(labels: list[str]) -> tuple[str | None, list[str]]:
    """Given a list of labels on a node, return (ontological_type, sub_labels)."""
    ont = None
    subs: list[str] = []
    for label in labels:
        if label in ONTOLOGICAL_TYPES:
            ont = label
        elif label in PROCESS_TYPES or label == KNOWLEDGE_MARKER:
            continue
        else:
            subs.append(label)
    return ont, subs


# ── TemporalKnowledgeSubstrate ───────────────────────────────


COLLISION_DESCRIPTION_CAP = 4000


async def _collision_message(tx, entity: "KnowledgeEntity") -> str:
    """Say what already occupies the name, not merely that something does.

    The refusal is deliberate and stays — a silent upsert would hide the
    ontological decision the design exists to surface. What was wrong was the
    blindness: the agent was told three options and given nothing to choose
    between them with.

    Reports whether the incoming description is byte-identical to the existing
    one. That is a measurement, not a judgement — it settles confirm-vs-evolve
    without the tool making the call.
    """
    existing = None
    try:
        result = await tx.run(
            load_cypher("knowledge_collision"),
            {"name": entity.name, "domain": entity.domain},
        )
        rows = await result.data()
        existing = rows[0] if rows else None
    except Exception as e:  # pragma: no cover - diagnostic must never mask the refusal
        logger.warning(f"collision lookup failed for '{entity.name}': {e}")

    head = (
        f"Refusing to create '{entity.name}' in domain '{entity.domain}': "
        f"a head-of-chain entity with that name and domain already exists. "
        f"Nothing in this batch was created — create_knowledge is atomic."
    )

    if existing is None:
        return (
            f"{head} The existing entity could not be read back; use "
            f"search_knowledge or get_chain_history to see it. Options: "
            f"(1) evolve_knowledge, (2) confirm_knowledge, (3) a different name."
        )

    description = existing.get("description") or ""
    identical = description == (entity.description or "")
    if len(description) > COLLISION_DESCRIPTION_CAP:
        description = (
            description[:COLLISION_DESCRIPTION_CAP]
            + f"\n... (truncated at {COLLISION_DESCRIPTION_CAP} chars; "
            f"get_chain_history for the full text)"
        )

    verdict = (
        "Your description is IDENTICAL to it, so this is an unchanged "
        "encounter: confirm_knowledge."
        if identical
        else "Your description DIFFERS from it."
    )

    return (
        f"{head}\n\n"
        f"WHAT IS THERE\n"
        f"  type       : {existing.get('ont_type')}\n"
        f"  labels     : {existing.get('labels')}\n"
        f"  created    : {existing.get('t_created')}\n"
        f"  description: {description}\n\n"
        f"{verdict}\n\n"
        f"Options: (1) evolve_knowledge to record new content, "
        f"(2) confirm_knowledge to record unchanged-encounter, "
        f"(3) a different name if this is a genuinely distinct thing."
    )


class TemporalKnowledgeSubstrate:
    def __init__(self, driver: AsyncDriver):
        self.driver = driver

    async def create_fulltext_index(self):
        """Create fulltext index spanning the 10 ontological labels."""
        try:
            for old_index in ["knowledge_index"]:
                try:
                    await self.driver.execute_query(
                        # old_index iterates a hard-coded list, never input
                        literal_cypher(f"DROP INDEX {old_index} IF EXISTS"),
                        routing_control=RoutingControl.WRITE
                    )
                except Exception:
                    pass

            await self.driver.execute_query(
                load_cypher("index_create"),
                routing_control=RoutingControl.WRITE,
            )
            logger.info("Created fulltext index knowledge_index")
        except Exception as e:
            logger.debug(f"Fulltext index creation: {e}")

    async def ensure_schema(self):
        """Create the process-layer uniqueness constraints.

        Uniqueness only. TypeQL declares `source-key @key` — unique AND
        required — but Neo4j existence constraints are Enterprise-only and the
        test image is community, so the required half is enforced where this
        project enforces every other invariant: in the tool. `begin_session`
        refuses a session with no director.

        Person.source_key is safe to constrain while the knowledge layer still
        carries :Person on 484 nodes: none of them holds a source_key, and
        Neo4j uniqueness ignores nulls. The existence half must wait for the
        cutover relabel.
        """
        for name, stmt in (
            ("session_id_unique",
             "CREATE CONSTRAINT session_id_unique IF NOT EXISTS "
             "FOR (s:Session) REQUIRE s.session_id IS UNIQUE"),
            ("person_source_key_unique",
             "CREATE CONSTRAINT person_source_key_unique IF NOT EXISTS "
             "FOR (p:Person) REQUIRE p.source_key IS UNIQUE"),
            # `domain` is not a property to index on — an entity is reached
            # from its Domain by walk. `name` is what the walk narrows by.
            ("knowledge_name",
             "CREATE INDEX knowledge_name IF NOT EXISTS "
             "FOR (n:Knowledge) ON (n.name)"),
        ):
            try:
                await self.driver.execute_query(
                    # stmt is one of the literal DDL statements in the tuple above
                    literal_cypher(stmt), routing_control=RoutingControl.WRITE)
                logger.info(f"Ensured constraint {name}")
            except Exception as e:
                logger.warning(f"Constraint {name} not created: {e}")

    # ── Process Tools ────────────────────────────────────────

    async def create_domain(self, name: str, description: str) -> Dict[str, Any]:
        """Create or retrieve a Domain node (idempotent via MERGE).

        Process-layer idempotence is structural honesty, not imposition —
        re-MERGE produces the same anchor.
        """
        logger.info(f"create_domain: {name}")
        result = await self.driver.execute_query(
            load_cypher("domain_merge"),
            {"name": name, "description": description},
            routing_control=RoutingControl.WRITE,
        )
        if not result.records:
            raise ValueError(f"Failed to create domain '{name}'")
        r = result.records[0]
        return {
            "name": r["name"],
            "description": r["description"],
            "t_created": _neo4j_datetime_to_str(r["t_created"]),
        }

    async def list_domains(self) -> List[Dict[str, Any]]:
        """List all Domain nodes with session count and last activity."""
        logger.info("list_domains")
        result = await self.driver.execute_query(
            "MATCH (d:Domain) "
            "OPTIONAL MATCH (d)-[:HAS_SESSION]->(s:Session) "
            "WITH d, count(s) AS session_count, max(s.timestamp) AS last_activity "
            "RETURN d.name AS name, d.description AS description, "
            "       d.t_created AS t_created, session_count, last_activity "
            "ORDER BY d.name",
            routing_control=RoutingControl.READ,
        )
        return [
            {
                "name": r["name"],
                "description": r["description"],
                "t_created": _neo4j_datetime_to_str(r["t_created"]),
                "session_count": r["session_count"],
                "last_activity": _neo4j_datetime_to_str(r["last_activity"]),
            }
            for r in result.records
        ]

    async def begin_session(
        self, domain: str, purpose: str, director: str, director_key: str
    ) -> Dict[str, Any]:
        """Begin a new session in a domain.

        Creates Person + DIRECTED + Session + HAS_SESSION + NEXT_SESSION.

        purpose is REQUIRED — silently-empty purpose violates
        refusal-over-imposition (the session's existence claim becomes vacuous).

        director and director_key are REQUIRED for the same reason one step
        further up: a session is an act, an act has a director, and the
        director is the only thing in the graph with a noesis. Everything else
        is downstream of it. See `_validate_director`.
        """
        director, director_key = _validate_director(director, director_key)
        logger.info(f"begin_session: domain={domain}, director={director_key}")

        if not purpose or not purpose.strip():
            raise ValueError(
                "begin_session requires a non-empty purpose. The session's "
                "existence claim is vacuous without a stated intent."
            )

        domain_check = await self.driver.execute_query(
            "MATCH (d:Domain {name: $domain}) RETURN d.name AS name",
            {"domain": domain},
            routing_control=RoutingControl.READ,
        )
        if not domain_check.records:
            raise ValueError(
                f"Domain '{domain}' not found. Call create_domain first."
            )

        session_id = str(uuid.uuid4())
        result = await self.driver.execute_query(
            load_cypher("session_create"),
            {"session_id": session_id, "domain": domain, "purpose": purpose,
             "director": director, "director_key": director_key,
             "status": SessionStatus.ACTIVE.value},
            routing_control=RoutingControl.WRITE,
        )

        if not result.records:
            raise ValueError(f"Failed to create session for domain '{domain}'")

        # NOT the domain state. That was 150k tokens on the largest domain,
        # returned before the session had a question. What comes back is what
        # is there, so the agent can ask for the part it needs.
        summary = await self.driver.execute_query(
            "MATCH (:Domain {name: $domain})-[:HAS_SESSION]->(:Session)"
            "-[:DISCOVERED]->(e:Knowledge) "
            "WHERE NOT (e)<-[:EVOLVED_FROM]-() "
            "RETURN e.ont_type AS type, count(e) AS n ORDER BY n DESC",
            {"domain": domain}, routing_control=RoutingControl.READ,
        )
        by_type = {r["type"]: r["n"] for r in summary.records}

        return {
            "session_id": session_id,
            "domain": domain,
            "director": result.records[0]["director"],
            "director_key": result.records[0]["director_key"],
            "timestamp": _neo4j_datetime_to_str(result.records[0]["timestamp"]),
            "domain_summary": {
                "head_entities": sum(by_type.values()),
                "by_type": by_type,
                "next": "call get_domain_state(domain, types=[...], limit=N) "
                        "for the part you need",
            },
        }

    async def end_session(self, session_id: str, summary: str) -> Dict[str, Any]:
        """End a session by setting summary and status on the Session node."""
        logger.info(f"end_session: session_id={session_id}")

        result = await self.driver.execute_query(
            "MATCH (s:Session {session_id: $session_id}) "
            f"SET s.summary = $summary, s.status = '{SessionStatus.COMPLETE.value}' "
            "WITH s "
            "OPTIONAL MATCH (s)-[:DISCOVERED]->(e) "
            "WITH s, count(e) AS entities_discovered "
            "OPTIONAL MATCH (s)-[:CONFIRMED]->(c) "
            "WITH s, entities_discovered, count(c) AS entities_confirmed "
            "RETURN s.session_id AS session_id, "
            "       [(d:Domain)-[:HAS_SESSION]->(s) | d.name][0] AS domain, "
            "       s.timestamp AS timestamp, s.purpose AS purpose, "
            "       s.summary AS summary, s.status AS status, "
            "       entities_discovered, entities_confirmed",
            {"session_id": session_id, "summary": summary},
            routing_control=RoutingControl.WRITE,
        )

        if not result.records:
            raise ValueError(f"Session '{session_id}' not found.")

        r = result.records[0]
        return {
            "session_id": r["session_id"],
            "domain": r["domain"],
            "timestamp": _neo4j_datetime_to_str(r["timestamp"]),
            "purpose": r["purpose"],
            "summary": r["summary"],
            "status": r["status"],
            "entities_discovered": r["entities_discovered"],
            "entities_confirmed": r["entities_confirmed"],
        }

    # ── Knowledge Tools ──────────────────────────────────────

    async def create_knowledge(
        self, session_id: str, entities: List[KnowledgeEntity]
    ) -> List[KnowledgeEntity]:
        """Create knowledge entities. Refuses on collision.

        If any entity name+domain already exists as a head-of-chain knowledge
        node, the entire batch is rejected. Operators should use
        evolve_knowledge to record changed content, confirm_knowledge to
        record unchanged-encounter, or pick a different name.

        Atomic: runs all creates in a single transaction; any failure rolls
        back the whole batch.
        """
        logger.info(f"create_knowledge: {len(entities)} entities")

        if any(not e.domain for e in entities):
            domain_result = await self.driver.execute_query(
                "MATCH (d:Domain)-[:HAS_SESSION]->(s:Session {session_id: $session_id}) "
                "RETURN d.name AS domain",
                {"session_id": session_id},
                routing_control=RoutingControl.READ,
            )
            if not domain_result.records:
                raise ValueError(f"Session '{session_id}' not found.")
            run_domain: str = domain_result.records[0]["domain"]
            for entity in entities:
                if not entity.domain:
                    entity.domain = run_domain

        for entity in entities:
            if entity.type.value in PROCESS_TYPES:
                raise ValueError(
                    f"Cannot create '{entity.type.value}' via create_knowledge. "
                    f"Process-layer types must be created via process tools: "
                    f"create_domain (Domain), begin_session (Session)."
                )

        async def _do_creates(tx):
            created: list[KnowledgeEntity] = []
            for entity in entities:
                ont_type, sub_labels = _resolve_labels(entity.type.value, entity.sub_labels)
                labels_str = _labels_string(ont_type, sub_labels)

                basis_clause = (
                    f"SET d.basis = $basis" if entity.basis is not None else ""
                )
                sources_clause = (
                    f"SET e.sources = $sources" if entity.sources is not None else ""
                )
                t_valid_clause = (
                    f"SET e.t_valid = datetime($t_valid)" if entity.t_valid else ""
                )

                query = load_cypher(
                    "knowledge_create",
                    labels=labels_str,
                    basis_clause=basis_clause,
                    sources_clause=sources_clause,
                    t_valid_clause=t_valid_clause,
                )

                params: Dict[str, Any] = {
                    "name": entity.name,
                    "domain": entity.domain,
                    "description": entity.description,
                    "ont_type": ont_type,
                    "session_id": session_id,
                }
                if entity.basis is not None:
                    params["basis"] = entity.basis.value
                if entity.sources is not None:
                    params["sources"] = entity.sources
                if entity.t_valid:
                    params["t_valid"] = entity.t_valid

                result = await tx.run(query, params)
                records = await result.values()

                if not records:
                    raise ValueError(
                        await _collision_message(tx, entity)
                    )

                entity.ont_type = ont_type
                entity.labels = [ont_type] + sub_labels
                created.append(entity)
            return created

        async with self.driver.session() as session:
            return await session.execute_write(_do_creates)

    async def evolve_knowledge(
        self,
        session_id: str,
        name: str,
        description: str,
        revision_why: str,
        domain: str = "",
        sub_labels: list[str] | None = None,
        basis: "Basis | None" = None,
        sources: list[str] | None = None,
        t_valid: str | None = None,
    ) -> Dict[str, Any]:
        """Evolve a knowledge entity — create a new chain node linked via EVOLVED_FROM.

        The new chain node preserves the ontological type of the old (chain
        invariant). Sub-labels can be REPLACED via the sub_labels parameter
        (omit or pass None to inherit old's sub-labels). Optional
        sources/t_valid set on the new node and basis on its DISCOVERED edge;
        these do not carry
        forward automatically across chain nodes.

        `revision_why` is required and lands on the EVOLVED_FROM edge. The
        edge is the only place a reason for a revision can live without being
        written into the description, which says what the entity IS.

        The head lookup, its write lock, the re-check under that lock and the
        write all run in one transaction. Two sessions evolving one head at
        once would otherwise both pass a separate check and fork the chain.
        """
        logger.info(f"evolve_knowledge: {name}")

        if not revision_why or not revision_why.strip():
            raise ValueError(
                f"Cannot evolve '{name}': revision_why is required. Say why the "
                f"description is changing — what was learned, corrected or "
                f"found missing. It is stored on the EVOLVED_FROM edge, so the "
                f"reason travels with the revision and stays out of the "
                f"description, which says what the entity is."
            )

        if not domain:
            domain_result = await self.driver.execute_query(
                "MATCH (d:Domain)-[:HAS_SESSION]->(s:Session {session_id: $session_id}) "
                "RETURN d.name AS domain",
                {"session_id": session_id},
                routing_control=RoutingControl.READ,
            )
            if not domain_result.records:
                raise ValueError(f"Session '{session_id}' not found.")
            domain = domain_result.records[0]["domain"]

        async def _do_evolve(tx):
            head = await tx.run(
                load_cypher("knowledge_evolve_head"),
                {"name": name, "domain": domain},
            )
            heads = [r async for r in head]

            if not heads:
                raise ValueError(
                    f"Knowledge entity '{name}' not found in domain '{domain}', "
                    f"or it has already been evolved (not head of chain)."
                )
            if len(heads) > 1:
                raise ValueError(
                    f"Cannot evolve '{name}' in domain '{domain}': {len(heads)} "
                    f"chains have a head by that name. Refusing to pick one."
                )

            old_labels = list(heads[0]["labels"])
            ont_type = heads[0]["ont_type"]
            if ont_type is None:
                # Defensive: pre-migration nodes may lack ont_type. Derive from labels.
                ont_type, _ = _split_labels(old_labels)
                if ont_type is None:
                    raise ValueError(
                        f"Cannot evolve '{name}' in domain '{domain}': head node "
                        f"lacks an ontological label. Run the v0.7.0 migration first."
                    )

            if sub_labels is None:
                _, inherited_subs = _split_labels(old_labels)
                new_subs = inherited_subs
            else:
                new_subs = list(sub_labels)

            labels_str = _labels_string(ont_type, new_subs)

            basis_clause = (
                f"SET d.basis = $basis" if basis is not None else ""
            )
            sources_clause = (
                f"SET new.sources = $sources" if sources is not None else ""
            )
            t_valid_clause = (
                f"SET new.t_valid = datetime($t_valid)" if t_valid else ""
            )

            params: Dict[str, Any] = {
                "old_eid": heads[0]["eid"],
                "domain": domain,
                "new_description": description,
                "revision_why": revision_why,
                "ont_type": ont_type,
                "session_id": session_id,
            }
            if basis is not None:
                params["basis"] = basis.value if isinstance(basis, Basis) else str(basis)
            if sources is not None:
                params["sources"] = sources
            if t_valid:
                params["t_valid"] = t_valid

            query = load_cypher(
                "knowledge_evolve",
                ont_label=ont_type,
                new_labels=labels_str,
                basis_clause=basis_clause,
                sources_clause=sources_clause,
                t_valid_clause=t_valid_clause,
            )

            result = await tx.run(query, params)
            records = [r async for r in result]
            if records:
                return records[0]

            # Nothing written. Either the session does not exist, or the head
            # was evolved by another session while this one waited for the lock.
            # Raising rolls the transaction back, lock property included.
            session = await tx.run(
                "MATCH (s:Session {session_id: $session_id}) RETURN count(s) AS n",
                {"session_id": session_id},
            )
            if (await session.single())["n"] == 0:
                raise ValueError(f"Session '{session_id}' not found.")
            raise ValueError(
                f"Cannot evolve '{name}' in domain '{domain}': another session "
                f"evolved it first, so it is no longer head of chain. Read the "
                f"current version with get_chain_history and evolve that."
            )

        async with self.driver.session() as session:
            r = await session.execute_write(_do_evolve)

        return {
            "name": r["name"],
            "ont_type": r["type"],
            "labels": list(r["labels"]),
            "domain": r["domain"],
            "description": r["description"],
            "evolved": True,
        }

    async def confirm_knowledge(
        self, session_id: str, names: List[str], domain: str = ""
    ) -> List[Dict[str, Any]]:
        """Confirm knowledge entities — 'I encountered this and nothing changed.'

        Creates CONFIRMED edges from the Session to head-of-chain entities.
        No new nodes, no mutations.
        """
        logger.info(f"confirm_knowledge: {len(names)} entities")

        if not domain:
            domain_result = await self.driver.execute_query(
                "MATCH (d:Domain)-[:HAS_SESSION]->(s:Session {session_id: $session_id}) "
                "RETURN d.name AS domain",
                {"session_id": session_id},
                routing_control=RoutingControl.READ,
            )
            if not domain_result.records:
                raise ValueError(f"Session '{session_id}' not found.")
            domain = domain_result.records[0]["domain"]

        confirmed = []
        for name in names:
            result = await self.driver.execute_query(
                load_cypher("knowledge_confirm"),
                {"name": name, "domain": domain, "session_id": session_id},
                routing_control=RoutingControl.WRITE,
            )
            if not result.records:
                raise ValueError(
                    f"Knowledge entity '{name}' not found in domain '{domain}', "
                    f"or it is not head of chain."
                )
            r = result.records[0]
            confirmed.append({
                "name": r["name"],
                "ont_type": r["type"],
                "labels": list(r["labels"]),
                "domain": r["domain"],
                "description": r["description"],
                "confirmed": True,
            })

        return confirmed

    async def create_connections(
        self, session_id: str, connections: List[Connection]
    ) -> List[Connection]:
        """Create knowledge connections. Hard-rejects process edges.

        Domain-scoped matching when source_domain/target_domain given;
        ambiguity errors otherwise. Head-of-chain only.

        Every edge records the session that authored it. An edge is an
        assertion — that these two things stand in this relation — and an
        assertion nobody made is the one thing this ontology has no room for.
        See `docs/design/ontology/README.md` on why it is a property rather
        than a node.
        """
        logger.info(f"create_connections: {len(connections)} in {session_id}")
        session_check = await self.driver.execute_query(
            "MATCH (s:Session {session_id: $session_id}) RETURN s.session_id AS id",
            {"session_id": session_id}, routing_control=RoutingControl.READ)
        if not session_check.records:
            raise ValueError(
                f"Session '{session_id}' not found. An edge is an assertion and "
                f"needs the session that made it."
            )

        for connection in connections:
            if connection.type.value in PROCESS_EDGES:
                raise ValueError(
                    f"Cannot create '{connection.type.value}' via create_connections. "
                    f"Process edges are created exclusively by process tools."
                )

            source_match = "MATCH (source:Knowledge {name: $source})"
            source_params: Dict[str, Any] = {"source": connection.source}
            if connection.source_domain:
                source_match = ("MATCH (:Domain {name: $source_domain})-[:HAS_SESSION]->"
                                "(:Session)-[:DISCOVERED]->(source:Knowledge {name: $source})")
                source_params["source_domain"] = connection.source_domain

            source_check = await self.driver.execute_query(
                f"{source_match} "
                f"WHERE NOT (source)<-[:EVOLVED_FROM]-() "
                f"RETURN DISTINCT elementId(source) AS eid, labels(source) AS labels, "
                f"[(d:Domain)-[:HAS_SESSION]->(:Session)-[:DISCOVERED]->(source) | d.name][0] AS domain",
                source_params,
                routing_control=RoutingControl.READ,
            )

            if not source_check.records:
                domain_hint = f" in domain '{connection.source_domain}'" if connection.source_domain else ""
                raise ValueError(
                    f"Source entity '{connection.source}'{domain_hint} not found "
                    f"or is not head of chain."
                )
            if len(source_check.records) > 1:
                domains = [r["domain"] for r in source_check.records]
                raise ValueError(
                    f"Source entity '{connection.source}' is ambiguous — found in domains: "
                    f"{domains}. Specify source_domain to disambiguate."
                )

            source_eid = source_check.records[0]["eid"]
            source_labels = set(source_check.records[0]["labels"])

            target_match = "MATCH (target:Knowledge {name: $target})"
            target_params: Dict[str, Any] = {"target": connection.target}
            if connection.target_domain:
                target_match = ("MATCH (:Domain {name: $target_domain})-[:HAS_SESSION]->"
                                "(:Session)-[:DISCOVERED]->(target:Knowledge {name: $target})")
                target_params["target_domain"] = connection.target_domain

            target_check = await self.driver.execute_query(
                f"{target_match} "
                f"WHERE NOT (target)<-[:EVOLVED_FROM]-() "
                f"RETURN DISTINCT elementId(target) AS eid, labels(target) AS labels, "
                f"[(d:Domain)-[:HAS_SESSION]->(:Session)-[:DISCOVERED]->(target) | d.name][0] AS domain",
                target_params,
                routing_control=RoutingControl.READ,
            )

            if not target_check.records:
                domain_hint = f" in domain '{connection.target_domain}'" if connection.target_domain else ""
                raise ValueError(
                    f"Target entity '{connection.target}'{domain_hint} not found "
                    f"or is not head of chain."
                )
            if len(target_check.records) > 1:
                domains = [r["domain"] for r in target_check.records]
                raise ValueError(
                    f"Target entity '{connection.target}' is ambiguous — found in domains: "
                    f"{domains}. Specify target_domain to disambiguate."
                )

            target_eid = target_check.records[0]["eid"]
            target_labels = set(target_check.records[0]["labels"])

            if connection.type == ConnectionType.REFERENT_CLAIM:
                raise ValueError(
                    "REFERENT_CLAIM cannot be created via create_connections. "
                    "It carries a required `resolution` (proposed / confirmed / "
                    "distinct) and there is nowhere here to put it — a claim "
                    "with no standing is the thing the edge exists to prevent. "
                    "Use claim_referent."
                )

            process_ends = [
                (connection.source, sorted(source_labels & PROCESS_TYPES)),
                (connection.target, sorted(target_labels & PROCESS_TYPES)),
            ]
            offending = [(n, ls) for n, ls in process_ends if ls]
            if offending:
                where = ", ".join(f"'{n}' is {'/'.join(ls)}" for n, ls in offending)
                raise ValueError(
                    f"Knowledge edges cannot touch the process layer: {where}. "
                    f"The knowledge layer never reaches the process layer, in "
                    f"either direction — a referent has no frame from which to "
                    f"perform an act of reference. If you mean a reference to a "
                    f"person, both endpoints must be knowledge nodes "
                    f"(type='PersonReferent')."
                )

            await self.driver.execute_query(
                f"MATCH (source) WHERE elementId(source) = $source_eid "
                f"MATCH (target) WHERE elementId(target) = $target_eid "
                f"MERGE (source)-[r:`{connection.type.value}`]->(target) "
                f"ON CREATE SET r.t_created = datetime() "
                f"SET r.session_id = $session_id",
                {"source_eid": source_eid, "target_eid": target_eid,
                 "session_id": session_id},
                routing_control=RoutingControl.WRITE,
            )

        return connections

    # ── Query Tools ──────────────────────────────────────────

    async def search_knowledge(
        self, query: str, domain: str | None = None, limit: int = 10
    ) -> DomainKnowledge:
        """Search knowledge entities via fulltext index. Returns head-of-chain only."""
        logger.info(f"search_knowledge: '{query}' (domain={domain}, limit={limit})")

        result_entities = await self.driver.execute_query(
            load_cypher("search_knowledge"),
            {"query": query, "domain": domain, "limit": limit},
            routing_control=RoutingControl.READ,
        )

        if not result_entities.records:
            return DomainKnowledge()

        entity_pairs = [
            {"name": r["name"], "domain": r["domain"] or ""}
            for r in result_entities.records
        ]
        entities = [
            KnowledgeEntity(
                name=r["name"],
                type=r["type"],
                domain=r["domain"] or "",
                description=r["description"] or "",
                score=r["score"],
                t_created=_neo4j_datetime_to_str(r["t_created"]),
                ont_type=r["type"],
                labels=list(r["labels"]) if r["labels"] else None,
            )
            for r in result_entities.records
        ]

        connections = await self._connections_between(entity_pairs)
        return DomainKnowledge(entities=entities, connections=connections)

    async def get_domain_state(
        self, domain: str, types: list[str] | None = None,
        limit: int | None = None, descriptions: bool = True,
    ) -> DomainKnowledge:
        """Get current (head-of-chain) knowledge entities for a domain.

        Unbounded by default when asked for directly. The bounding exists
        because begin_session used to return this inline — 498 heads and
        600,820 characters on Service Docs, roughly 150k tokens as the
        first act of a session, which is an availability defect rather than
        an ergonomics complaint.
        """
        logger.info(f"get_domain_state: {domain} types={types} limit={limit}")
        if types:
            unknown = [t for t in types
                       if t not in ONTOLOGICAL_TYPES and t not in SUB_LABEL_PARENT]
            if unknown:
                raise ValueError(
                    f"Unknown type(s) {unknown}. Must be ontological types "
                    f"({sorted(ONTOLOGICAL_TYPES)}) or sub-labels "
                    f"({sorted(SUB_LABEL_PARENT)})."
                )
        if limit is not None and limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}.")

        result = await self.driver.execute_query(
            load_cypher(
                "domain_state",
                limit_clause="LIMIT $limit" if limit is not None else "",
                description_expr="e.description" if descriptions else "''",
            ),
            {
                "domain": domain,
                "types": types or None,
                "limit": limit,
                "excluded": sorted(PROCESS_EDGES | {ConnectionType.EVOLVED_FROM.value}),
            },
            routing_control=RoutingControl.READ,
        )

        entities = []
        connections: list[Connection] = []
        seen_connections = set()

        for r in result.records:
            entities.append(KnowledgeEntity(
                name=r["name"],
                type=r["type"],
                domain=domain,
                description=r["description"] or "",
                t_created=_neo4j_datetime_to_str(r["t_created"]),
                ont_type=r["type"],
                labels=list(r["labels"]) if r["labels"] else None,
            ))

            for conn in r["connections"]:
                if conn["target"] is not None:
                    conn_key = (r["name"], conn["target"], conn["type"])
                    if conn_key not in seen_connections:
                        seen_connections.add(conn_key)
                        try:
                            connections.append(Connection(
                                source=r["name"],
                                target=conn["target"],
                                type=conn["type"],
                                source_domain=domain,
                                target_domain=conn.get("target_domain") or "",
                            ))
                        except ValueError:
                            pass

        return DomainKnowledge(entities=entities, connections=connections)

    async def claim_referent(
        self, session_id: str, source: str, source_domain: str,
        target: str, target_domain: str, resolution: str,
    ) -> Dict[str, Any]:
        """Claim that two referents refer to the same thing.

        A claim, never a structural identity. In the knowledge layer everything
        is a referent held in possibility space by whoever referred to it, so
        seventeen "Alice Example" nodes across eleven domains are seventeen
        acts of reference, correctly distinct. Saying two of them are the same
        person is something SOMEONE asserts, in a session, and it can be wrong.
        """
        if resolution not in {r.value for r in Resolution}:
            raise ValueError(
                f"Unknown resolution '{resolution}'. Must be one of "
                f"{sorted(r.value for r in Resolution)}."
            )
        if (source, source_domain) == (target, target_domain):
            raise ValueError(
                f"Refusing to claim '{source}' in '{source_domain}' co-refers "
                f"with itself. A claim needs two referents."
            )

        logger.info(
            f"claim_referent: {source}@{source_domain} -> "
            f"{target}@{target_domain} ({resolution})"
        )
        result = await self.driver.execute_query(
            load_cypher("referent_claim"),
            {"session_id": session_id, "source": source,
             "source_domain": source_domain, "target": target,
             "target_domain": target_domain, "resolution": resolution},
            routing_control=RoutingControl.WRITE,
        )
        if not result.records:
            raise ValueError(
                f"Could not claim co-reference: one of '{source}' in "
                f"'{source_domain}' or '{target}' in '{target_domain}' is not a "
                f"head-of-chain knowledge entity, or session '{session_id}' "
                f"does not exist."
            )
        return dict(result.records[0])

    async def get_session_history(self, domain: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Get the Session chain for a domain."""
        logger.info(f"get_session_history: domain={domain}, limit={limit}")

        result = await self.driver.execute_query(
            load_cypher("session_history"),
            {"domain": domain, "limit": limit},
            routing_control=RoutingControl.READ,
        )

        return [
            {
                "session_id": r["session_id"],
                "director": r["director"],
                "director_key": r["director_key"],
                "timestamp": _neo4j_datetime_to_str(r["timestamp"]),
                "purpose": r["purpose"],
                "summary": r["summary"],
                "status": r["status"],
                "entities_discovered": r["entities_discovered"],
                "entities_confirmed": r["entities_confirmed"],
            }
            for r in result.records
        ]

    async def get_chain_history(self, name: str, domain: str = "") -> List[ChainStep]:
        """Walk EVOLVED_FROM backward from head-of-chain. Returns chain ordered oldest-first.

        Required because evolve-as-default means accumulated phenomenological
        content lives on chain nodes that head-of-chain queries hide.
        """
        logger.info(f"get_chain_history: {name} (domain={domain})")

        if not domain:
            raise ValueError(
                "get_chain_history requires a domain. Same-named entities may "
                "exist in multiple domains."
            )

        ontological_types_list = sorted(ONTOLOGICAL_TYPES)

        result = await self.driver.execute_query(
            load_cypher("chain_history"),
            {"name": name, "domain": domain, "ontological_types": ontological_types_list},
            routing_control=RoutingControl.READ,
        )

        if not result.records:
            raise ValueError(
                f"No knowledge chain found for '{name}' in domain '{domain}'."
            )

        return [
            ChainStep(
                name=r["name"],
                type=r["type"],
                domain=r["domain"] or "",
                description=r["description"] or "",
                t_created=_neo4j_datetime_to_str(r["t_created"]),
                labels=list(r["labels"]) if r["labels"] else None,
                revision_why=r["revision_why"],
                discovered_by=[
                    ChainAuthor(
                        session_id=a["session_id"],
                        director=a["director"],
                        timestamp=_neo4j_datetime_to_str(a["timestamp"]),
                        purpose=a["purpose"],
                    )
                    for a in r["discovered_by"]
                ],
            )
            for r in result.records
        ]

    async def _connections_between(
        self, pairs: List[Dict[str, str]]
    ) -> List[Connection]:
        """Get connections between exactly the entities identified by (name, domain).

        Takes pairs rather than names because a name does not identify an
        entity: 69 names exist in more than one domain, and every head-of-chain
        node shares its name with its own predecessors. Matching on name alone
        fabricated both — see search_connections.cypher.

        Process edges are excluded by the query, not by the Connection model
        raising ValueError on an unrecognised type. Filtering by exception
        happened to work and would have leaked the moment a process edge type
        was added to ConnectionType.
        """
        if not pairs:
            return []

        result = await self.driver.execute_query(
            load_cypher("search_connections"),
            {
                "pairs": pairs,
                "excluded": sorted(PROCESS_EDGES | {ConnectionType.EVOLVED_FROM.value}),
            },
            routing_control=RoutingControl.READ,
        )
        connections: list[Connection] = []
        for r in result.records:
            if r.get("connectionType"):
                try:
                    connections.append(Connection(
                        source=r["source"],
                        target=r["target"],
                        type=r["connectionType"],
                        source_domain=r["source_domain"] or "",
                        target_domain=r["target_domain"] or "",
                    ))
                except ValueError:
                    pass
        return connections
