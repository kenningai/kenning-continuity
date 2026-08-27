MATCH (d:Domain {name: $domain})-[:HAS_SESSION]->(s:Session)
WITH s ORDER BY s.timestamp ASC
LIMIT $limit
OPTIONAL MATCH (dir:Person)-[:DIRECTED]->(s)
WITH s, dir
OPTIONAL MATCH (s)-[:DISCOVERED]->(e)
WITH s, dir, count(e) AS entities_discovered
OPTIONAL MATCH (s)-[:CONFIRMED]->(c)
WITH s, dir, entities_discovered, count(c) AS entities_confirmed
RETURN s.session_id AS session_id,
       dir.name AS director, dir.source_key AS director_key,
       s.timestamp AS timestamp, s.purpose AS purpose,
       s.summary AS summary, s.status AS status,
       entities_discovered, entities_confirmed
ORDER BY s.timestamp ASC
