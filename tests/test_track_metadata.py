import pytest

from myne.models import Track


@pytest.mark.parametrize("invalid", [
    {"id": None}, {"title": None}, {"id": True}, {"title": "  "},
    {"artists": "Artist"}, {"artists": [None]}, {"genres": {"Rock": 1}},
    {"album": 7}, {"provider": None}, {"duration": True},
    {"duration": float("nan")}, {"duration": float("inf")}, {"duration": -1},
])
def test_invalid_metadata_is_rejected_instead_of_poisoning_the_queue(invalid):
    with pytest.raises((TypeError, ValueError)):
        Track.from_dict({"id": "song", "title": "Song", **invalid})


def test_saved_track_metadata_round_trips():
    track = Track("song", "أغنية", ("Artist",), 0, "Album", genres=("Rock",))
    assert Track.from_dict(track.to_dict()) == track
