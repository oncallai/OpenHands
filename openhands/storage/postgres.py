import os

import psycopg2
import psycopg2.extras

from openhands.storage.db import DBStore


class PostgresStore(DBStore):
    def __init__(self, table: str | None = None) -> None:
        self.host = os.environ.get('SUPABASE_HOST')
        self.port = os.environ.get('SUPABASE_PORT', '5432')
        self.user = os.environ.get('SUPABASE_USER')
        self.password = os.environ.get('SUPABASE_PASSWORD')
        self.dbname = os.environ.get('SUPABASE_DBNAME')
        self.table = table or os.environ.get('SUPABASE_TABLE')
        self.conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            dbname=self.dbname,
        )
        self.conn.autocommit = True

    def write(self, path: str, contents: str | bytes) -> None:
        # Upsert by 'id' (path)
        table = self.table
        query = f'INSERT INTO {table} (id, contents) VALUES (%s, %s) '
        query += 'ON CONFLICT (id) DO UPDATE SET contents = EXCLUDED.contents'
        with self.conn.cursor() as cur:
            cur.execute(query, (path, contents))

    def read(self, path: str) -> str:
        table = self.table
        query = f'SELECT contents FROM {table} WHERE id = %s'
        with self.conn.cursor() as cur:
            cur.execute(query, (path,))
            row = cur.fetchone()
            if not row:
                raise FileNotFoundError(f'No record found for id={path}')
            return row[0]

    def list(self, path: str) -> list[str]:
        # List all ids that start with 'path' as prefix
        table = self.table
        prefix = path.rstrip('/')
        query = f'SELECT id FROM {table} WHERE id LIKE %s'
        like_pattern = prefix + '%' if prefix else '%'
        with self.conn.cursor() as cur:
            cur.execute(query, (like_pattern,))
            return [row[0] for row in cur.fetchall()]

    def delete(self, path: str) -> None:
        table = self.table
        query = f'DELETE FROM {table} WHERE id = %s'
        with self.conn.cursor() as cur:
            cur.execute(query, (path,))
