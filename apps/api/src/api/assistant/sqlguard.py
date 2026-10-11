"""Guard for model-written SQL. Defence in depth: the database is the real wall (role app_ro can
read only the chat.* views, read-only transaction, 5 s statement timeout, RLS on the caller's
blocks and JVs); this check refuses anything else before it gets there.

Accepted: exactly one SELECT (UNION / CTEs allowed) whose every table is a `chat.v_*` view
(or a CTE defined in the same statement), with no denied functions and no literal values in
place of parameters it was told to use. It is wrapped in an outer SELECT with a row limit.
"""

import re

import sqlglot
from sqlglot import exp

ALLOWED_SCHEMA = "chat"
MAX_ROWS = 200
DENIED_FUNCTIONS = {
    "pg_sleep",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "dblink",
    "dblink_exec",
    "set_config",
    "current_setting",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "query_to_xml",
    "copy",
    "txid_current",
    "nextval",
    "setval",
    "pg_advisory_lock",
    "pg_notify",
}
PARAM = re.compile(r"%\((\w+)\)s")


class UnsafeSQL(ValueError):
    pass


def check(sql: str, params: dict[str, object] | None = None) -> str:
    """The SQL wrapped with a row limit, or UnsafeSQL with the reason."""
    params = params or {}
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        raise UnsafeSQL("one statement only")
    if "--" in s or "/*" in s:
        raise UnsafeSQL("no comments")
    names = set(PARAM.findall(s))
    if missing := names - set(params):
        raise UnsafeSQL(f"parameters without values: {sorted(missing)}")
    # sqlglot cannot parse psycopg placeholders: give it typed stand-ins.
    probe = PARAM.sub(lambda m: f"__param_{m.group(1)}", s)
    try:
        trees = sqlglot.parse(probe, read="postgres")
    except sqlglot.errors.ParseError as e:
        raise UnsafeSQL(f"does not parse: {str(e)[:200]}") from e
    if len(trees) != 1 or trees[0] is None:
        raise UnsafeSQL("one statement only")
    tree = trees[0]
    if not isinstance(tree, exp.Select | exp.Union | exp.Intersect | exp.Except):
        raise UnsafeSQL(f"only SELECT is allowed, got {type(tree).__name__}")
    for node in tree.walk():
        if isinstance(
            node,
            exp.Insert
            | exp.Update
            | exp.Delete
            | exp.Create
            | exp.Drop
            | exp.Alter
            | exp.Command
            | exp.Merge
            | exp.Into,
        ):
            raise UnsafeSQL(f"{type(node).__name__} is not allowed")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        name, schema = t.name.lower(), (t.db or "").lower()
        if not schema and name in ctes:
            continue
        if schema != ALLOWED_SCHEMA or not name.startswith("v_"):
            raise UnsafeSQL(f"only chat.v_* views may be read, not {t.sql()}")
    for f in tree.find_all(exp.Func):
        fname = (f.sql_name() if not isinstance(f, exp.Anonymous) else f.name).lower()
        if fname in DENIED_FUNCTIONS:
            raise UnsafeSQL(f"function {fname} is not allowed")
    return f"SELECT * FROM ({s}) AS q LIMIT {MAX_ROWS}"
