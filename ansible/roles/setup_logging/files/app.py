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


def make_submission(team_api_token, log_data):
    team_token_hash = hashlib.sha256(team_api_token.encode()).hexdigest()

    conn = pool.getconn()

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO logging_attempts (
                        team_token_hash,
                        log_data
                    )
                    VALUES (%s, %s)
                    """,
                    (
                        team_token_hash,
                        log_data,
                    ),
                )

    finally:
        pool.putconn(conn)


# TODO:
# API: team_api_token: str, log_data: Dict
@app.route("/submit", methods=["POST"])
def submit():
    # Flask will normally return 405 for non-POST requests because of
    # methods=["POST"], but keep this check for an explicit JSON response.
    if request.method != "POST":
        return (
            jsonify({"status": "error", "message": "Only POST requests are allowed."}),
            405,
        )

    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form.to_dict()

    team_api_token = data.get("team_api_token")
    log_data = data.get("log_data")

    # TODO:
    # Limit size of log_data
    # Limit size of team_api_token
    # TODO: Verify team_api_token = str and log = dict

    missing = []
    if not team_api_token:
        missing.append("team_api_token")
    if not log_data:
        missing.append("log_data")

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
        make_submission(team_api_token, log_data)

    except Exception:
        app.logger.exception("Failed to insert logging record")

        return jsonify({"status": "error", "message": "Internal server error."}), 500

    return (
        jsonify({"status": "success", "message": "Log entry stored successfully."}),
        201,
    )
