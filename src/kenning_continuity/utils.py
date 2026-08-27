import argparse
import os
import re
import logging
from pathlib import Path
from typing import Any, Literal, LiteralString, TypedDict, Union, cast

logger = logging.getLogger("kenning_continuity")
logger.setLevel(logging.INFO)

# ── Cypher Query Loading ─────────────────────────────────────

_CYPHER_DIR = Path(__file__).parent / "_cypher"
_CYPHER_CACHE: dict[str, str] = {}


def load_cypher(name: str, **replacements: str) -> LiteralString:
    """Load a Cypher query from _cypher/{name}.cypher.

    Caches file contents on first load. Template placeholders use
    __name__ convention (e.g. __label__, __rel_type__) to avoid
    collision with Cypher's {prop: $param} syntax.

    Args:
        name: Query filename without .cypher extension
        **replacements: Template substitutions (key → __key__ in file)
    """
    if name not in _CYPHER_CACHE:
        path = _CYPHER_DIR / f"{name}.cypher"
        _CYPHER_CACHE[name] = path.read_text()
    query = _CYPHER_CACHE[name]
    for key, value in replacements.items():
        query = query.replace(f"__{key}__", value)
    # The driver types its query parameter `LiteralString` to make string-built
    # queries — the SQL-injection shape — a type error. This project cannot
    # satisfy that literally: Neo4j will not parameterize a label or a
    # relationship type, so those must be substituted into the text.
    #
    # The cast is sound because substitution is closed, not open. Every value
    # reaching a placeholder comes from a validated set: `_resolve_labels`
    # raises on any type outside ONTOLOGICAL_TYPES / SUB_LABEL_PARENT,
    # `_validate_sub_label` guards the rest, ConnectionType is an enum, and
    # `_labels_string` backtick-quotes each part. No caller-supplied free text
    # ever reaches here — runtime values still travel as $params.
    #
    # It lives in this one function deliberately. Casting at each of the ~30
    # call sites would put the judgement where it cannot be reviewed.
    return cast(LiteralString, query)


def literal_cypher(query: str) -> LiteralString:
    """Assert that a runtime-built query is safe to hand the driver.

    Same reasoning as `load_cypher`'s cast, applied to the handful of queries
    built inline rather than from a template. Each call site carries its own
    one-line justification, because the judgement differs per site: some
    interpolate a validated enum, one deliberately forwards caller text under a
    read-only guard. Sharing the mechanism must not mean sharing the argument.
    """
    return cast(LiteralString, query)


# ── Utilities ────────────────────────────────────────────────

def format_namespace(namespace: str) -> str:
    """Format namespace by ensuring it ends with a hyphen if not empty."""
    if namespace:
        if namespace.endswith("-"):
            return namespace
        else:
            return namespace + "-"
    else:
        return ""

def _is_write_query(query: str) -> bool:
    """Check if the query is a write query."""
    return (
        re.search(r"\b(MERGE|CREATE|INSERT|SET|DELETE|REMOVE|ADD)\b", query, re.IGNORECASE)
        is not None
    )


def _value_sanitize(d: Any, list_limit: int = 128) -> Any:
    """
    Sanitize the input dictionary or list.

    Sanitizes the input by removing embedding-like values,
    lists with more than 128 elements, that are mostly irrelevant for
    generating answers in a LLM context. These properties, if left in
    results, can occupy significant context space and detract from
    the LLM's performance by introducing unnecessary noise and cost.

    Sourced from: https://github.com/neo4j/neo4j-graphrag-python/blob/main/src/neo4j_graphrag/schema.py#L88

    Parameters
    ----------
    d : Any
        The input dictionary or list to sanitize.
    list_limit : int
        The limit for the number of elements in a list.

    Returns
    -------
    Any
        The sanitized dictionary or list.
    """
    if isinstance(d, dict):
        new_dict = {}
        for key, value in d.items():
            if isinstance(value, dict):
                sanitized_value = _value_sanitize(value)
                if (
                    sanitized_value is not None
                ):  # Check if the sanitized value is not None
                    new_dict[key] = sanitized_value
            elif isinstance(value, list):
                if len(value) < list_limit:
                    sanitized_value = _value_sanitize(value)
                    if (
                        sanitized_value is not None
                    ):  # Check if the sanitized value is not None
                        new_dict[key] = sanitized_value
                # Do not include the key if the list is oversized
            else:
                new_dict[key] = value
        return new_dict
    elif isinstance(d, list):
        if len(d) < list_limit:
            return [
                _value_sanitize(item) for item in d if _value_sanitize(item) is not None
            ]
        else:
            return None
    else:
        return d


class ServerConfig(TypedDict):
    """Exactly the keyword arguments `server.main` accepts.

    `host`, `port` and `path` are None under the stdio transport, where they
    are meaningless. Declaring that here rather than typing them `str`/`int`
    keeps the boundary honest: process_config really does emit None for them,
    and a caller should not have to discover it at runtime.
    """
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    neo4j_database: str
    transport: Literal["stdio", "sse", "streamable-http"]
    namespace: str
    host: str | None
    port: int | None
    path: str | None
    allow_origins: list[str]
    allowed_hosts: list[str]
    read_timeout: int


def process_config(args: argparse.Namespace) -> ServerConfig:
    """
    Process the command line arguments and environment variables to create a config dictionary.
    This may then be used as input to the main server function.
    If any value is not provided, then a warning is logged and a default value is used, if appropriate.

    Parameters
    ----------
    args : argparse.Namespace
        The command line arguments.

    Returns
    -------
    config : dict[str, str]
        The configuration dictionary.
    """

    config: ServerConfig = {}  # type: ignore[typeddict-item]  # every key is assigned below

    # parse uri
    if args.db_url is not None:
        config["neo4j_uri"] = args.db_url
    else:
        _url = os.getenv("NEO4J_URL")
        if _url is not None:
            config["neo4j_uri"] = _url
        else:
            _uri = os.getenv("NEO4J_URI")
            if _uri is not None:
                config["neo4j_uri"] = _uri
            else:
                logger.warning("Warning: No Neo4j connection URL provided. Using default: bolt://localhost:7687")
                config["neo4j_uri"] = "bolt://localhost:7687"

    # parse username
    if args.username is not None:
        config["neo4j_user"] = args.username
    else:
        _username = os.getenv("NEO4J_USERNAME")
        if _username is not None:
            config["neo4j_user"] = _username
        else:
            logger.warning("Warning: No Neo4j username provided. Using default: neo4j")
            config["neo4j_user"] = "neo4j"

    # parse password
    if args.password is not None:
        config["neo4j_password"] = args.password
    else:
        _password = os.getenv("NEO4J_PASSWORD")
        if _password is not None:
            config["neo4j_password"] = _password
        else:
            logger.warning("Warning: No Neo4j password provided. Using default: password")
            config["neo4j_password"] = "password"

    # parse database
    if args.database is not None:
        config["neo4j_database"] = args.database
    else:
        _database = os.getenv("NEO4J_DATABASE")
        if _database is not None:
            config["neo4j_database"] = _database
        else:
            logger.warning("Warning: No Neo4j database provided. Using default: neo4j")
            config["neo4j_database"] = "neo4j"

    # parse transport
    if args.transport is not None:
        config["transport"] = args.transport
    else:
        _transport = os.getenv("NEO4J_TRANSPORT")
        if _transport is not None:
            if _transport not in ("stdio", "sse", "streamable-http"):
                logger.warning(
                    f"Warning: NEO4J_TRANSPORT={_transport!r} is not a recognised transport. "
                    "Using default: stdio")
                _transport = "stdio"
            config["transport"] = _transport
        else:
            logger.warning("Warning: No transport type provided. Using default: stdio")
            config["transport"] = "stdio"

    # parse server host
    if args.server_host is not None:
        if config["transport"] == "stdio":
            logger.warning("Warning: Server host provided, but transport is `stdio`. The `server_host` argument will be set, but ignored.")
        config["host"] = args.server_host
    else:
        _host = os.getenv("NEO4J_MCP_SERVER_HOST")
        if _host is not None:
            if config["transport"] == "stdio":
                logger.warning("Warning: Server host provided, but transport is `stdio`. The `NEO4J_MCP_SERVER_HOST` environment variable will be set, but ignored.")
            config["host"] = _host
        elif config["transport"] != "stdio":
            logger.warning("Warning: No server host provided and transport is not `stdio`. Using default server host: 127.0.0.1")
            config["host"] = "127.0.0.1"
        else:
            logger.info("Info: No server host provided and transport is `stdio`. `server_host` will be None.")
            config["host"] = None

    # parse server port
    if args.server_port is not None:
        if config["transport"] == "stdio":
            logger.warning("Warning: Server port provided, but transport is `stdio`. The `server_port` argument will be set, but ignored.")
        config["port"] = args.server_port
    else:
        _port = os.getenv("NEO4J_MCP_SERVER_PORT")
        if _port is not None:
            if config["transport"] == "stdio":
                logger.warning("Warning: Server port provided, but transport is `stdio`. The `NEO4J_MCP_SERVER_PORT` environment variable will be set, but ignored.")
            config["port"] = int(_port)
        elif config["transport"] != "stdio":
            logger.warning("Warning: No server port provided and transport is not `stdio`. Using default server port: 8000")
            config["port"] = 8000
        else:
            logger.info("Info: No server port provided and transport is `stdio`. `server_port` will be None.")
            config["port"] = None

    # parse server path
    if args.server_path is not None:
        if config["transport"] == "stdio":
            logger.warning("Warning: Server path provided, but transport is `stdio`. The `server_path` argument will be set, but ignored.")
        config["path"] = args.server_path
    else:
        _path = os.getenv("NEO4J_MCP_SERVER_PATH")
        if _path is not None:
            if config["transport"] == "stdio":
                logger.warning("Warning: Server path provided, but transport is `stdio`. The `NEO4J_MCP_SERVER_PATH` environment variable will be set, but ignored.")
            config["path"] = _path
        elif config["transport"] != "stdio":
            logger.warning("Warning: No server path provided and transport is not `stdio`. Using default server path: /mcp/")
            config["path"] = "/mcp/"
        else:
            logger.info("Info: No server path provided and transport is `stdio`. `server_path` will be None.")
            config["path"] = None

    # parse allow origins
    if args.allow_origins is not None:
        # Handle comma-separated string from CLI

        config["allow_origins"] = [origin.strip() for origin in args.allow_origins.split(",") if origin.strip()]

    else:
        _allow_origins = os.getenv("NEO4J_MCP_SERVER_ALLOW_ORIGINS")
        if _allow_origins is not None:
            # split comma-separated string into list
            config["allow_origins"] = [
                origin.strip() for origin in os.getenv("NEO4J_MCP_SERVER_ALLOW_ORIGINS", "").split(",")
                if origin.strip()
            ]
        else:
            logger.info(
                "Info: No allow origins provided. Defaulting to no allowed origins."
            )
            config["allow_origins"] = list()

    # parse allowed hosts for DNS rebinding protection
    if args.allowed_hosts is not None:
        # Handle comma-separated string from CLI
        config["allowed_hosts"] = [host.strip() for host in args.allowed_hosts.split(",") if host.strip()]

    else:
        _allowed_hosts = os.getenv("NEO4J_MCP_SERVER_ALLOWED_HOSTS")
        if _allowed_hosts is not None:
            # split comma-separated string into list
            config["allowed_hosts"] = [
                host.strip() for host in os.getenv("NEO4J_MCP_SERVER_ALLOWED_HOSTS", "").split(",")
                if host.strip()
            ]
        else:
            logger.info(
                "Info: No allowed hosts provided. Defaulting to secure mode - only localhost and 127.0.0.1 allowed."
            )
            config["allowed_hosts"] = ["localhost", "127.0.0.1"]

    # namespace configuration
    if args.namespace is not None:
        logger.info(f"Info: Namespace provided for tools: {args.namespace}")
        config["namespace"] = args.namespace
    else:
        _namespace = os.getenv("NEO4J_NAMESPACE")
        if _namespace is not None:
            logger.info(f"Info: Namespace provided for tools: {os.getenv('NEO4J_NAMESPACE')}")
            config["namespace"] = _namespace
        else:
            logger.info("Info: No namespace provided for tools. No namespace will be used.")
            config["namespace"] = ""

    # parse read timeout
    if args.read_timeout is not None:
        config["read_timeout"] = args.read_timeout
    else:
        _read_timeout = os.getenv("NEO4J_READ_TIMEOUT")
        if _read_timeout is not None:
            config["read_timeout"] = int(_read_timeout)
        else:
            logger.info("Info: No read timeout provided. Using default: 30 seconds")
            config["read_timeout"] = 30

    return config
