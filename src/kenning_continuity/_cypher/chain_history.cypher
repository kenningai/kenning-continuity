MATCH (:Domain {name: $domain})-[:HAS_SESSION]->(:Session)
     -[:DISCOVERED]->(head:Knowledge {name: $name})
WHERE NOT (head)<-[:EVOLVED_FROM]-()
WITH DISTINCT head
MATCH path = (head)-[:EVOLVED_FROM*0..]->(n)
WITH n, length(path) AS depth
RETURN n.name AS name,
       n.ont_type AS type,
       labels(n) AS labels,
       $domain AS domain,
       n.description AS description,
       n.t_created AS t_created,
       depth
ORDER BY n.t_created ASC
