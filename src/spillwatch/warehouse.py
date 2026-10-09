"""DuckDB warehouse: schema setup, idempotent loads and the raw parquet landing zone."""

from datetime import datetime

import duckdb
import pandas as pd

from spillwatch.config import RAW_DIR, SQL_DIR, WAREHOUSE_PATH


def connect(path=WAREHOUSE_PATH) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute("SET TimeZone = 'UTC'")
    con.execute((SQL_DIR / "schema.sql").read_text())
    return con


def land_raw(df: pd.DataFrame, source: str, run_at: datetime) -> None:
    """Keep an immutable copy of every pull so the warehouse can always be rebuilt."""
    folder = RAW_DIR / source / f"date={run_at:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    df.to_parquet(folder / f"{source}_{run_at:%Y%m%dT%H%M%S}.parquet", index=False)


def insert_ignore(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> int:
    """Insert rows, skipping any whose primary key already exists. Returns rows added."""
    if df.empty:
        return 0
    before = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    cols = ", ".join(df.columns)
    con.register("incoming", df)
    con.execute(f"INSERT OR IGNORE INTO {table} ({cols}) SELECT {cols} FROM incoming")
    con.unregister("incoming")
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] - before


def replace_table(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> int:
    con.register("incoming", df)
    con.execute(f"DELETE FROM {table}")
    con.execute(f"INSERT INTO {table} ({', '.join(df.columns)}) SELECT {', '.join(df.columns)} FROM incoming")
    con.unregister("incoming")
    return len(df)


def run_models(con: duckdb.DuckDBPyConnection) -> None:
    con.execute((SQL_DIR / "models.sql").read_text())


def log_run(con, job, started_at, rows, status="success", message=None) -> None:
    con.execute(
        "INSERT INTO pipeline_runs VALUES (?, ?, now(), ?, ?, ?)",
        [job, started_at, rows, status, message],
    )
