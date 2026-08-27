// Find the head and take its write lock, in the transaction that will write
// the successor. SET is what acquires the lock; the property is removed again
// by knowledge_evolve.cypher before commit, so the predecessor leaves this
// transaction exactly as it entered. A concurrent evolve of the same head
// blocks here until this transaction commits, then finds — in
// knowledge_evolve.cypher's re-check — that the node is no longer head.
MATCH (:Domain {name: $domain})-[:HAS_SESSION]->(:Session)
     -[:DISCOVERED]->(old:Knowledge {name: $name})
WHERE NOT (old)<-[:EVOLVED_FROM]-()
WITH DISTINCT old
SET old._evolve_lock = true
RETURN elementId(old) AS eid, labels(old) AS labels, old.ont_type AS ont_type
