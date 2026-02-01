"""
database.py
───────────
Pure database layer.  Owns the SQLAlchemy engine, thread-safe
connection handling, and every query/write the app needs.

Nothing in here knows about S3, file I/O, or watermark detection.
"""

from __future__ import annotations

import os
from threading import Lock
from uuid import uuid4

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()


class Database:
    """
    Thread-safe SQL Server wrapper.

    Responsibilities
    ────────────────
    • Engine creation & connection pooling
    • Raw SQL execution
    • DataFrame ↔ table read/write
    • Upsert (insert-new-only via staging table + MERGE)
    • Schema helpers (create_table_if_not_exists)
    • Domain shortcut (update_all_final_tags)
    """

    def __init__(self) -> None:
        self._user   = os.getenv("DB_USER")
        self._password = os.getenv("DB_PASSWORD")
        self._host   = os.getenv("DB_HOST")
        self._port   = os.getenv("DB_PORT")
        self._db     = "parts_db"
        self._driver = "ODBC+Driver+18+for+SQL+Server"
        self._engine = self._build_engine()
        self._lock   = Lock()

    # ── engine ──────────────────────────────────────────────

    def _build_engine(self):
        url = (
            f"mssql+pyodbc://{self._user}:{self._password}"
            f"@{self._host}:{self._port}/{self._db}"
            f"?driver={self._driver}&TrustServerCertificate=yes"
        )
        return create_engine(url, pool_pre_ping=True, fast_executemany=True)

    # ── core query / write ──────────────────────────────────

    def execute_sql(self, sql_text: str):
        """Execute any SQL statement."""
        with self._lock, self._engine.begin() as conn:
            return conn.execute(text(sql_text))

    def read_sql_query(self, sql_text: str) -> pd.DataFrame:
        """Execute a SELECT and return the result as a DataFrame."""
        with self._lock, self._engine.begin() as conn:
            return pd.read_sql_query(text(sql_text), conn)

    def to_sql(
        self,
        df: pd.DataFrame,
        table_name: str,
        if_exists: str = "append",
        index: bool = False,
        schema: str | None = None,
    ) -> None:
        """Bulk-write a DataFrame into a table."""
        with self._lock, self._engine.begin() as conn:
            df.to_sql(
                table_name,
                conn,
                if_exists=if_exists,
                index=index,
                method="multi",
                schema=schema,
                chunksize=20_000,
            )

    # ── schema helpers ──────────────────────────────────────

    def create_table_if_not_exists(self, table_name: str, df: pd.DataFrame) -> None:
        """
        Create *table_name* with a schema inferred from *df*.
        Does nothing if the table already exists.
        """
        exists_query = (
            f"IF OBJECT_ID('{table_name}', 'U') IS NOT NULL "
            "SELECT 1 ELSE SELECT 0;"
        )
        if self.read_sql_query(exists_query).iloc[0, 0] == 1:
            print(f"Table {table_name} already exists.")
            return

        col_defs = ", ".join(
            f"[{col}] {self._pandas_dtype_to_sql(df[col].dtype)}"
            for col in df.columns
        )
        self.execute_sql(f"CREATE TABLE [{table_name}] ({col_defs});")
        print(f"Table {table_name} created successfully.")

    # ── upsert ──────────────────────────────────────────────

    def upsert_append_new_only(
        self,
        df: pd.DataFrame,
        target: str = "dbo.parts",
        key_col: str = "number",
    ) -> None:
        """
        Bulk-load *df* into a staging table, MERGE into *target*
        (insert only rows whose *key_col* doesn't exist yet), then
        drop the stage.  Runs inside a single transaction.
        """
        if df.empty:
            return

        schema, tgt_name = target.split(".", 1)
        stage_name  = f"{tgt_name}_stage_{uuid4().hex[:8]}"
        stage_full  = f"{schema}.{stage_name}"

        cols         = list(df.columns)
        col_csv      = ", ".join(f"[{c}]" for c in cols)
        src_cols_csv = ", ".join(f"src.[{c}]" for c in cols)

        try:
            with self._lock, self._engine.begin() as conn:
                conn.execute(text(f"SELECT TOP 0 * INTO {stage_full} FROM {target};"))
                df.to_sql(
                    name=stage_name,
                    con=conn,
                    schema=schema,
                    if_exists="append",
                    index=False,
                    method="multi",
                    chunksize=1000,
                )
                conn.execute(text(f"""
                    MERGE {target} AS tgt
                    USING (SELECT DISTINCT {col_csv} FROM {stage_full}) AS src
                    ON  tgt.[{key_col}] = src.[{key_col}]
                    WHEN NOT MATCHED BY TARGET THEN
                        INSERT ({col_csv}) VALUES ({src_cols_csv});
                """))
        finally:
            with self._engine.begin() as conn:
                conn.execute(text(
                    f"IF OBJECT_ID('{stage_full}', 'U') IS NOT NULL "
                    f"DROP TABLE {stage_full};"
                ))

    # ── domain helpers ──────────────────────────────────────

    def update_all_final_tags(self) -> None:
        """
        For every part that has no final_tag yet, set it to the
        lexicographically smallest tag_value from part_tags.
        """
        self.execute_sql("""
            WITH s AS (
                SELECT part_id, MIN(tag_value) AS tag_value
                FROM   dbo.part_tags
                GROUP BY part_id
            )
            UPDATE p
            SET    p.final_tag = s.tag_value
            FROM   dbo.parts AS p
            INNER JOIN s ON s.part_id = p.part_id
            WHERE  p.final_tag IS NULL;
        """)

    # ── private helpers ─────────────────────────────────────

    @staticmethod
    def _pandas_dtype_to_sql(dtype) -> str:
        if pd.api.types.is_integer_dtype(dtype):
            return "INT"
        if pd.api.types.is_float_dtype(dtype):
            return "FLOAT"
        if str(dtype) == "datetime64[ns]":
            return "DATETIME2"
        return "NVARCHAR(MAX)"