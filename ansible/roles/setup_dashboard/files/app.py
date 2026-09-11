from flask import Flask, jsonify
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor
import os

app = Flask(__name__)

DB_CONFIG = {
    "host": os.environ["DB_HOST"],
    "database": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

pool = ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    **DB_CONFIG,
)

VALID_TABLES =  [
    "teams",
    "vms",
    "students",
    "submissions",
    "logging"
]

def fetch_table(cursor, table_name):
    assert table_name in VALID_TABLES
    cursor.execute(f"SELECT * FROM {table_name} ORDER BY id;")
    return cursor.fetchall()

@app.route("/upload", methods=["PUT"])
def upload():
    pass

@app.route("/", methods=["GET"])
def index():
    conn = pool.getconn()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            data = {
                "teams": fetch_table(cur, "teams"),
                "vms": fetch_table(cur, "vms"),
                "students": fetch_table(cur, "students"),
                "submissions": fetch_table(cur, "submissions"),
                "logging": fetch_table(cur, "logging"),
            }

        return jsonify({
            "status": "success",
            "data": data
        }), 200

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

    finally:
        pool.putconn(conn)