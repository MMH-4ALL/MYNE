from concurrent.futures import ThreadPoolExecutor

import pytest

from myne.errors import MyneError
from myne.models import Playlist, Track
from myne.storage import Store


def song(i: int) -> Track:
    return Track(str(i), f"Song {i}", ("Artist",), 120, genres=("Rock",))


def test_imported_playlists_persist_without_replacing_queue_names_or_current(tmp_path):
    store = Store(tmp_path / "db")
    store.enqueue([song(1), song(2)])
    store.claim_next()
    store.save_queue("Night")
    imported = Playlist("Night", (song(3), song(3), song(4)), "Spotify",
                        "https://open.spotify.com/playlist/" + "A" * 22, ("Public view may be partial.",))
    saved = store.import_playlist(imported)
    assert saved.name == "Night (2)"
    assert store.playlist("Night").tracks == (song(2),)
    assert store.current() == song(1)
    assert store.queue() == [song(2)]
    assert Store(store.database).playlist("Night (2)") == saved
    assert store.import_playlist(imported).name == "Night (3)"
    store.save_queue("Night (2)")
    assert store.playlist("Night (2)").source == "Saved queue"
    assert store.playlist("Night (2)").source_url is None


def test_concurrent_playlist_imports_keep_all_collections(tmp_path):
    store = Store(tmp_path / "db")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.import_playlist(Playlist("Shared", (song(1),))), range(8)))
    assert len({p.name for p in results}) == 8
    assert len(store.playlists()) == 8


def test_queue_survives_restart_and_recovers_interrupted_current(tmp_path):
    path = tmp_path / "myne.db"
    store = Store(path)
    store.enqueue([song(1), song(2)])
    assert store.claim_next() == song(1)
    restarted = Store(path)
    assert restarted.current() == song(1)
    restarted.recover_current()
    restarted.recover_current()  # idempotent recovery
    assert restarted.queue() == [song(1), song(2)]
    assert restarted.current() is None


def test_named_snapshot_is_atomic_and_excludes_current(tmp_path):
    store = Store(tmp_path / "db")
    store.enqueue([song(1), song(2)])
    store.claim_next()
    assert store.save_queue("evening") == 1
    store.clear_queue()
    store.enqueue([song(3)])
    assert store.restore_queue("evening") == 1
    assert store.queue() == [song(2)]
    assert store.current() == song(1)
    assert store.snapshots() == ["evening"]
    with pytest.raises(MyneError):
        store.restore_queue("missing")
    assert store.queue() == [song(2)]


def test_statistics_count_threshold_and_record_actual_segments(tmp_path):
    store = Store(tmp_path / "db")
    store.enqueue([song(1), song(2), Track("short", "Short", ("Other",), 10)])
    store.claim_next()
    store.finish_current(29, False)
    store.claim_next()
    store.finish_current(40, False)
    store.claim_next()
    store.finish_current(5, True)
    stats = store.stats()
    assert stats["listening_seconds"] == 74
    assert stats["plays"] == 2
    assert stats["artists"] == [("Artist", 1), ("Other", 1)]
    assert stats["genres"] == [("Rock", 1)]
    store.finish_current(20, True)
    assert store.stats() == stats  # no double accounting
    for bad in (-1, float("inf"), float("nan")):
        with pytest.raises(MyneError):
            store.finish_current(bad, True)


def test_concurrent_enqueues_do_not_lose_tracks(tmp_path):
    store = Store(tmp_path / "db")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: Store(store.database).enqueue([song(i)]), range(40)))
    assert len(store.queue()) == 40
    assert {t.id for t in store.queue()} == {str(i) for i in range(40)}
    assert store.remove(0).id not in {t.id for t in store.queue()}
    for index in (-1, 100):
        with pytest.raises(MyneError):
            store.remove(index)


def test_cannot_claim_two_current_tracks(tmp_path):
    store = Store(tmp_path / "db")
    store.enqueue([song(1), song(2)])
    store.claim_next()
    with pytest.raises(MyneError):
        store.claim_next()
    assert store.queue() == [song(2)]


def test_newer_database_schema_is_rejected_without_mutation(tmp_path):
    import sqlite3

    path = tmp_path / "future.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(MyneError, match="newer Myne version"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99


def test_favorites_persist_and_distinguish_providers(tmp_path):
    from dataclasses import replace

    store = Store(tmp_path / "db")
    first = song(1)
    other = replace(first, provider="local")
    assert store.toggle_favorite(first) is True
    assert store.toggle_favorite(other) is True
    restarted = Store(store.database)
    assert restarted.favorites() == [other, first]
    assert restarted.toggle_favorite(first) is False
    assert store.favorites() == [other]


def test_recent_tracks_use_committed_listening_and_latest_unique_order(tmp_path):
    store = Store(tmp_path / "db")
    store.enqueue([song(1), song(2), song(1)])
    assert store.recent_tracks() == []
    for _ in range(3):
        store.claim_next()
        store.finish_current(15, False)
    assert store.recent_tracks() == [song(1), song(2)]
    assert store.stats()["listening_seconds"] == 45


def test_queue_editing_preserves_duplicates_current_and_stale_guard(tmp_path, monkeypatch):
    store = Store(tmp_path / "db")
    store.enqueue([song(0), song(1), song(2), song(1)])
    store.claim_next()
    assert store.move(1, -1, song(2)) == 0
    assert store.queue() == [song(2), song(1), song(1)]
    with pytest.raises(MyneError, match="queue changed"):
        store.remove(0, song(1))
    with pytest.raises(MyneError, match="queue changed"):
        store.move(0, 1, song(1))
    assert store.current() == song(0)
    monkeypatch.setattr("myne.storage.random.SystemRandom.shuffle", lambda _self, tracks: tracks.reverse())
    assert store.shuffle_queue() == 3
    assert store.queue() == [song(1), song(1), song(2)]
    assert store.remove(0, song(1)) == song(1)
    assert store.current() == song(0)
    assert store.save_queue("Evening") == 2
    assert store.playlists() == [("Evening", 2)]
