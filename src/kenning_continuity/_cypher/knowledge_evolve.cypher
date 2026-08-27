// The successor is discovered by the evolving session, which is what places it
// in a domain. It inherits no domain property because there is none to inherit.
MATCH (:Domain {name: $domain})-[:HAS_SESSION]->(:Session)
     -[:DISCOVERED]->(old:Knowledge:`__ont_label__` {name: $name})
WHERE NOT (old)<-[:EVOLVED_FROM]-()
WITH DISTINCT old
MATCH (s:Session {session_id: $session_id})
CREATE (new:__new_labels__ {
  name: old.name,
  description: $new_description,
  ont_type: $ont_type,
  t_created: datetime()
})
__sources_clause__
__t_valid_clause__
CREATE (new)-[:EVOLVED_FROM]->(old)
CREATE (s)-[d:DISCOVERED]->(new)
__basis_clause__
RETURN new.name AS name, new.ont_type AS type, labels(new) AS labels,
       new.description AS description, $domain AS domain
