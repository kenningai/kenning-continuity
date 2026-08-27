"""Migration: pre-ontology graph → the knowledge-focused ontology.

For a graph written by a predecessor schema — process nodes labelled Run,
knowledge nodes carrying Gotcha/Change/Dependency, edges named RELATES_TO,
and no ontological labels or ont_type at all. If your graph was created by
this package, you do not need this.

Run with:
    python -m kenning_continuity._migration.knowledge_focus \\
        --db-url bolt://localhost:7687 \\
        --username neo4j --password ... \\
        --database <database-name> \\
        [--dry-run]

`--database` is REQUIRED. Neo4j Desktop 2 hosts multiple named databases on
a single instance; specifying the wrong one is destructive.

Phases:
  1. Backup advisory (out-of-band — script does NOT take a backup; you must)
  2. Process layer rename (Run → Session, HAS_RUN → HAS_SESSION, etc.)
  3. Knowledge layer label renames (Gotcha → Lesson, Change → Event)
  4. Knowledge layer edge rename (RELATES_TO → ASSOCIATED_WITH)
  5. Add ontological labels (Actor on Person/Organization; Structure on
     System/Process/Configuration)
  6. Add ont_type property to every knowledge node
  7. Dependency reclassification (operator-assisted — prints helper query)
  8. Fulltext index rebuild
  9. Verification queries

Each phase is independently reversible if the migration is wrong. Idempotent:
re-running the script after a partial failure picks up where it left off
(except Phase 1, which is advisory only).
"""
import argparse
import asyncio
import logging
import sys
from typing import Any

from neo4j import AsyncGraphDatabase, RoutingControl


logger = logging.getLogger("kenning_continuity._migration.knowledge_focus")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# Phase queries -------------------------------------------------------------

PHASE_2_PROCESS_RENAMES = [
    ("Session label", "MATCH (n:Run) REMOVE n:Run SET n:Session RETURN count(n) AS n"),
    ("session_id property", "MATCH (n:Session) WHERE n.run_id IS NOT NULL "
                            "SET n.session_id = n.run_id REMOVE n.run_id RETURN count(n) AS n"),
    ("HAS_RUN → HAS_SESSION", "MATCH (a)-[r:HAS_RUN]->(b) "
                              "MERGE (a)-[r2:HAS_SESSION]->(b) "
                              "ON CREATE SET r2 = properties(r) "
                              "DELETE r RETURN count(r2) AS n"),
    ("NEXT_RUN → NEXT_SESSION", "MATCH (a)-[r:NEXT_RUN]->(b) "
                                "MERGE (a)-[r2:NEXT_SESSION]->(b) "
                                "ON CREATE SET r2 = properties(r) "
                                "DELETE r RETURN count(r2) AS n"),
]

PHASE_3_KNOWLEDGE_LABEL_RENAMES = [
    ("Gotcha → Lesson", "MATCH (n:Gotcha) REMOVE n:Gotcha SET n:Lesson RETURN count(n) AS n"),
    ("Change → Event", "MATCH (n:Change) REMOVE n:Change SET n:Event RETURN count(n) AS n"),
]

PHASE_4_EDGE_RENAMES = [
    ("RELATES_TO → ASSOCIATED_WITH", "MATCH (a)-[r:RELATES_TO]->(b) "
                                     "MERGE (a)-[r2:ASSOCIATED_WITH]->(b) "
                                     "ON CREATE SET r2 = properties(r) "
                                     "DELETE r RETURN count(r2) AS n"),
]

PHASE_5_ONTOLOGICAL_LABELS = [
    ("Actor (from Person/Organization)",
     "MATCH (n) WHERE (n:Person OR n:Organization) AND NOT n:Actor "
     "SET n:Actor RETURN count(n) AS n"),
    ("Structure (from System/Process/Configuration)",
     "MATCH (n) WHERE (n:System OR n:Process OR n:Configuration) AND NOT n:Structure "
     "SET n:Structure RETURN count(n) AS n"),
]

PHASE_6_ONT_TYPE_PROPERTY = [
    ("Actor", "MATCH (n:Actor) WHERE n.ont_type IS NULL SET n.ont_type = 'Actor' RETURN count(n) AS n"),
    ("Structure", "MATCH (n:Structure) WHERE n.ont_type IS NULL SET n.ont_type = 'Structure' RETURN count(n) AS n"),
    ("Artifact", "MATCH (n:Artifact) WHERE n.ont_type IS NULL SET n.ont_type = 'Artifact' RETURN count(n) AS n"),
    ("Event", "MATCH (n:Event) WHERE n.ont_type IS NULL SET n.ont_type = 'Event' RETURN count(n) AS n"),
    ("Insight", "MATCH (n:Insight) WHERE n.ont_type IS NULL SET n.ont_type = 'Insight' RETURN count(n) AS n"),
    ("Pattern", "MATCH (n:Pattern) WHERE n.ont_type IS NULL SET n.ont_type = 'Pattern' RETURN count(n) AS n"),
    ("Challenge", "MATCH (n:Challenge) WHERE n.ont_type IS NULL SET n.ont_type = 'Challenge' RETURN count(n) AS n"),
    ("Solution", "MATCH (n:Solution) WHERE n.ont_type IS NULL SET n.ont_type = 'Solution' RETURN count(n) AS n"),
    ("Lesson", "MATCH (n:Lesson) WHERE n.ont_type IS NULL SET n.ont_type = 'Lesson' RETURN count(n) AS n"),
    ("Rationale", "MATCH (n:Rationale) WHERE n.ont_type IS NULL SET n.ont_type = 'Rationale' RETURN count(n) AS n"),
]

PHASE_7_DEPENDENCY_HELPER = (
    "MATCH (d:Dependency) "
    "OPTIONAL MATCH (d)-[r_out]->(out) "
    "WHERE NOT type(r_out) IN ['DISCOVERED', 'CONFIRMED'] "
    "OPTIONAL MATCH (in_node)-[r_in]->(d) "
    "WHERE NOT type(r_in) IN ['DISCOVERED', 'CONFIRMED', 'HAS_SESSION', 'NEXT_SESSION'] "
    "RETURN d.name AS name, d.domain AS domain, d.description AS description, "
    "       collect(DISTINCT {dir: 'out', target: out.name, type: type(r_out)}) AS outgoing, "
    "       collect(DISTINCT {dir: 'in', source: in_node.name, type: type(r_in)}) AS incoming"
)

PHASE_8_INDEX_REBUILD = [
    ("Drop old fulltext index", "DROP INDEX knowledge_index IF EXISTS"),
    ("Create new fulltext index",
     "CREATE FULLTEXT INDEX knowledge_index IF NOT EXISTS "
     "FOR (n:Actor|Structure|Artifact|Event|Insight|Pattern|Challenge|Solution|Lesson|Rationale) "
     "ON EACH [n.name, n.description]"),
]

PHASE_9_VERIFICATION = [
    ("Every knowledge node has exactly one ontological label",
     "MATCH (n) WHERE n.ont_type IS NOT NULL "
     "WITH n, [l IN labels(n) WHERE l IN ['Actor','Structure','Artifact','Event',"
     "'Insight','Pattern','Challenge','Solution','Lesson','Rationale']] AS ont_labels "
     "WHERE size(ont_labels) <> 1 "
     "RETURN n.name AS name, n.domain AS domain, labels(n) AS labels, ont_labels"),
    ("Every knowledge node's ont_type matches its ontological label",
     "MATCH (n) WHERE n.ont_type IS NOT NULL "
     "WITH n, [l IN labels(n) WHERE l = n.ont_type][0] AS match "
     "WHERE match IS NULL "
     "RETURN n.name AS name, n.domain AS domain, n.ont_type AS ont_type, labels(n) AS labels"),
    ("No legacy labels remain (Run/Gotcha/Change)",
     "MATCH (n) WHERE n:Run OR n:Gotcha OR n:Change "
     "RETURN labels(n) AS labels, count(n) AS n"),
    ("No legacy edge types remain (HAS_RUN/NEXT_RUN/RELATES_TO)",
     "MATCH ()-[r:HAS_RUN|NEXT_RUN|RELATES_TO]->() "
     "RETURN type(r) AS type, count(r) AS n"),
    ("Dependency nodes remaining (operator must reclassify per Phase 7)",
     "MATCH (n:Dependency) RETURN count(n) AS n"),
]


# Runner --------------------------------------------------------------------

async def _run_phase(driver, name: str, queries: list[tuple[str, str]], dry_run: bool) -> None:
    logger.info(f"=== {name} ===")
    for label, query in queries:
        if dry_run:
            logger.info(f"  [DRY RUN] {label}: {query[:120]}{'…' if len(query) > 120 else ''}")
            continue
        try:
            result = await driver.execute_query(query, routing_control=RoutingControl.WRITE)
            if result.records:
                count = result.records[0].get("n", "n/a")
                logger.info(f"  {label}: {count} affected")
            else:
                logger.info(f"  {label}: no records returned")
        except Exception as e:
            logger.error(f"  {label}: FAILED — {e}")
            raise


async def _run_dependency_helper(driver, dry_run: bool) -> None:
    logger.info("=== Phase 7: Dependency reclassification (operator-assisted) ===")
    if dry_run:
        logger.info(f"  [DRY RUN] Would query: {PHASE_7_DEPENDENCY_HELPER[:120]}…")
        return
    result = await driver.execute_query(PHASE_7_DEPENDENCY_HELPER, routing_control=RoutingControl.READ)
    if not result.records:
        logger.info("  No Dependency nodes found. Phase 7 complete.")
        return
    logger.warning(f"  Found {len(result.records)} Dependency nodes requiring operator review.")
    logger.warning("  Reclassify each as follows:")
    logger.warning("    REMOVE n:Dependency SET n:Structure:System SET n.ont_type = 'Structure'  // common case")
    logger.warning("    REMOVE n:Dependency SET n:Actor:Organization SET n.ont_type = 'Actor'    // vendor case")
    for record in result.records:
        d: dict[str, Any] = dict(record)
        logger.warning(f"    - {d.get('domain')!r}/{d.get('name')!r}: {d.get('description', '')[:80]}")


async def _run_verification(driver) -> bool:
    logger.info("=== Phase 9: Verification ===")
    all_ok = True
    for label, query in PHASE_9_VERIFICATION:
        try:
            result = await driver.execute_query(query, routing_control=RoutingControl.READ)
            issues = result.records or []
            if not issues:
                logger.info(f"  {label}: OK")
            else:
                # The "Dependency nodes remaining" check returns one row with count=0 in success case
                if "Dependency nodes remaining" in label:
                    n = issues[0].get("n", 0) if issues else 0
                    if n == 0:
                        logger.info(f"  {label}: OK (0 remaining)")
                    else:
                        logger.warning(f"  {label}: {n} remain — Phase 7 incomplete")
                else:
                    logger.error(f"  {label}: {len(issues)} issue(s)")
                    for issue in issues[:5]:
                        logger.error(f"    {dict(issue)}")
                    all_ok = False
        except Exception as e:
            logger.error(f"  {label}: query FAILED — {e}")
            all_ok = False
    return all_ok


async def migrate(uri: str, user: str, password: str, database: str, dry_run: bool) -> int:
    logger.warning("=" * 70)
    logger.warning("Phase 1: BACKUP ADVISORY")
    logger.warning("This script does NOT take a backup. Confirm you have one before continuing.")
    logger.warning("Migration is destructive in spots. Press Ctrl+C now if unsure.")
    logger.warning("=" * 70)

    driver = AsyncGraphDatabase.driver(uri, auth=(user, password), database=database)
    try:
        if not dry_run:
            await driver.verify_connectivity()
            logger.info(f"Connected to Neo4j at {uri}")
        else:
            logger.info(f"[DRY RUN] Would connect to Neo4j at {uri}")

        await _run_phase(driver, "Phase 2: Process layer rename", PHASE_2_PROCESS_RENAMES, dry_run)
        await _run_phase(driver, "Phase 3: Knowledge label renames", PHASE_3_KNOWLEDGE_LABEL_RENAMES, dry_run)
        await _run_phase(driver, "Phase 4: Edge rename (RELATES_TO → ASSOCIATED_WITH)", PHASE_4_EDGE_RENAMES, dry_run)
        await _run_phase(driver, "Phase 5: Add ontological labels", PHASE_5_ONTOLOGICAL_LABELS, dry_run)
        await _run_phase(driver, "Phase 6: Add ont_type property", PHASE_6_ONT_TYPE_PROPERTY, dry_run)
        await _run_dependency_helper(driver, dry_run)
        await _run_phase(driver, "Phase 8: Fulltext index rebuild", PHASE_8_INDEX_REBUILD, dry_run)

        if dry_run:
            logger.info("Dry run complete. No changes were made. Re-run without --dry-run to apply.")
            return 0

        ok = await _run_verification(driver)
        if ok:
            logger.info("Migration complete. Graph is on the current ontology.")
            return 0
        else:
            logger.error("Migration FAILED verification. Review issues above and address before use.")
            return 2
    finally:
        await driver.close()


def main():
    parser = argparse.ArgumentParser(
        description="Migrate a pre-ontology knowledge graph onto the current ontology.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--db-url", required=True, help="Neo4j bolt URL (e.g., bolt://localhost:7687)")
    parser.add_argument("--username", default="neo4j", help="Neo4j username (default: neo4j)")
    parser.add_argument("--password", required=True, help="Neo4j password")
    parser.add_argument("--database", required=True, help="Neo4j database name (REQUIRED — Neo4j Desktop 2 hosts multiple named databases on one instance; running against the wrong one is destructive)")
    parser.add_argument("--dry-run", action="store_true", help="Print phase queries without executing")
    args = parser.parse_args()

    rc = asyncio.run(migrate(
        uri=args.db_url, user=args.username, password=args.password,
        database=args.database, dry_run=args.dry_run,
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
