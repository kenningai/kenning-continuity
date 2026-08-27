// See _resolve.md — domain is a query parameter, resolved by walk.
OPTIONAL MATCH (:Domain {name: $domain})-[:HAS_SESSION]->(:Session)
              -[:DISCOVERED]->(existing:Knowledge {name: $name})
WHERE NOT (existing)<-[:EVOLVED_FROM]-()
WITH existing
WHERE existing IS NULL
MATCH (s:Session {session_id: $session_id})
CREATE (e:__labels__ {
  name: $name,
  description: $description,
  ont_type: $ont_type,
  t_created: datetime()
})
__sources_clause__
__t_valid_clause__
CREATE (s)-[d:DISCOVERED]->(e)
__basis_clause__
RETURN e.name AS name, e.ont_type AS type, labels(e) AS labels
