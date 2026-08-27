MERGE (p:Person {source_key: $director_key})
ON CREATE SET p.name = $director
WITH p
CREATE (s:Session {
  session_id: $session_id,
  timestamp: datetime(), purpose: $purpose,
  summary: '', status: $status
})
CREATE (p)-[:DIRECTED {method: 'platform-stated'}]->(s)
WITH s, p
MATCH (d:Domain {name: $domain})
CREATE (d)-[:HAS_SESSION]->(s)
WITH s, p
OPTIONAL MATCH (:Domain {name: $domain})-[:HAS_SESSION]->(prev:Session)
WHERE prev.session_id <> $session_id
WITH s, p, prev ORDER BY prev.timestamp DESC LIMIT 1
FOREACH (_ IN CASE WHEN prev IS NOT NULL THEN [1] ELSE [] END |
    CREATE (prev)-[:NEXT_SESSION]->(s)
)
RETURN s.session_id AS session_id, s.timestamp AS timestamp,
       p.name AS director, p.source_key AS director_key
