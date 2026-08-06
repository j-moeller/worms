from flask import Flask
import psycopg2
import random
import string
import os

app = Flask(__name__)

DB_CONFIG = {
    "host": os.environ["DB_HOST"],
    "database": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"]
}

def get_random_text(length=20):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length)) + "_scoring"

def insert_payload(payload):
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO data (payload) VALUES (%s)",
            (payload,)
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()

@app.route("/")
def hello():
    return "Hello from Flask!"

@app.route("/insert")
def insert():
    text = get_random_text()
    insert_payload(text)
    return f"Inserted payload: {text}"