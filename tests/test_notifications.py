"""
tests/test_notifications.py — Mixtape

Regression tests for notification creation logic, specifically covering
Issue #4: a friend rating a song did not notify the song's original sharer,
unlike adding that song to a playlist (which did).
"""

import pytest
from app import create_app, db
from models import User, Song
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed_song(app):
    """Create a sharer, a friend, and a song shared by the sharer."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        friend = User(username="friend", email="friend@example.com")
        db.session.add_all([sharer, friend])
        db.session.flush()

        song = Song(
            title="Test Track", artist="Test Artist", shared_by=sharer.id
        )
        db.session.add(song)
        db.session.commit()

        yield {"sharer": sharer, "friend": friend, "song": song}


def test_rating_a_friends_song_notifies_the_sharer(app, seed_song):
    """
    A friend rating a shared song should notify the original sharer.

    This is the core regression test for Issue #4: rate_song() previously
    never called create_notification(), so the sharer's notification count
    never changed after a friend rated their song.
    """
    with app.app_context():
        sharer_id = seed_song["sharer"].id
        friend_id = seed_song["friend"].id
        song_id = seed_song["song"].id

        before = get_notifications(sharer_id)
        assert len(before) == 0

        rate_song(friend_id, song_id, 5)

        after = get_notifications(sharer_id)
        assert len(after) == 1
        assert after[0]["type"] == "song_rated"


def test_rating_your_own_song_does_not_notify_yourself(app, seed_song):
    """A user rating their own song should not generate a notification."""
    with app.app_context():
        sharer_id = seed_song["sharer"].id
        song_id = seed_song["song"].id

        rate_song(sharer_id, song_id, 4)

        notifications = get_notifications(sharer_id)
        assert len(notifications) == 0


def test_updating_an_existing_rating_does_not_notify_again(app, seed_song):
    """
    Changing an already-submitted rating should not create a second
    notification — only the first rating should notify the sharer.
    """
    with app.app_context():
        sharer_id = seed_song["sharer"].id
        friend_id = seed_song["friend"].id
        song_id = seed_song["song"].id

        rate_song(friend_id, song_id, 3)
        assert len(get_notifications(sharer_id)) == 1

        rate_song(friend_id, song_id, 5)  # update, not a new rating
        assert len(get_notifications(sharer_id)) == 1  # still just 1
