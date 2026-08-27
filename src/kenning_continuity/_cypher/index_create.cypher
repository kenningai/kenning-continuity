CREATE FULLTEXT INDEX knowledge_index IF NOT EXISTS
FOR (n:Actor|Structure|Artifact|Place|Event|Insight|Pattern|Challenge|Solution|Lesson|Rationale)
ON EACH [n.name, n.description]
