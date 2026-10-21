# Kenning Continuity

**Enterprise knowledge that accumulates instead of degrading.**

An MCP server that lets LLM agents accumulate organizational knowledge across
sessions, scoped by domain, backed by Neo4j. Every structural invariant is
enforced by a tool rather than asked for in an instruction, and no raw Cypher
write is exposed — so the graph cannot corrupt itself no matter what the model
decides to do.

It is in production at an enterprise scale: 57 domains, 619 sessions, and
about 5,400 knowledge nodes accumulated over six months of daily use.

## Why this does not degrade as it fills

Most accumulating memory systems fail in one of two ways. They confabulate from
the first day, because nothing distinguishes a thing that was observed from a
thing that was inferred by a model trying to be helpful. Or they survive the
demo and then degrade under mass, because every write is an unconstrained
append and the store slowly fills with near-duplicates, silent overwrites, and
claims nobody can trace to an author.

Four commitments, all enforced structurally:

**Nothing is overwritten.** There is no `update_knowledge` and no
`retype_knowledge`. Changing what is known about an entity creates a *new* node
linked to its predecessor by `EVOLVED_FROM`. The prior understanding is still
there, still readable, still attributable. Being wrong earlier is a fact about
the record, not something to be erased from it.

The reason for a revision is required, and it is held by the `EVOLVED_FROM`
edge rather than written into the new description, which says only what the
entity is. A chain is a line: a version has at most one successor, so two
sessions revising the same thing at once cannot leave two current versions.
Nothing compacts a chain and nothing deletes one. The person directing a
session is the one whose knowledge the graph holds, and a claim an earlier
version made, which no later version contradicts, was true as of when it was
stated.

**Every claim reaches its author in two hops.** A knowledge entity is
discovered by a `Session`; a `Session` is directed by a `Person`. Both edges
are written by tools that refuse to run without a real director. So "who said
this, and when, and toward what purpose" is always answerable — not by
convention, but because there is no code path that creates knowledge without
it.

**Identity is never inferred from a string.** Seventeen nodes named
"Alice Example" across eleven domains are seventeen *acts of reference*,
correctly distinct. Asserting that two of them are one person is a claim someone makes,
in a session, recorded as a `REFERENT_CLAIM` that can carry the value
`distinct` — because a system with nowhere to record the answer *no* will
re-ask the same question forever. String-matching identity is where most
knowledge graphs quietly begin lying.

**Structure carries facts; properties do not duplicate them.** A knowledge
entity holds no `domain` property. It is placed in a domain by the session that
discovered it and reached from there by a walk. The property that preceded this
had drifted, which is what denormalizations do: 21 entities carried a `domain`
string naming a real domain that had no path to them at all. A fact held as
text beside a structure, rather than by it, is a fact that will disagree with
the structure eventually.

There is no `confidence` field, deliberately. Someone's confidence on an
occasion is a fact about their experience and does not transmit; a later reader
derives their own from the description against what they know. What confidence
scores reach for is carried instead by edges any reader can adjudicate:
`VALIDATION`, `INVALIDATING`, and a `Challenge` with no paired `Solution`.

If you have run an accumulating memory system at scale and hit these walls
yourself, [we want to hear from
you](https://github.com/kenningai/kenning-continuity/blob/main/TRADEMARKS.md#asking-and-the-standing-invitation).

## Architecture

**Process layer** (immutable) — `Person → Session`, within `Domain → Session`.
`NEXT_SESSION` always points forward in time, enforced by the tool. Membership
in this layer is the capacity to hold possible futures and measure them toward
actualization; nothing here is a referent.

**Knowledge layer** (append-only) — 11 ontological types and 7 sub-labels,
connected by 13 connection types, every node marked `:Knowledge`. Description
changes create a new chain node linked by `EVOLVED_FROM`.

See [`HOWTO.xml`](https://github.com/kenningai/kenning-continuity/blob/main/HOWTO.xml) for the operator-facing teaching to give an
LLM as invariant scaffolding, and [`docs/design/`](https://github.com/kenningai/kenning-continuity/blob/main/docs/design) for the
reasoning behind each commitment. `HOWTO.xml` is installed alongside the
package, so a `pip install` reaches it at
`kenning_continuity/HOWTO.xml` inside site-packages.

## Prerequisites

- Python 3.10+
- Neo4j 5.x instance (local or remote)
- [uv](https://docs.astral.sh/uv/) (for development; not needed to install)

## Quick start

```bash
pip install kenning-continuity

kenning-continuity --db-url bolt://localhost:7687
```

Or from source:

```bash
uv sync
uv run kenning-continuity --db-url bolt://localhost:7687
```

## Configuration

CLI flags take precedence over environment variables, which take precedence
over defaults.

| CLI flag | Env var | Default | Description |
|---|---|---|---|
| `--db-url` | `NEO4J_URI` or `NEO4J_URL` | `bolt://localhost:7687` | Neo4j connection URL |
| `--username` | `NEO4J_USERNAME` | `neo4j` | Neo4j username |
| `--password` | `NEO4J_PASSWORD` | `password` | Neo4j password |
| `--database` | `NEO4J_DATABASE` | `neo4j` | Neo4j database name |
| `--transport` | `NEO4J_TRANSPORT` | `stdio` | `stdio`, `sse`, or `streamable-http` |
| `--namespace` | `NEO4J_NAMESPACE` | *(none)* | Tool name prefix (e.g. `myapp` → `myapp-begin_session`) |
| `--read-timeout` | `NEO4J_READ_TIMEOUT` | `30` | Read query timeout, seconds |
| `--server-host` | `NEO4J_MCP_SERVER_HOST` | `127.0.0.1` | HTTP host (non-stdio transports) |
| `--server-port` | `NEO4J_MCP_SERVER_PORT` | `8000` | HTTP port (non-stdio transports) |
| `--server-path` | `NEO4J_MCP_SERVER_PATH` | `/mcp/` | HTTP path (non-stdio transports) |
| `--allow-origins` | `NEO4J_MCP_SERVER_ALLOW_ORIGINS` | *(none)* | Comma-separated CORS origins |
| `--allowed-hosts` | `NEO4J_MCP_SERVER_ALLOWED_HOSTS` | *(none)* | Comma-separated hosts, DNS-rebinding protection |

## MCP client configuration

### Claude Desktop / Claude Code

```json
{
  "mcpServers": {
    "temporal-knowledge": {
      "command": "kenning-continuity",
      "args": ["--db-url", "bolt://localhost:7687"]
    }
  }
}
```

### HTTP transport

```bash
kenning-continuity \
  --db-url bolt://localhost:7687 \
  --transport streamable-http \
  --server-host 0.0.0.0 \
  --server-port 8000 \
  --allow-origins "http://localhost:3000" \
  --allowed-hosts "localhost,127.0.0.1"
```

## Tool surface (24 tools)

A session runs: `create_domain` (once) → `begin_session` →
create / evolve / confirm / connect knowledge → `end_session`.

The server is stateless. `begin_session` returns a `session_id` that the client
passes to every subsequent knowledge tool. A session never closed stays
`active` — by design.

### Process (4)

| Tool | Description |
|---|---|
| `list_domains` | All domains with session counts and last activity |
| `create_domain` | Create a knowledge domain (idempotent) |
| `begin_session` | Start a session; returns `session_id`. `domain`, `purpose`, `director` and `director_key` are all required — the director's `Person` node is created here and nowhere else |
| `end_session` | Close a session with a summary of what was learned |

`begin_session` returns a **count by type**, not the domain state. Returning
the state inline was 600,820 characters on one production domain — roughly
150k tokens spent before the session had asked a question. Call
`get_domain_state` with `types` and `limit` for the part you actually need.

### Knowledge mutation (5)

| Tool | Description |
|---|---|
| `create_knowledge` | Create entities. Refuses on collision, refuses process types, and refuses unknown fields rather than dropping them |
| `evolve_knowledge` | The only way to change a description. Creates a new chain node preserving the prior one via `EVOLVED_FROM`, and requires a `revision_why` that it stores on that edge; keeps the ontological type, replaces the sub-label set |
| `confirm_knowledge` | Record that entities were reviewed and found unchanged |
| `create_connections` | Link entities. Takes a `session_id` and records it on every edge — an edge is an assertion and reaches its author |
| `claim_referent` | Claim two referents are the same thing, with a required `resolution`: `proposed`, `confirmed`, or `distinct` |

### Query (5)

| Tool | Description |
|---|---|
| `search_knowledge` | Fulltext across names and descriptions, head-of-chain only, with each entity's `basis` and `sources` |
| `get_domain_state` | Current entities for a domain, with each one's `basis` and `sources`; optional `types`, `limit`, `descriptions` |
| `get_session_history` | Who worked on what, when, and toward what purpose |
| `get_chain_history` | Walk `EVOLVED_FROM` backward — what was previously understood, why each version replaced the last, who wrote it, and what each version cited |

Provenance is per version and never inherited: a revision's `sources` name what
that revision drew on, and `null` means nothing was recorded, not that the
knowledge is unsourced.
| `read_cypher` | Read-only Cypher escape hatch; writes are rejected |

### Taxonomy (2)

| Tool | Description |
|---|---|
| `list_knowledge_types` | 11 ontological types and 7 sub-labels, with the lattice |
| `list_connection_types` | 13 knowledge and 5 process edge types |

### Graph analytics (8)

| Tool | Description |
|---|---|
| `gds_create_projection` | Create a GDS graph projection, optionally scoped to one domain by walk; refuses rather than returning an empty one |
| `gds_drop_projection` | Drop a projection |
| `gds_pagerank` | PageRank centrality |
| `gds_articlerank` | ArticleRank — PageRank with hub transmission damped; the gap between the two shows centrality borrowed from hubs |
| `gds_betweenness` | Betweenness centrality — bridge nodes |
| `gds_louvain` | Louvain community detection |
| `gds_leiden` | Leiden community detection — every community it returns is internally connected |
| `gds_wcc` | Weakly connected components |

## Type system

Every knowledge node carries `:Knowledge`, exactly one ontological label, an
`ont_type` property naming that label deterministically, and zero or more
sub-labels via Neo4j multi-labeling.

| Ontological type | What it is |
|---|---|
| `Actor` | That which acts; has agency |
| `Structure` | Arrangement without agency; persistent shape |
| `Artifact` | Produced thing that persists and can be referenced |
| `Place` | A site, campus, region or extent — always a referent, never a referrer |
| `Event` | Temporally-located happening |
| `Insight` | A realization, discovery, breakthrough |
| `Pattern` | Recurring structure recognized across instances |
| `Challenge` | Present-tense obstacle |
| `Solution` | Resolution that worked; pairs with `Challenge` |
| `Lesson` | Rule derived from experience |
| `Rationale` | The reason behind something |

### Sub-labels are a lattice, not a flat map

```
PersonReferent → Actor          System        → Structure
Organization   → Actor          Process       → Structure
Team           → Organization → Actor         Configuration → Structure
                                              Service       → Structure
```

`create_knowledge` accepts either an ontological type (`Actor`) or a sub-label
(`Team`), and applies the **whole chain**: `:Knowledge:Actor:Organization:Team`.
A query for organizations therefore finds teams. A flat map would emit
`:Actor:Team`, silently dropping the middle level, and nothing would report it.

### Connection types (13)

`EVOLVED_FROM`, `ENABLING`, `REQUIRING`, `INFORMING`, `CAUSING`, `COMPOSING`,
`EXTENDING`, `RECOGNITION`, `VALIDATION`, `INVALIDATING`, `ASSOCIATED_WITH`,
`SITUATED_AT`, `REFERENT_CLAIM`.

Process edges — `HAS_SESSION`, `NEXT_SESSION`, `DIRECTED`, `DISCOVERED`,
`CONFIRMED` — are written only by process tools and cannot be created through
`create_connections`.

### What a description should say

A description says what the entity **is**, not what happened to or around it.
Temporal observations belong on `Event` nodes reached by edges.

- Correct, for an `Actor:PersonReferent`: *"Founder of Example Corp. Largest
  individual shareholder (8.4%). Filed under Schedule 13D — not passive."*
- Wrong: *"SCHEDULE 13D/A Amendment No. 10 filed Oct 8, 2025. FOUNDER GOES
  PUBLIC…"*

The filing **is** an `Event`. Alice Example **is** the founder. The filing
revealing something about them is an `INFORMING` edge. Their description should
survive any number of future filings unchanged, unless their identity itself
changes.

## Development

```bash
uv sync --group dev

./.venv/bin/pytest tests/unit          # mocked substrate, no database
./.venv/bin/pytest tests/integration   # real Neo4j via testcontainers; needs Docker
./.venv/bin/pyright
```

A defect that lives in a Cypher query is invisible to the unit tests — they
mock the substrate, and an edge-fabrication bug once passed every one of them
identically before and after the fix. Anything touching a query belongs in
`tests/integration/`.

## License and attribution

Licensed under the [Apache License, Version 2.0](https://github.com/kenningai/kenning-continuity/blob/main/LICENSE). Use it, modify it,
run it in production, commercially or otherwise.

The work here is the ontology — the type lattice, the resolution-by-walk, the
placement of epistemic standing in edges, the principle that every invariant
lives in a tool. That is given away freely, and Apache-2.0 asks only that
attribution travel with it: keep `LICENSE` and `NOTICE`, and mark what you
changed.

The Kenning AI name is not part of that grant (Apache-2.0 §6). See
[`TRADEMARKS.md`](https://github.com/kenningai/kenning-continuity/blob/main/TRADEMARKS.md) for what you may do without asking — which is
nearly everything, including saying what your software is built on.

## Contact

**contact@kenningai.com** — no form, no funnel, it reaches us directly.

We are especially interested in hearing from anyone who has run an accumulating
memory system at scale and formed a view on why they fail. That failure mode is
the reason for nearly every decision in this repository, and disagreement from
someone who has hit it themselves is worth more to us than agreement from
anyone who hasn't.

---

Kenning AI · [kenningai.com](https://kenningai.com)
