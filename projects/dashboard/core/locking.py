"""Transaction-scoped locks for shared resources without a natural row yet."""
from contextlib import contextmanager
import hashlib

from django.db import connections, transaction


@contextmanager
def serialized_resource(namespace, identity, *, using="default"):
    """Serialize the same resource across workers; release on commit/rollback.

    SQLite development mode already serializes writes. Production PostgreSQL
    needs a stable key (Python's hash is different in each worker).
    """
    with transaction.atomic(using=using):
        connection = connections[using]
        if connection.vendor == "postgresql":
            digest = hashlib.sha256(f"worms:{namespace}:{identity}".encode()).digest()
            key = int.from_bytes(digest[:8], "big", signed=True)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])
        yield
