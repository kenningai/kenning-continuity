// A co-reference CLAIM between two knowledge-layer referents.
//
// Both endpoints are resolved by walking from their Domain — see _resolve.md —
// so a name living in eleven domains cannot produce a cross product, and a
// process node is unreachable by construction: a referent has no frame from
// which to perform an act of reference.
//
// `session_id` rides on the edge because a claim with no author is precisely
// what this edge exists to prevent — it is the vector that made the claim, not
// derivable from the endpoints. The claim's TIME is not stored: it is the
// session's timestamp, one hop away.
MATCH (:Domain {name: $source_domain})-[:HAS_SESSION]->(:Session)
     -[:DISCOVERED]->(source:Knowledge {name: $source})
WHERE NOT (source)<-[:EVOLVED_FROM]-()
MATCH (:Domain {name: $target_domain})-[:HAS_SESSION]->(:Session)
     -[:DISCOVERED]->(target:Knowledge {name: $target})
WHERE NOT (target)<-[:EVOLVED_FROM]-()
WITH DISTINCT source, target
MATCH (s:Session {session_id: $session_id})
MERGE (source)-[c:REFERENT_CLAIM]->(target)
SET c.resolution = $resolution, c.session_id = $session_id
RETURN source.name AS source, $source_domain AS source_domain,
       target.name AS target, $target_domain AS target_domain,
       c.resolution AS resolution, c.session_id AS session_id
