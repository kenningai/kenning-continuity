CALL db.index.fulltext.queryNodes('knowledge_index', $query) YIELD node, score
WHERE NOT (node)<-[:EVOLVED_FROM]-()
WITH node, score,
     [(d:Domain)-[:HAS_SESSION]->(:Session)-[:DISCOVERED]->(node) | d.name][0] AS domain
WHERE ($domain IS NULL OR domain = $domain)
WITH node, score, domain
ORDER BY score DESC
LIMIT $limit
RETURN node.name AS name, node.ont_type AS type, labels(node) AS labels,
       domain, node.description AS description,
       score, node.t_created AS t_created
