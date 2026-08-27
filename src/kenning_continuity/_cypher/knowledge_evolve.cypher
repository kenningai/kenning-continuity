// Runs after knowledge_evolve_head.cypher, in the same transaction, holding
// the predecessor's write lock. The head check is repeated HERE, under the
// lock: a session that evolved this node while we waited has committed by now,
// so its EVOLVED_FROM is visible and nothing matches. That empty result is the
// refusal. Checking only before the lock is how a chain forks.
//
// The successor is discovered by the evolving session, which is what places it
// in a domain. It inherits no domain property because there is none to inherit.
// The session is not copied onto EVOLVED_FROM either: it is one hop away, via
// the DISCOVERED edge written in this statement.
MATCH (old:Knowledge:`__ont_label__`)
WHERE elementId(old) = $old_eid
  AND NOT (old)<-[:EVOLVED_FROM]-()
MATCH (s:Session {session_id: $session_id})
REMOVE old._evolve_lock
CREATE (new:__new_labels__ {
  name: old.name,
  description: $new_description,
  ont_type: $ont_type,
  t_created: datetime()
})
__sources_clause__
__t_valid_clause__
CREATE (new)-[:EVOLVED_FROM {revision_why: $revision_why}]->(old)
CREATE (s)-[d:DISCOVERED]->(new)
__basis_clause__
RETURN new.name AS name, new.ont_type AS type, labels(new) AS labels,
       new.description AS description, $domain AS domain
