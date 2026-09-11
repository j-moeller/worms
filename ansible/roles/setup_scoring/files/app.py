import hashlib

from flask import Flask, request, jsonify
from psycopg2.pool import ThreadedConnectionPool
from psycopg2 import errors
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


def make_submission(team_api_token, vm_flag):
    team_token_hash = hashlib.sha256(team_api_token.encode()).hexdigest()
    vm_flag_hash = hashlib.sha256(vm_flag.encode()).hexdigest()

    conn = pool.getconn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO flag_capture_attempts (team_token_hash, vm_flag_hash)
                    VALUES (%s, %s)
                    """,
                    (team_token_hash, vm_flag_hash),
                )
    finally:
        pool.putconn(conn)


# TODO:
# API: team_api_token: str, vm_flag: str
@app.route("/submit", methods=["POST"])
def submit():
    # Flask will normally return 405 for non-POST requests because of
    # methods=["POST"], but keep this check for an explicit JSON response.
    if request.method != "POST":
        return (
            jsonify({"status": "error", "message": "Only POST requests are allowed."}),
            405,
        )

    # Accept either JSON or form-encoded data.
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form

    team_api_token = data.get("team_api_token")
    vm_flag = data.get("vm_flag")

    # TODO: Verify team_api_token = str and vm_flag = str

    missing = []
    if not team_api_token:
        missing.append("team_api_token")
    if not vm_flag:
        missing.append("vm_flag")

    if missing:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Missing required parameter(s).",
                    "missing": missing,
                }
            ),
            400,
        )

    try:
        make_submission(team_api_token, vm_flag)

    except Exception as e:
        app.logger.exception("Failed to insert submission")

        return jsonify({"status": "error", "message": "Internal server error."}), 500

    return (
        jsonify({"status": "success", "message": "Submission stored successfully."}),
        201,
    )
