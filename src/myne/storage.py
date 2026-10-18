"""SQLite persistence. Each operation owns a connection and transaction."""

import json
import math
import sqlite3
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from myne.errors import MyneError
from myne.models import Track


class Store:
    def __init__(self, database: Path) -> None:
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise MyneError("This database was created by a newer Myne version. Upgrade Myne to open it.")
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS queue (position INTEGER PRIMARY KEY, track TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots (name TEXT PRIMARY KEY, tracks TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY, track TEXT NOT NULL, seconds REAL NOT NULL,
                    completed INTEGER NOT NULL, played_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                PRAGMA user_version=1;
            """)
        database.chmod(0o600)

    @contextmanager
    def _connect(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=10)
        try:
            db.execute("PRAGMA busy_timeout=10000")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _dump(track: Track) -> str:
        return json.dumps(track.to_dict(), ensure_ascii=False)

    @staticmethod
    def _queue(db: sqlite3.Connection) -> list[Track]:
        return [
            Track.from_dict(json.loads(row[0]))
            for row in db.execute("SELECT track FROM queue ORDER BY position")
        ]

    def queue(self) -> list[Track]:
        with self._connect() as db:
            return self._queue(db)

    def enqueue(self, tracks: list[Track], front: bool = False) -> None:
        if not tracks:
            return
        with self._connect(True) as db:
            if front:
                queued = self._queue(db)
                db.execute("DELETE FROM queue")
                db.executemany(
                    "INSERT INTO queue(position,track) VALUES (?,?)",
                    ((i, self._dump(t)) for i, t in enumerate([*tracks, *queued])),
                )
            else:
                start = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM queue").fetchone()[0]
                db.executemany(
                    "INSERT INTO queue(position,track) VALUES (?,?)",
                    ((start + i, self._dump(t)) for i, t in enumerate(tracks)),
                )

    def claim_next(self) -> Track | None:
        with self._connect(True) as db:
            if db.execute("SELECT 1 FROM settings WHERE key='current'").fetchone():
                raise MyneError("A track is already claimed by the player.")
            row = db.execute("SELECT position,track FROM queue ORDER BY position LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM queue WHERE position=?", (row[0],))
            db.execute("INSERT INTO settings(key,value) VALUES ('current',?)", (row[1],))
            return Track.from_dict(json.loads(row[1]))

    def current(self) -> Track | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='current'").fetchone()
            return Track.from_dict(json.loads(row[0])) if row else None

    def recover_current(self) -> None:
        with self._connect(True) as db:
            row = db.execute("SELECT value FROM settings WHERE key='current'").fetchone()
            if row:
                position = db.execute("SELECT COALESCE(MIN(position),0)-1 FROM queue").fetchone()[0]
                db.execute("INSERT INTO queue(position,track) VALUES (?,?)", (position, row[0]))
                db.execute("DELETE FROM settings WHERE key='current'")

    def finish_current(self, seconds: float, completed: bool) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise MyneError("Listening time must be a finite positive number.")
        with self._connect(True) as db:
            row = db.execute("SELECT value FROM settings WHERE key='current'").fetchone()
            if row:
                if seconds > 0:
                    db.execute(
                        "INSERT INTO history(track,seconds,completed) VALUES (?,?,?)",
                        (row[0], seconds, int(completed)),
                    )
                db.execute("DELETE FROM settings WHERE key='current'")

    def clear_queue(self) -> None:
        with self._connect(True) as db:
            db.execute("DELETE FROM queue")

    def remove(self, index: int) -> Track:
        with self._connect(True) as db:
            row = db.execute(
                "SELECT position,track FROM queue ORDER BY position LIMIT 1 OFFSET ?", (max(0, index),)
            ).fetchone()
            if index < 0 or row is None:
                raise MyneError("Queue position is out of range.")
            db.execute("DELETE FROM queue WHERE position=?", (row[0],))
            return Track.from_dict(json.loads(row[1]))

    def save_queue(self, name: str = "default") -> int:
        with self._connect(True) as db:
            tracks = self._queue(db)
            db.execute(
                "INSERT OR REPLACE INTO snapshots(name,tracks) VALUES (?,?)",
                (name, json.dumps([t.to_dict() for t in tracks])),
            )
            return len(tracks)

    def restore_queue(self, name: str = "default") -> int:
        with self._connect(True) as db:
            row = db.execute("SELECT tracks FROM snapshots WHERE name=?", (name,)).fetchone()
            if row is None:
                raise MyneError(f"No saved queue named {name!r}. Run myne queue save first.")
            tracks = [Track.from_dict(t) for t in json.loads(row[0])]
            db.execute("DELETE FROM queue")
            db.executemany(
                "INSERT INTO queue(position,track) VALUES (?,?)",
                ((i, self._dump(t)) for i, t in enumerate(tracks)),
            )
            return len(tracks)

    def snapshots(self) -> list[str]:
        with self._connect() as db:
            return [r[0] for r in db.execute("SELECT name FROM snapshots ORDER BY name")]

    def set_setting(self, key: str, value: Any) -> None:
        with self._connect(True) as db:
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (key, json.dumps(value)))

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def stats(self) -> dict[str, Any]:
        songs: Counter[str] = Counter()
        artists: Counter[str] = Counter()
        genres: Counter[str] = Counter()
        seconds = 0.0
        plays = 0
        with self._connect() as db:
            for raw, listened in db.execute("SELECT track,seconds FROM history"):
                track = Track.from_dict(json.loads(raw))
                seconds += listened
                # A play counts after 30 seconds (or half a shorter track).
                threshold = min(30.0, track.duration / 2) if track.duration and track.duration > 0 else 30.0
                if listened < threshold:
                    continue
                plays += 1
                songs[f"{track.title} — {', '.join(track.artists)}"] += 1
                artists.update(track.artists or ("Unknown artist",))
                genres.update(track.genres)
        return {
            "listening_seconds": seconds,
            "plays": plays,
            "songs": songs.most_common(10),
            "artists": artists.most_common(10),
            "genres": genres.most_common(10),
        }
