FROM gcr.io/kustodian-dev01/openhands:latest

# Install required packages for database migrations
RUN pip install alembic psycopg2-binary cloud-sql-python-connector pg8000 gspread

WORKDIR /app

CMD ["uvicorn", "saas_server:app", "--host", "0.0.0.0", "--port", "3000"]
