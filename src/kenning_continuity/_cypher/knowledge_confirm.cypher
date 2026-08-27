MATCH (:Domain {name: $domain})-[:HAS_SESSION]->(:Session)
     -[:DISCOVERED]->(e:Knowledge {name: $name})
WHERE NOT (e)<-[:EVOLVED_FROM]-()
WITH DISTINCT e
MATCH (s:Session {session_id: $session_id})
MERGE (s)-[c:CONFIRMED]->(e)
ON CREATE SET c.t_created = datetime()
RETURN e.name AS name, e.ont_type AS type, labels(e) AS labels,
       $domain AS domain, e.description AS description
