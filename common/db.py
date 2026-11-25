"""Postgres connection pool shared by the API and the worker."""
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from common.config import settings

_pool: ConnectionPool | None = None


def conninfo() -> str:
    s = settings()
    return (f"host={s.db_host} port={s.db_port} dbname={s.db_name} "
            f"user={s.db_user} password={s.db_password}")


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(conninfo(), min_size=1, max_size=10,
                               kwargs={"row_factory": dict_row}, open=True)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
