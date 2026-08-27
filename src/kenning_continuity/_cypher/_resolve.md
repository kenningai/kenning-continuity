The canonical resolution of a knowledge entity by (name, domain).

    MATCH (:Domain {name: $domain})-[:HAS_SESSION]->(:Session)
          -[:DISCOVERED]->(e:Knowledge {name: $name})
    WHERE NOT (e)<-[:EVOLVED_FROM]-()

`domain` is a query parameter, never a stored property. A knowledge entity is
downstream of exactly one Domain by construction — every entity is discovered by
at least one session (`@card(1..)` on `discovered:found`) and every session
belongs to exactly one domain (`@card(1..1)` on `has-session:held`) — so the
walk is total and unambiguous. Measured on the production graph: 3,101 of 3,101
resolve to exactly one domain, none to zero and none to more.

The property it replaces was a denormalization and had already drifted: 21
entities carried a `domain` string naming a real Domain that had no path to them
at all. A fact held as text beside a structure rather than by it.
