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

    # Event-specific methods for openhands_events table
    def write_event(self, session_id: str, event_index: int, event_data) -> None:
        query = """
            INSERT INTO openhands_events (session_id, event_index, event_data)
            VALUES (%s, %s, %s)
            ON CONFLICT (session_id, event_index) DO UPDATE SET event_data = EXCLUDED.event_data
        """
        with self.conn.cursor() as cur:
            cur.execute(
                query, (session_id, event_index, psycopg2.extras.Json(event_data))
            )
            self.conn.commit()

    def read_event(self, session_id: str, event_index: int):
        query = """
            SELECT event_data FROM openhands_events WHERE session_id = %s AND event_index = %s
        """
        with self.conn.cursor() as cur:
            cur.execute(query, (session_id, event_index))
            row = cur.fetchone()
            if not row:
                raise FileNotFoundError(
                    f'No event found for session_id={session_id} event_index={event_index}'
                )
            return row[0]

    def list_events(self, session_id: str):
        query = """
            SELECT event_index FROM openhands_events WHERE session_id = %s ORDER BY event_index ASC
        """
        with self.conn.cursor() as cur:
            cur.execute(query, (session_id,))
            return [row[0] for row in cur.fetchall()]

    def delete_event(self, session_id: str, event_index: int) -> None:
        query = """
            DELETE FROM openhands_events WHERE session_id = %s AND event_index = %s
        """
        with self.conn.cursor() as cur:
            cur.execute(query, (session_id, event_index))
            self.conn.commit()
