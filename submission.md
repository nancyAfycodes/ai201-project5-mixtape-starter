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

**Independent confirmation:** the project's own `tests/test_streaks.py` contains `test_streak_increments_on_sunday`, which asserts a Saturday→Sunday listen should increment the streak to 2. Running `pytest tests/test_streaks.py -v` confirms this test currently **fails** (`assert 1 == 2`) while the other 4 streak tests pass — matching my manual reproduction exactly and confirming this is a pre-existing, project-authored expectation, not just my own interpretation of the docstring.

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

### Issue #3: The same song keeps showing up twice in search — investigated, not reproducible in this environment

**How I attempted to reproduce it:**

`seed_data.py`'s own comments flag multi-tag songs (3+ tags) as the intended trigger for this bug, since `search_songs()` in `search_service.py` performs an `outerjoin` against `song_tags` without any apparent deduplication step. I tested this from multiple angles:

1. Queried `search_songs("Crown Heights")` (a 3-tag song) directly — returned exactly 1 result, not 3.
2. Queried the same `search_songs()` function across a 0-tag, 1-tag, and three different 3-tag songs individually — all returned exactly 1 result each, with correct tag lists.
3. Ran a broad query (`search_songs("a")`) matching 13 of the seeded songs and manually counted occurrences of each returned `id` — no duplicates found; 13 rows returned for 13 total `Song` rows in the DB.
4. Bypassed the ORM entirely and ran the equivalent join as raw SQL directly against the DB:
   ```sql
   SELECT song.id, song.title FROM song
   LEFT OUTER JOIN song_tags ON song.id = song_tags.song_id
   WHERE song.title LIKE '%Crown Heights%'
   ```
   This **did** return 3 identical rows (one per tag) — confirming the join itself produces row fan-out at the SQL level, exactly as the code's structure would suggest.
5. Ran the project's own test suite (`pytest tests/test_search.py -v`), including `test_search_no_duplicates_multi_tag_song`, whose inline comment states `# Should be 1, bug causes it to be 3`. All 5 tests passed, including this one.

**Conclusion:**

The join does produce duplicate rows at the raw SQL level (step 4), but SQLAlchemy's ORM layer (version 2.0.51, confirmed via `pip show sqlalchemy`) appears to automatically deduplicate repeated primary keys when the query selects full mapped entities (`db.session.query(Song)`), collapsing the 3 joined rows back into a single `Song` object before `search_songs()` ever returns. This most likely means the originally-intended bug relied on different deduplication behavior in an older SQLAlchemy version, and is not observable as a user-facing defect in this environment as currently configured.

Per the Milestone 2 guidance ("If you can't reproduce a bug after a genuine attempt, try a different one from the list"), I moved on to Issue #5 as a third bug to fix, after a genuine, multi-angle reproduction attempt on this one.

### Issue #4: Notified when a friend adds my song to a playlist, but not when they rate it

**How I reproduced it:**

Ran a standalone script (`debug_repro_issue4.py`) that exercises `rate_song()` directly — the same function `POST /songs/<song_id>/rate` calls:

1. Selected a seeded song ("Crown Heights Anthem," shared by user `ea6d7a9f...`) and a different user (`nova`, `a51eefbe...`) as the rater — confirmed the rater is not the song's original sharer.
2. Counted the sharer's notifications via `get_notifications()` before rating: **0**.
3. Called `rate_song(rater.id, song.id, 5)` — this succeeded and returned a `Rating` with the correct `score`, `user_id`, and `song_id`.
4. Counted the sharer's notifications again after rating: **0** — unchanged.

The rating itself is saved correctly and is queryable elsewhere in the app; specifically no `Notification` row is created as a side effect of rating, confirmed by a direct before/after count rather than just reading the code.

**How I found the root cause:**

Traced the call chain from the route down: `routes/songs.py::rate` → `notification_service.rate_song`. Read `rate_song()` line by line and found it only ever loads `Song`/`User`, checks for an existing `Rating`, updates-or-creates it, and commits — no call to `create_notification()` anywhere in the function body. Compared this directly against the sibling function `add_to_playlist()` in the same file, which follows the same overall shape (load entities → mutate/persist → conditionally notify) but does call `create_notification()` as its final step, guarded by a check that the actor isn't the song's original sharer. The moment I confirmed this was the specific cause (not just a "suspicious area") was seeing that `rate_song()` has no equivalent call or guard condition at all — the notification step isn't broken, it's simply absent.

**The root cause:**

`rate_song()` in `notification_service.py` persists the `Rating` correctly but never calls `create_notification()`, unlike its sibling function `add_to_playlist()`, which does call it after mutating the playlist. As a result, a song's original sharer receives a notification when a friend adds their song to a playlist, but receives no notification when a friend rates that same song — even though both are user-facing, friend-triggered interactions with a shared song.

*(Fix and side-effect verification to be completed in Milestone 3.)*

### Issue #5: The last song in a playlist never shows up

**How I reproduced it:**

Ran a standalone script (`debug_repro_issue5.py`) comparing raw `playlist_entries` rows against `get_playlist_songs()`'s output, across all 3 seeded playlists:

| Playlist | Rows in `playlist_entries` | Songs returned by `get_playlist_songs()` | Missing |
|---|---|---|---|
| Late Night Vibes | 7 | 6 | Free Throws |
| Friday Energy | 7 | 6 | Harlem Renaissance |
| Study Mode | 7 | 6 | Lagos to London |

In every case, exactly one song was missing, and in every case it was the song with the highest `position` value — i.e., the most recently added song in that playlist.

**Independent confirmation:** the project's own `tests/test_playlists.py` contains `test_playlist_returns_all_songs` (asserting `len(songs) == 5`, with an inline comment `# Bug causes this to return 4`) and `test_playlist_returns_songs_in_order` (asserting all 5 seeded titles are returned in order). Running `pytest tests/test_playlists.py -v` confirms both currently **fail**: the first with `assert 4 == 5`, the second showing the returned list is missing exactly `'Track 5'` — the highest-position song. This matches my manual reproduction exactly and confirms the exclusion is unintended (the test's own comment explicitly calls it a "bug"), ruling out the possibility that the missing song is an intentional trailing exclusion.

**How I found the root cause:**

Re-read `get_playlist_songs()` in `playlist_service.py` line by line. The query itself — join `Song` to `playlist_entries`, filter by `playlist_id`, order ascending by `position` — has no limit or filter that would exclude anything; `.all()` returns the full, correctly-ordered list. The very next line is where the count changes:
```python
return [song.to_dict() for song in songs[:-1]]
```
The slice `songs[:-1]` returns everything except the last element of the list. Confirmed this is the exact mechanism by checking: (1) the query result before the slice contains all songs, and (2) since the list is ordered ascending by `position`, the last element is always the most-recently-added song — which matches exactly which song went missing in every reproduction case above.

**The root cause:**

`get_playlist_songs()` correctly queries and orders all songs in a playlist by ascending `position`, but its return statement applies `songs[:-1]` before converting the results to dicts, which unconditionally drops the last element of the list. Because the list is ordered ascending by position, the last element is always the most recently added song — so every playlist is missing exactly one song: whichever one was added last. This is confirmed as unintended by both the function's own docstring ("This function returns all songs in the playlist") and the project's existing test suite, which explicitly labels the resulting count mismatch a bug.

*(Fix and side-effect verification to be completed in Milestone 3.)*

### Patterns observed

- **Domain logic doesn't always live where its route file's name implies.** Playlist mutation (`add_to_playlist`) lives in `notification_service.py`, not `playlist_service.py`.
- **Inconsistent access to association tables.** Some code goes through ORM relationships (`playlist.songs.append(...)`, `user.friends`), other code queries the raw association table object directly (`playlist_entries`, `song_tags`) with explicit joins.
- **Per-row hydration instead of joined queries.** `feed_service.py` calls `db.session.get()` individually for each friend/song inside a loop rather than joining `User`/`Song` into the original query.
- **Song creation has no route.** All `Song` rows in this repo originate from `seed_data.py`; no blueprint exposes a "create/share song" endpoint.
- **Docstrings vs. code.** In at least one service file reviewed so far, a docstring's stated behavior and the code's actual branching didn't fully match — worth checking this systematically against every function, not just the one already found.