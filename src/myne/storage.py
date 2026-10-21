"""SQLite persistence. Each operation owns a connection and transaction."""

import json
import math
import random
import sqlite3
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from myne.errors import MyneError
from myne.models import Playlist, Track


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
                CREATE TABLE IF NOT EXISTS playlist_sources (
                    name TEXT PRIMARY KEY, metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS favorites (
                    provider TEXT NOT NULL, id TEXT NOT NULL, track TEXT NOT NULL,
                    PRIMARY KEY(provider,id)
                );
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

    def remove(self, index: int, expected: Track | None = None) -> Track:
        with self._connect(True) as db:
            row = db.execute(
                "SELECT position,track FROM queue ORDER BY position LIMIT 1 OFFSET ?", (max(0, index),)
            ).fetchone()
            if index < 0 or row is None:
                raise MyneError("Queue position is out of range.")
            if expected is not None and Track.from_dict(json.loads(row[1])) != expected:
                raise MyneError("The queue changed. Refresh it and select the track again.")
            db.execute("DELETE FROM queue WHERE position=?", (row[0],))
            return Track.from_dict(json.loads(row[1]))

    @staticmethod
    def _replace_queue(db: sqlite3.Connection, tracks: list[Track]) -> None:
        db.execute("DELETE FROM queue")
        db.executemany(
            "INSERT INTO queue(position,track) VALUES (?,?)",
            ((index, Store._dump(track)) for index, track in enumerate(tracks)),
        )

    def move(self, index: int, step: int, expected: Track) -> int:
        if step not in (-1, 1):
            raise MyneError("Move a queued track one place up or down.")
        with self._connect(True) as db:
            tracks = self._queue(db)
            if not 0 <= index < len(tracks) or tracks[index] != expected:
                raise MyneError("The queue changed. Refresh it and select the track again.")
            target = max(0, min(len(tracks) - 1, index + step))
            tracks[index], tracks[target] = tracks[target], tracks[index]
            self._replace_queue(db, tracks)
            return target

    def shuffle_queue(self) -> int:
        with self._connect(True) as db:
            tracks = self._queue(db)
            random.SystemRandom().shuffle(tracks)
            self._replace_queue(db, tracks)
            return len(tracks)

    def skip_to(self, index: int, expected: Track) -> None:
        """Select an existing queue entry without copying its remaining tracks.

        Validate the displayed selection in the same transaction as the trim,
        so another client cannot silently make us start a different song.
        """
        with self._connect(True) as db:
            row = db.execute(
                "SELECT position,track FROM queue ORDER BY position LIMIT 1 OFFSET ?", (index,)
            ).fetchone()
            if index < 0 or row is None or Track.from_dict(json.loads(row[1])) != expected:
                raise MyneError("The queue changed. Refresh it and select the track again.")
            db.execute("DELETE FROM queue WHERE position<?", (row[0],))

    def save_queue(self, name: str = "default") -> int:
        with self._connect(True) as db:
            tracks = self._queue(db)
            db.execute(
                "INSERT OR REPLACE INTO snapshots(name,tracks) VALUES (?,?)",
                (name, json.dumps([t.to_dict() for t in tracks])),
            )
            db.execute("DELETE FROM playlist_sources WHERE name=?", (name,))
            return len(tracks)

    def import_playlist(self, playlist: Playlist) -> Playlist:
        """Save a new collection atomically, without replacing names or the queue."""
        base = playlist.name.strip()[:80] or "Imported playlist"
        with self._connect(True) as db:
            name, suffix = base, 2
            while db.execute("SELECT 1 FROM snapshots WHERE name=?", (name,)).fetchone():
                ending = f" ({suffix})"
                name = base[:80 - len(ending)] + ending
                suffix += 1
            db.execute("INSERT INTO snapshots(name,tracks) VALUES (?,?)",
                       (name, json.dumps([t.to_dict() for t in playlist.tracks], ensure_ascii=False)))
            db.execute("INSERT INTO playlist_sources(name,metadata) VALUES (?,?)", (name, json.dumps({
                "source": playlist.source, "source_url": playlist.source_url,
                "warnings": playlist.warnings,
            }, ensure_ascii=False)))
        from dataclasses import replace
        return replace(playlist, name=name)

    def playlist(self, name: str) -> Playlist:
        with self._connect() as db:
            row = db.execute("SELECT tracks FROM snapshots WHERE name=?", (name,)).fetchone()
            if row is None:
                raise MyneError("This playlist no longer exists. Refresh your library.")
            source = db.execute("SELECT metadata FROM playlist_sources WHERE name=?", (name,)).fetchone()
            metadata = json.loads(source[0]) if source else {}
            return Playlist(name, tuple(Track.from_dict(t) for t in json.loads(row[0])),
                            metadata.get("source", "Saved queue"), metadata.get("source_url"),
                            tuple(metadata.get("warnings") or ()))

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

    def playlists(self) -> list[tuple[str, int]]:
        with self._connect() as db:
            return [
                (name, len(json.loads(tracks)))
                for name, tracks in db.execute("SELECT name,tracks FROM snapshots ORDER BY name")
            ]

    def favorites(self) -> list[Track]:
        with self._connect() as db:
            return [Track.from_dict(json.loads(row[0])) for row in db.execute(
                "SELECT track FROM favorites ORDER BY rowid DESC"
            )]

    def toggle_favorite(self, track: Track) -> bool:
        with self._connect(True) as db:
            key = (track.provider, track.id)
            if db.execute("SELECT 1 FROM favorites WHERE provider=? AND id=?", key).fetchone():
                db.execute("DELETE FROM favorites WHERE provider=? AND id=?", key)
                return False
            db.execute("INSERT INTO favorites(provider,id,track) VALUES (?,?,?)", (*key, self._dump(track)))
            return True

    def recent_tracks(self, limit: int = 50) -> list[Track]:
        seen: set[tuple[str, str]] = set()
        result: list[Track] = []
        with self._connect() as db:
            for (raw,) in db.execute("SELECT track FROM history ORDER BY id DESC LIMIT 1000"):
                track = Track.from_dict(json.loads(raw))
                key = (track.provider, track.id)
                if key not in seen:
                    seen.add(key)
                    result.append(track)
                    if len(result) >= limit:
                        break
        return result

    def set_setting(self, key: str, value: Any) -> None:
        self.set_settings({key: value})

    def set_settings(self, values: dict[str, Any]) -> None:
        rows = [(key, json.dumps(value, allow_nan=False)) for key, value in values.items()]
        with self._connect(True) as db:
            db.executemany("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", rows)

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
