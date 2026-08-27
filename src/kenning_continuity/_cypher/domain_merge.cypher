MERGE (d:Domain {name: $name})
ON CREATE SET d.t_created = datetime(),
              d.description = $description
RETURN d.name AS name, d.description AS description,
       d.t_created AS t_created
