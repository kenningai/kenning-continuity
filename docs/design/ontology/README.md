# Ontology models

The ontology is authored here, in TypeQL, **before** it is built. The Neo4j
implementation is downstream of this directory, not alongside it.

| file | what it is |
|---|---|
| `v0.1.1.tql` | the ontology as shipped — what the tools enforce today |

## The loop

1. Model the change in TypeQL. Load it. If it will not commit, the design is wrong.
2. Agree the model.
3. Build the Neo4j implementation from it.
4. When production surfaces a problem: **identify it in the model, fix the model,
   agree the fix, then update the graph and the logic.** Never the other way round.

Step 4 is the whole point and it is the step that gets skipped at 11pm during an
incident. So it is gated rather than trusted — see below.

## Why a declared schema rather than a description logic

OWL's open-world assumption reads an absent property as *unknown*. These
substrates read an absent property as *the frame did not assert it*, which is a
positive fact and the reason the ETL refuses to infer. Frame fidelity is
symmetric — no out-of-frame assertions, no in-frame omissions — and under OWA
the most deliberate act in the discipline, refusing to fill a gap, is
indistinguishable from ignorance. Closed-world is not a convenience here. It is
what "model the frame as it is" already commits to.

OWL/SHACL remains available for one narrow job: deciding whether a set of
*axioms* entails something nobody wrote. Reach for it only when an ontology has
axioms that constrain each other — disjointness or exclusivity between types,
properties declared functional or unique whose values could silently
distinguish individuals, or a relation declared inverse/transitive/symmetric
whose interaction is not obvious by inspection. A frame transcription has none
of those and gains nothing from a reasoner.

## The gate

`tests/unit/test_ontology_model.py` parses the model and asserts the Python
constants match it — ontological types, sub-labels *and the root each resolves
to by walking the declared chain*, connection types, process entities and
process edges. Set equality in both directions, so a type added to the code
fails as loudly as one dropped from the model.

Proven two-sided by mutation in both directions: dropping `rationale` from the
model fails; adding `Intention` to `KnowledgeType` fails.

## The residue — what is NOT in the model

Read this before treating `*.tql` as the specification. Not everything a
production graph enforces is expressible in a declared schema, and the gap is
where the next recall lives.

- **`EVOLVED_FROM` acyclicity.** Not expressible in TypeQL, and not in OWL 2 DL
  either — the profile forbids irreflexivity on transitive properties, so it
  refuses exactly the axiom that would catch a cycle. Cypher enforces it
  trivially. It stays a tool guard.
- **Head-of-chain semantics.** `WHERE NOT (e)<-[:EVOLVED_FROM]-()` is a query
  pattern, not a schema fact. The entire notion of "current state" — which is
  what `get_domain_state`, `evolve_knowledge`, `confirm_knowledge` and
  `create_connections` are all built on — is invisible to the model.
- **Cardinality that depends on chain position.** "Exactly one head per
  name+domain" is a property of a traversal, not of a type.
- **Indexes, payload bounds, GDS projections, read timeouts.** Not ontology.
- **Stranded edges, oversized re-entry payloads, an unscoped
  `search_connections`.** Application logic and query authoring. Reviewing this
  model against the implementation, a declared schema caught half the defects
  found; these are the other half. Anyone selling it as a silver bullet is
  selling that other half.
- **A declaration nothing uses.** An unowned attribute is legal TypeQL — the
  schema is not inconsistent, it just carries a dead type, so it loads green.
  A draft declared `attribution-method` with its full value vocabulary and
  never bound it to `directed`; the gate that reads that property therefore had
  nothing to read, and the surrounding documents claimed a property the model
  did not carry. Found by inspection, not by either instrument. Now gated by
  `TestNoOrphanedAttributes` in `tests/unit/test_ontology_model.py`, in both
  directions — an attribute nothing owns fails, and an `owns` naming an
  undeclared attribute fails.

  Worth stating plainly, since it cuts against the case for the tooling: a
  declared schema refuses contradictions between the things you wrote. It does
  not refuse a thing you wrote and then forgot to connect. That is the mirror of
  the OWL finding — a reasoner reported an entailment nobody authored; TypeDB
  stayed silent about an authorship that entailed nothing.

The discipline is to name the checkable surface correctly, not maximally. A
model presented as complete when it is partial trains people to trust the part
it never covered.

## Loading a model

TypeDB Community, one container, one database per ontology — which is the
operational reason this directory exists at all. Neo4j Community gives you a
single database named `neo4j`, so every ontology needs its own server; this
machine currently runs two Neo4j containers at ~1GB each for two ontologies,
against one 210MB TypeDB container holding three databases.

```bash
docker run -d --name typedb -p 1729:1729 -p 8000:8000 typedb/typedb:latest

TOK=$(curl -s -X POST http://localhost:8000/v1/signin \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"password"}' | jq -r .token)

curl -s -X POST "http://localhost:8000/v1/databases/tk_v080" \
  -H "Authorization: Bearer $TOK"

python3 - <<'PY'
import json, os, urllib.request
q = open('docs/design/ontology/v0.1.1.tql').read()
body = json.dumps({"databaseName": "tk_v080", "transactionType": "schema",
                   "query": q, "commit": True}).encode()
req = urllib.request.Request("http://localhost:8000/v1/query", data=body,
    headers={"Authorization": "Bearer " + os.environ["TOK"],
             "Content-Type": "application/json"})
print(urllib.request.urlopen(req).read().decode())
PY
```

Validated against TypeDB 3.12.3.

## The gate is two-sided, and it is run against the loaded schema

The unit test (`tests/unit/test_ontology_model.py`) *parses* this directory. It
cannot tell you the TypeQL still commits — a file edited by hand can be
syntactically dead and the parser will not notice.

So the model is reloaded from scratch into a fresh database and probed, and the
probes assert refusals as well as acceptances. Last run, 12 for 12:

| probe | verdict |
|---|---|
| a person directs a session | ACCEPT |
| a person-**referent** directs a session | REFUSE `INF11` |
| a knowledge edge touches a session | REFUSE `INF11` |
| a referent points at a process person | REFUSE `INF11` |
| `referent-claim` with resolution `maybe` | REFUSE `CNT8` |
| `referent-claim` with resolution `distinct`, discovered | ACCEPT |
| a session with status `paused` | REFUSE `CNT8` |
| a discovery with basis `probably` | REFUSE `CNT8` |
| a `DIRECTED` with method `guessed` | REFUSE `CNT8` |
| `confidence` on a knowledge node | REFUSE `INF2` |
| an `inquiry` | REFUSE `INF2` |
| a knowledge entity no session discovered | REFUSE `CNT5` |

**A failed seed makes every subsequent probe a false pass.** The first attempt
at this run seeded two `person-referent`s with no `discovered` edge; the seed
failed `CNT5`, so every `match`-based probe matched nothing, inserted nothing,
and returned no error — six acceptances that were really emptiness. Assert the
seed committed before reading any verdict.

---

## Where TypeQL and Neo4j do not line up

The model is authored in TypeQL and built in Neo4j, and the two are not direct
analogs. This table is not a negotiation between them — it is downstream of the
model like everything else. The model says what is true; each row records how
Neo4j is made to carry that meaning where it cannot state it directly.

Nothing leads the model. Not the code, not the store's limitations, not an
incident. A limitation of Neo4j is a fact about Neo4j, and the response is to
represent the model faithfully within it and write the representation down —
never to bend the model toward what the store finds easy.

A divergence that is not in this table is a defect, not a design.

The test gate (`tests/unit/test_ontology_model.py`) checks the type *surface* —
entities, sub-label chains, edges, process names, orphaned attributes. It cannot
check these, because they are about what the two formalisms can say at all.

| the model says | Neo4j cannot | the representation | why it carries the meaning |
|---|---|---|---|
| `knowledge-link plays discovered:found @card(1..1)` — every edge is discovered by a session | make a relationship the endpoint of another relationship | `session_id` property on every knowledge edge, written by `create_connections` and `claim_referent` | The edge reaches its author in two hops: `Session {session_id}` ← `DIRECTED` ← `Person`. What makes an assertion adjudicable here is that someone made it in a session, and that is exactly what is recorded. |
| `person`, `session`, `domain` are entities with no supertype | distinguish a layer structurally | `PROCESS_TYPES` guards in the tools, plus a `:Knowledge` marker label on every knowledge node | The marker replaced ten separately-maintained exclusion lists, each of which had to be extended by hand every time the process layer grew. A positive assertion does not drift. |
| `knowledge` and `session` do not own `domain` | nothing — the model is right | `domain` is a **query parameter**, never a stored property. An entity is reached from its Domain: `(:Domain {name})-[:HAS_SESSION]->(:Session)-[:DISCOVERED]->(e:Knowledge {name})`. Measured on production: 3,101 of 3,101 resolve to exactly one domain, none to zero or more. The property this replaced had already drifted — 21 entities named a real Domain with no path to them at all. **Note the walk is unambiguous by measurement, not by construction:** `discovered:found` is `@card(1..)`, so an entity discovered by sessions in two domains resolves to two, and the tools refuse it rather than pick. |
| `team sub organization sub actor` | resolve a label chain | all levels applied: `:Knowledge:Actor:Organization:Team` | A query for organizations finds teams, which is what `sub` means. `SUB_LABEL_PARENT` holds parent pointers and the flat root map is derived from it, so the two cannot disagree. |
| `@card(1..1)` on `directed`, `session-status`, `resolution`, `attribution-method` | express cardinality | refusal in the tool | `begin_session` refuses a session with no director; `claim_referent` refuses an unknown resolution. Every invariant in this project lives in a tool, not in instructions. |
| `owns source-key @key` — unique AND required | require a property (Community edition) | uniqueness constraint in `ensure_schema`, requiredness in `_validate_director` | Existence constraints are Enterprise-only and the test image is Community. The required half is enforced where the rest of the guard already is. |
| `knowledge plays discovered:found @card(1..)` — no knowledge entity exists that no session discovered | require a node to participate in a relationship | `create_knowledge` and `evolve_knowledge` write the `DISCOVERED` edge in the same statement that creates the node | The model **refuses** an orphan outright — an `insert` of an undiscovered `insight` fails with `CNT5` at commit. Neo4j will happily hold one, so the guarantee is the tool's. |
| `knowledge-link plays discovered:found @card(1..1)` again, from the write side | as above, for relationships | `session_id` is a **required** argument to `create_connections` and `claim_referent`, not an optional one | A `referent-claim` inserted with no discovering session fails with `CNT5`. The model does not treat edge provenance as a nice-to-have, so neither does the tool. |
| relation names are hyphenated lowercase (`associated-with`, `next-session`) | — | `SCREAMING_SNAKE` (`ASSOCIATED_WITH`, `NEXT_SESSION`) | Notation, not meaning. The gate converts between them so a rename in either place fails. |
| entity names are hyphenated lowercase (`person-referent`) | — | `PascalCase` labels (`PersonReferent`) | As above. |

### What is deliberately not in the model

Not everything a production graph enforces is expressible in a declared schema,
and pretending otherwise is how the next recall gets built.

- **`EVOLVED_FROM` acyclicity.** Neither TypeQL nor OWL 2 DL can state it — the
  DL profile forbids irreflexivity on transitive properties, i.e. it refuses
  exactly the axiom that would catch a cycle. Cypher enforces it trivially.
- **Head-of-chain semantics.** `WHERE NOT (e)<-[:EVOLVED_FROM]-()` is a query
  pattern, not a schema fact. The entire notion of "current state" is invisible
  to the model.
- **Indexes, payload bounds, GDS projections.** Not ontology at all.

### Derivable properties are not stored, and the rule has been applied against itself

A property recoverable in one hop is not written. Applied to `t_created` on
`EVOLVED_FROM` (the successor's own stamp), to `t_created` on `DIRECTED` (the
session's), and to `t-superseded` on `knowledge`, which was struck from the
model for exactly this reason after being declared.

The one retained exception is `t_created` on knowledge edges. It is redundant
in principle for any edge carrying a `session_id`, and it is kept because an
edge authored before the person layer existed has no author, leaving it as the
only temporal anchor such an edge has.
