import os
import psycopg2
import logging
from dotenv import load_dotenv  # Automatically load .env file

load_dotenv()

MIGRATION_FILE = os.path.join(os.path.dirname(__file__), '001_create_tables.sql')

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

def get_conn():
    return psycopg2.connect(
        host=os.environ['SUPABASE_HOST'],
        port=os.environ.get('SUPABASE_PORT', '5432'),
        user=os.environ['SUPABASE_USER'],
        password=os.environ['SUPABASE_PASSWORD'],
        dbname=os.environ['SUPABASE_DBNAME'],
    )

def run_migration():
    if not os.path.exists(MIGRATION_FILE):
        logging.error(f'Migration file not found: {MIGRATION_FILE}')
        return
    with open(MIGRATION_FILE, 'r') as f:
        sql = f.read()
    # Split SQL on semicolons, ignore empty statements
    statements = [stmt.strip() for stmt in sql.split(';') if stmt.strip()]
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                for statement in statements:
                    try:
                        cur.execute(statement)
                        logging.info('Executed: %s', statement.splitlines()[0].strip())
                    except Exception as e:
                        logging.warning('Statement failed (possibly already exists or not idempotent): %s\n%s', statement, e)
            conn.commit()
        logging.info('Migration applied successfully.')
    except Exception as e:
        logging.error(f'Migration failed: {e}')

if __name__ == '__main__':
    run_migration()
