// Connections between the entities a search actually returned.
//
// Matching is by NODE IDENTITY, not by name, and edges resolve THROUGH the
// chain. The prior version matched `source.name IN $names AND target.name IN
// $names` against head nodes only, which held three defects, all measured live:
//   · no head-of-chain filter — a head shares its name with its own
//     predecessors, so a chain's EVOLVED_FROM edges surfaced as self-loops
//     (8 spurious rows on one `Alice Example` search)
//   · no domain scoping — an edge authored between two referents in one domain
//     was reported between their same-named counterparts in another (9 live
//     pairs, both directions)
//   · no chain resolution — an edge authored before either endpoint evolved
//     was unreachable (737 graph-wide)
//
// Cross-domain edges are NOT excluded: 138 are deliberately authored. The fix
// is identity, not same-domain confinement.
UNWIND $pairs AS pair
MATCH (:Domain {name: pair.domain})-[:HAS_SESSION]->(:Session)
     -[:DISCOVERED]->(n:Knowledge {name: pair.name})
WHERE NOT (n)<-[:EVOLVED_FROM]-()
WITH collect(DISTINCT n) AS nodes
UNWIND nodes AS source
MATCH (source)-[:EVOLVED_FROM*0..]->(anc)-[r]->(o)
WHERE NOT type(r) IN $excluded
MATCH (target)-[:EVOLVED_FROM*0..]->(o)
WHERE NOT (target)<-[:EVOLVED_FROM]-()
  AND target IN nodes
RETURN DISTINCT source.name AS source,
       [(sd:Domain)-[:HAS_SESSION]->(:Session)-[:DISCOVERED]->(source) | sd.name][0] AS source_domain,
       target.name AS target,
       [(td:Domain)-[:HAS_SESSION]->(:Session)-[:DISCOVERED]->(target) | td.name][0] AS target_domain,
       type(r) AS connectionType
