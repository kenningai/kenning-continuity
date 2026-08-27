// Current knowledge for a domain: head-of-chain entities, with their edges
// resolved THROUGH the chain.
//
// An edge is authored on whichever node was head at the time. Reading edges
// only between two head-of-chain nodes therefore strands every connection the
// moment either endpoint evolves — not deleted, unreachable. Measured live:
// 3,678 visible against 4,425 authored, so 737 were unreachable, and the
// stranding is symmetric (either endpoint being evolved is enough).
//
// `types` and `limit` bound the payload. begin_session used to return this
// whole thing inline: 498 heads and 600,820 characters on Service Docs,
// ~150k tokens as the first act of a session. Truncating descriptions would
// not have helped — they are uniformly long (p50 616 chars, p95 1,677, only
// 11 over 5k) — so selection is the only remedy.
//
// Append-only has no retraction, so an edge absent from a later chain node is
// not a retraction — every authored edge stays live. Dedup is by
// (source head, target head, type); a pair connected by two types keeps both.
// Entities are reached FROM the domain rather than filtered by a copy of its
// name — see _resolve.md. A target's domain is walked the same way.
MATCH (:Domain {name: $domain})-[:HAS_SESSION]->(:Session)-[:DISCOVERED]->(e:Knowledge)
WHERE NOT (e)<-[:EVOLVED_FROM]-()
  AND ($types IS NULL OR e.ont_type IN $types OR any(l IN labels(e) WHERE l IN $types))
WITH DISTINCT e
ORDER BY e.t_created DESC
__limit_clause__
OPTIONAL MATCH (e)-[:EVOLVED_FROM*0..]->(anc)-[r]->(o:Knowledge)
WHERE NOT type(r) IN $excluded
OPTIONAL MATCH (target:Knowledge)-[:EVOLVED_FROM*0..]->(o)
WHERE NOT (target)<-[:EVOLVED_FROM]-()
RETURN e.name AS name, e.ont_type AS type, labels(e) AS labels,
       __description_expr__ AS description, e.t_created AS t_created,
       collect(DISTINCT {
         target: target.name,
         target_domain: [(td:Domain)-[:HAS_SESSION]->(:Session)
                          -[:DISCOVERED]->(target) | td.name][0],
         type: type(r)}) AS connections
ORDER BY e.t_created DESC
