# Mixtape — Submission

## Codebase Map

### Main files and their roles

| File | Role |
|---|---|
| `app.py` | Flask application factory. Configures the DB URI (SQLite by default via `DATABASE_URL` env var), registers all four blueprints (`songs`, `playlists`, `users`, `feed`), and creates tables on startup. |
| `models.py` | SQLAlchemy models: `User`, `Song`, `Tag`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`, plus three association tables (`friendships`, `song_tags`, `playlist_entries`). `playlist_entries` carries extra columns (`position`, `added_by`, `added_at`) beyond a plain many-to-many link. |
| `routes/songs.py` | Search, song detail, rating, and listening-event endpoints. Delegates to `search_service`, `notification_service` (for rating), and `streak_service` (for listen events). No route here creates a `Song` — song creation isn't exposed via any route in this repo; songs only exist via `seed_data.py`. |
| `routes/playlists.py` | Create playlist, get playlist metadata, get playlist songs, add song to playlist. The "add song" route delegates to `notification_service.add_to_playlist`, not `playlist_service` — the mutation logic lives in a different file than the name would suggest. |
| `routes/users.py` | User lookup, streak lookup, notification list/mark-read. Delegates to `streak_service` and `notification_service`. |
| `routes/feed.py` | "Listening now" and general activity feed endpoints. Delegates to `feed_service`. |
| `services/streak_service.py` | Records listening events and updates `User.listening_streak` / `last_listened_at` based on calendar-day comparisons. |
| `services/feed_service.py` | Builds the "friends listening now" (24h window, deduped per friend) and general activity feed (no time filter, capped by `limit`) from `ListeningEvent` rows filtered to `user.friends`. |
| `services/search_service.py` | Text search over `Song.title` / `Song.artist`, outer-joined to `song_tags`. |
| `services/notification_service.py` | Notification CRUD (`create_notification`, `get_notifications`, `mark_as_read`) **and** two domain actions that happen to live here: `add_to_playlist` (mutates `playlist.songs`, then conditionally notifies the original sharer) and `rate_song` (create-or-update a `Rating`, no notification triggered). |
| `services/playlist_service.py` | Playlist creation and read-only retrieval: `get_playlist`, `get_user_playlists` (creator-only, not collaborators), and `get_playlist_songs` (joins the raw `playlist_entries` table, ordered by `position`). |
| `seed_data.py` | Populates the DB with 5 users (bidirectional friendships), 25 songs (with 0, 1, or 3+ tags — multi-tag songs are explicitly noted in the script as exercising Issue #3), 3 playlists, a mix of recent and older `ListeningEvent` rows, preset streak values, and one example "song added to playlist" notification. |
| `tests/` | `test_streaks.py`, `test_search.py`, `test_playlists.py` — existing test coverage to reference/extend. |

### Data flow: adding a song to a playlist

1. **Request:** `POST /playlists/<playlist_id>/songs` with `{ song_id, added_by }`.
2. **Route (`routes/playlists.py::add_song`):** validates both fields are present, then calls `add_to_playlist(playlist_id, song_id, added_by)` — imported from `services.notification_service`, not `services.playlist_service`, despite the route file's name.
3. **Service (`notification_service.add_to_playlist`):**
   - Loads `Song`, `User` (adder), `Playlist` — 404/400 via `ValueError` if any is missing.
   - If the song isn't already in `playlist.songs`, appends it via the ORM relationship (`playlist.songs.append(song)`) and commits. This writes a row into the `playlist_entries` association table.
   - If the adder is not the song's original sharer (`song.shared_by`), calls `create_notification(...)` to notify the sharer.
4. **Read-back (separate request):** `GET /playlists/<playlist_id>/songs` → `routes/playlists.py::get_songs` → `playlist_service.get_playlist_songs`, which queries the **raw `playlist_entries` table** directly (not the ORM relationship), joined to `Song`, ordered by `position` ascending.

**Notable pattern:** the write path and read path touch the same association table (`playlist_entries`) through two different access methods — ORM relationship append vs. raw table join/query. The write path never explicitly sets `position`; how that column ends up populated is worth tracing carefully.

### Data flow: a friend's listen appearing in "Friends Listening Now"

1. **Record:** `POST /songs/<song_id>/listen` with `{ user_id }` → `routes/songs.py::listen` → `streak_service.record_listening_event`, which creates a `ListeningEvent(listened_at=now)` and also updates the listener's streak in the same call (one action, two side effects).
2. **View:** `GET /feed/<user_id>/listening-now` → `routes/feed.py::listening_now` → `feed_service.get_friends_listening_now`, which:
   - Resolves `friend_ids` from `user.friends` (confirmed bidirectional in `seed_data.py`).
   - Filters `ListeningEvent` to `user_id IN friend_ids AND listened_at >= (now - 24h)`.
   - Dedupes to one (most recent) event per friend.
3. The two functions never call each other directly — they're connected only by the shared `ListeningEvent` table and the meaning of `listened_at` on both ends.

### Data flow: a user rates a song

1. **Request:** `POST /songs/<song_id>/rate` with `{ user_id, score }`.
2. **Route (`routes/songs.py::rate`):** validates both fields are present, casts `score` to `int`, then calls `rate_song(user_id, song_id, score)` — imported from `services.notification_service`, the same file that hosts `add_to_playlist`.
3. **Service (`notification_service.rate_song`):**
   - Validates `score` is between 1 and 5.
   - Loads `Song` and `User` (rater) — raises `ValueError` if either is missing.
   - Looks up an existing `Rating` for this `(user_id, song_id)` pair (enforced unique by `models.py`'s `UniqueConstraint`).
   - If found, updates its `score` in place; if not, creates a new `Rating` row.
   - Commits and returns the `Rating`.
4. **Notification: does not happen.** Unlike its sibling function `add_to_playlist` in the same file — which calls `create_notification(...)` after mutating the playlist — `rate_song` never calls `create_notification` anywhere in its body. The `Rating` itself is persisted correctly and readable via other endpoints, but no `Notification` row is ever created as a result of a rating.

**Comparison to the playlist-add flow:** both functions follow the same shape (load entities → mutate/persist → [conditionally] notify), but only `add_to_playlist` completes the third step. This directly explains reported issue #4 ("notified when a friend added my song to a playlist but not when they rated it") — the rating path is simply missing the call that the playlist-add path has. See root cause analysis section for full write-up once fixed.

## Root Cause Analysis

### Issue #1: My listening streak keeps resetting

**How I reproduced it:**

Isolated `update_listening_streak()` in a Flask shell rather than going through HTTP, to get precise control over the `now` value passed in.

- **Case A (bug):** Set a `User` with `listening_streak = 5` and `last_listened_at` = Saturday, June 27, 2026 (UTC). Called `update_listening_streak(user, sunday)` with `sunday` = June 28, 2026 (UTC) — exactly one consecutive calendar day later. Expected the streak to increment to 6 per the documented rules ("If the user listened yesterday: streak increments by 1"). Instead, the streak dropped to 1. Confirmed `sunday.weekday() == 6`.
- **Case B (control):** Repeated the identical setup but with `last_listened_at` = Monday, June 29, 2026 and the new listen on Tuesday, June 30, 2026 — also exactly one consecutive day apart. This time the streak correctly incremented from 5 to 6. Confirmed `tuesday.weekday() == 1`.

The two cases are identical in every respect (same starting streak, same one-day gap) except which day of the week the second listen fell on. This isolates the failure to the specific case where the new listening event occurs on a Sunday, ruling out a broader problem with the day-gap/increment logic itself.

*(Root cause, fix, and side-effect verification to be completed in Milestone 3.)*

### Issue #2: Friends Listening Now shows people from yesterday

**How I reproduced it:**

Started by testing the existing seed data directly (`get_friends_listening_now` for nova and for kenji) — both returned correct results, with no stale friends appearing. This ruled out the codebase's default state as a way to see the bug, so I moved to isolating the underlying mechanisms one at a time using a standalone script (`debug_repro.py`, run with `python debug_repro.py` inside an `app.app_context()`), rather than relying on the seed data alone:

1. **Checked whether SQLite strips timezone info on read.** Queried a real `ListeningEvent` row directly and found `listened_at.tzinfo` returns `None` after being read back from the DB, even though it's written as timezone-aware (`datetime.now(timezone.utc)`). This looked like a plausible cause at first.
2. **Verified whether that round-trip actually breaks the SQL filter.** Compiled the literal SQL `get_friends_listening_now` generates (via `query.statement.compile(..., compile_kwargs={"literal_binds": True})`) and compared the cutoff literal against raw stored strings pulled directly via `sqlite3`. Both were in the identical format (no timezone offset on either side), so the comparison is internally consistent — this ruled out the timezone round-trip as the cause.
3. **Checked the `friendships` table directly** via raw SQL — found exactly 10 rows, matching `seed_data.py`'s 5 bidirectional `add_friendship()` calls, no duplicates or missing pairs. Ruled out.
4. **Reviewed `routes/feed.py`** — confirmed it's a clean pass-through with no transformation between the service return value and the JSON response. Ruled out.
5. **Manufactured a targeted data point:** inserted a `ListeningEvent` for `aaliya` (kenji's only friend with no other competing recent event) at exactly 19 hours before "now," then called `get_friends_listening_now(kenji.id)`. Result: **aaliya appeared in kenji's feed** with the 19-hour-old timestamp.

This confirms the bug is reproducible, but only when a friend's most recent event falls somewhere between roughly "more than a few hours old" and the 24-hour cutoff — the existing seed data didn't happen to contain a case in that exact window, which is why the bug wasn't visible without manufacturing one.

**How I found the root cause:**

Compared `get_friends_listening_now()` against its sibling function `get_activity_feed()` in the same file. `get_activity_feed`'s own docstring explicitly states it is the *unfiltered, historical* feed ("this is not filtered by recency"), implying by contrast that `get_friends_listening_now` was intended to represent a much narrower, genuinely live window — not a full calendar day.

**The root cause:**

`get_friends_listening_now()` filters events using `RECENT_THRESHOLD = timedelta(hours=24)`. The filter and its underlying SQL comparison work exactly as coded (verified: the compiled cutoff literal matched the stored timestamp format exactly, ruling out a timezone/string-comparison defect). The actual problem is the threshold value itself: 24 hours is too generous for a feature presented to users as "Listening Now." A friend who listened at, e.g., 10pm the previous night will still appear as "currently listening" for the entire next day, which matches the reported symptom ("shows people from yesterday"). This is a threshold/design defect rather than a broken comparison or missing condition.

*(Fix and side-effect verification to be completed in Milestone 3.)*

### Patterns observed

- **Domain logic doesn't always live where its route file's name implies.** Playlist mutation (`add_to_playlist`) lives in `notification_service.py`, not `playlist_service.py`.
- **Inconsistent access to association tables.** Some code goes through ORM relationships (`playlist.songs.append(...)`, `user.friends`), other code queries the raw association table object directly (`playlist_entries`, `song_tags`) with explicit joins.
- **Per-row hydration instead of joined queries.** `feed_service.py` calls `db.session.get()` individually for each friend/song inside a loop rather than joining `User`/`Song` into the original query.
- **Song creation has no route.** All `Song` rows in this repo originate from `seed_data.py`; no blueprint exposes a "create/share song" endpoint.
- **Docstrings vs. code.** In at least one service file reviewed so far, a docstring's stated behavior and the code's actual branching didn't fully match — worth checking this systematically against every function, not just the one already found.