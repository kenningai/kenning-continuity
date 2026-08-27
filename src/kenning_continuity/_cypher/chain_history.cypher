MATCH (:Domain {name: $domain})-[:HAS_SESSION]->(:Session)
     -[:DISCOVERED]->(head:Knowledge {name: $name})
WHERE NOT (head)<-[:EVOLVED_FROM]-()
WITH DISTINCT head
MATCH path = (head)-[:EVOLVED_FROM*0..]->(n)
WITH n, length(path) AS depth
// The reason this version replaced its predecessor lives on the edge leaving
// it. The first version has no such edge; a pre-v0.2.0 one has the edge and
// no reason. Both come back null, and are told apart by position.
OPTIONAL MATCH (n)-[ef:EVOLVED_FROM]->()
WITH n, depth, ef.revision_why AS revision_why
// Authorship by walk: Version <- DISCOVERED <- Session <- DIRECTED <- Person.
OPTIONAL MATCH (s:Session)-[:DISCOVERED]->(n)
OPTIONAL MATCH (p:Person)-[:DIRECTED]->(s)
WITH n, depth, revision_why, s, p ORDER BY s.timestamp ASC
WITH n, depth, revision_why,
     [x IN collect(CASE WHEN s IS NULL THEN null ELSE
        {session_id: s.session_id, director: p.name,
         timestamp: s.timestamp, purpose: s.purpose} END) WHERE x IS NOT NULL]
       AS discovered_by
RETURN n.name AS name,
       n.ont_type AS type,
       labels(n) AS labels,
       $domain AS domain,
       n.description AS description,
       n.t_created AS t_created,
       revision_why,
       discovered_by,
       depth
// Oldest first, by position in the chain rather than by clock: the edges are
// the order, and a stamp is only a reading taken beside them.
ORDER BY depth DESC
