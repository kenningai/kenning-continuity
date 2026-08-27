// What already occupies a (name, domain) that create_knowledge just refused.
//
// Runs only on the collision path. The refusal names three options — evolve,
// confirm, rename — but choosing between them requires knowing what is there,
// so before this the agent had to search, read, decide, then act: four calls
// to make one decision. 89% of the production graph has never been evolved,
// which is what a four-call decision produces.
MATCH (:Domain {name: $domain})-[:HAS_SESSION]->(:Session)
     -[:DISCOVERED]->(existing:Knowledge {name: $name})
WHERE NOT (existing)<-[:EVOLVED_FROM]-()
RETURN DISTINCT existing.description AS description,
       existing.ont_type AS ont_type,
       labels(existing) AS labels,
       toString(existing.t_created) AS t_created
