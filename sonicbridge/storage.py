"""Durable sender checkpoint, receiver chunks, and inbox."""
from contextlib import contextmanager
import json
import sqlite3
from time import time


class Store:
    def __init__(self, path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = path
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS transfers (
                    id TEXT PRIMARY KEY, total INTEGER, seen REAL, complete INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT REFERENCES transfers(id) ON DELETE CASCADE,
                    position INTEGER, payload BLOB, PRIMARY KEY (id, position)
                );
                CREATE TABLE IF NOT EXISTS checkpoint (
                    slot INTEGER PRIMARY KEY CHECK(slot = 1), transfer TEXT, size INTEGER,
                    payload BLOB, options TEXT
                );
                CREATE TABLE IF NOT EXISTS inbox (
                    id TEXT PRIMARY KEY, name TEXT, data BLOB, sealed INTEGER
                );
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                yield db
        finally:
            db.close()

    def load(self, db, transfer):
        key = str(transfer)
        row = db.execute("SELECT total, complete FROM transfers WHERE id = ?", (key,)).fetchone()
        if row is None:
            return None
        chunks = dict(db.execute("SELECT position, payload FROM chunks WHERE id = ?", (key,)))
        return row[0], bool(row[1]), chunks

    def save_chunk(self, db, packet):
        key = str(packet.transfer)
        now = time()
        db.execute("INSERT INTO transfers (id,total,seen) VALUES (?,?,?) "
                   "ON CONFLICT(id) DO UPDATE SET seen=excluded.seen", (key, packet.total, now))
        db.execute("INSERT OR IGNORE INTO chunks VALUES (?,?,?)", (key, packet.index, packet.payload))
        db.execute("DELETE FROM transfers WHERE seen < ? OR id IN "
                   "(SELECT id FROM transfers ORDER BY seen DESC LIMIT -1 OFFSET 256)", (now - 7 * 86400,))

    def finish(self, db, packet, message, sealed):
        key = str(packet.transfer)
        db.execute("UPDATE transfers SET complete=1 WHERE id=?", (key,))
        db.execute("INSERT OR IGNORE INTO inbox VALUES (?,?,?,?)", (key, message.name, message.data, sealed))
        db.execute("DELETE FROM inbox WHERE rowid NOT IN (SELECT rowid FROM inbox ORDER BY rowid DESC LIMIT 20)")

    def messages(self):
        with self.connection() as db:
            return list(db.execute("SELECT name,data,sealed FROM inbox ORDER BY rowid"))

    def pending(self):
        with self.connection() as db:
            row = db.execute("SELECT transfer,size,payload,options FROM checkpoint WHERE slot=1").fetchone()
        return (int(row[0]), row[1], row[2], json.loads(row[3])) if row else None

    def save_pending(self, transfer, size, payload, options):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO checkpoint VALUES (1,?,?,?,?)",
                       (str(transfer), size, payload, json.dumps(options)))

    def clear_pending(self):
        with self.connection() as db:
            db.execute("DELETE FROM checkpoint WHERE slot=1")
