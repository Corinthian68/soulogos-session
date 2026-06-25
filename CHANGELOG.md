# Changelog

All notable changes to Soulogos Session are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [0.1.2] - 2026-06-24

### Added

- **Session name parameter.** `/capture-join` accepts an optional `name` argument (e.g. "Crown of the Oathbreaker Session 6"). The name is stored in the sessions table, shown in the `/capture-list` embed, and included in the headers of all generated files.
- **Export button.** Posts the raw timestamped transcript as a `.md` file attachment to the DM-only `#prep-notes` channel and sends the same file ephemerally to the DM who clicked.
- **Condense button.** Generates a structured session debrief using `claude-sonnet-4-6` with the Crown summary prompt. Covers what happened, canon facts locked, NPC state changes, arc beats per PC, threads opened and closed, items and resources, location at session end, next-session pressure, and carry-in state. Posts to `#prep-notes` as a file attachment.
- **Recap button.** Generates a player-facing prose recap using `claude-sonnet-4-6` with the player recap prompt. 200-300 words of flowing prose, no mechanical detail, written in second person. Posts to `#session-log` as a file attachment.
- **Delete All button.** A single "Delete All Sessions" button in the `/capture-list` embed deletes all sessions and transcript lines for the server and refreshes the embed in place.
- **Refresh after delete.** Clicking a per-session Delete button now edits the original embed message to reflect the updated list rather than leaving stale buttons.
- **Dual channel routing.** Export, Condense, and Transcribe post to the DM-only channel (`DM_CHANNEL_ID`, defaults to `#prep-notes`). Recap posts to the player-visible channel (`PLAYER_CHANNEL_ID`, defaults to `#session-log`). Both are configurable via environment variables.
- **Prompt files.** `data/prompts/crown_summary_prompt.txt` and `data/prompts/crown_recap_prompt.txt` are loaded at startup and drive the Condense and Recap buttons respectively. If a file is missing the bot falls back to a built-in generic prompt.
- **Timestamps in transcript exports.** Every line in an exported transcript is prefixed with `[HH:MM:SS]` derived from the stored ISO timestamp.
- **TTRPG Whisper vocabulary prompt.** An `initial_prompt` containing D&D and Crown-specific terms (character names, spell vocabulary, location names) is passed to faster-whisper to improve transcription accuracy on fantasy proper nouns.
- **Long transcript chunking.** Condense and Recap break transcripts over 3000 words into sections, summarize each section individually, then make one final Claude call to stitch the section summaries into a single unified output.
- **Session name in embed and button labels.** The `/capture-list` embed shows the session name next to the ID. Button labels show the session name, truncated to 77 characters to stay within Discord limits.
- **`on_app_command_error` handler.** Unhandled slash command errors are now logged with full traceback and reported back to the user ephemerally.
- **`name` column in sessions table.** A `name TEXT` column is added to the `sessions` schema. Existing databases are migrated automatically via `ALTER TABLE` on `store.init()`.
- **`get_session` store method.** Fetches a single session row by ID, used by the Export, Condense, and Recap callbacks to retrieve the session name.
- **`delete_all_sessions` store method.** Deletes all sessions and their transcript lines for a given guild, returns the count of deleted sessions.

### Changed

- **Command prefix renamed from `session-` to `capture-`.** All four slash commands are now `/capture-join`, `/capture-end`, `/capture-list`, and `/capture-delete`. This avoids collision with the main Soulogos bot's `session-` namespace.
- **Transcribe button now posts to `#prep-notes`.** The generic Claude summary is posted to `DM_CHANNEL_ID` as an ephemeral DM file attachment. Previously it was ephemeral-only.
- **`capture-list` shows up to 4 sessions instead of 5.** Row 4 is now reserved for the Delete All button.
- **`/capture-list` embed field format.** Each field now shows session name (if set), line count, and status on a single compact line.
- **Log noise reduced.** The `write() called` and `dave_decrypt result` lines in `recorder.py` are now `DEBUG` level. Warning-level logs for DAVE decryption failures are unchanged.
- **`_build_session_embed` extracted** from the `/capture-list` command handler into a shared helper, reused by the delete-and-refresh flow.
- **Store migrated from `aiosqlite` to synchronous `sqlite3 + asyncio.to_thread`.** See v0.1.1 entry; finalized in this release.

---

## [0.1.1] - 2026-06-01

### Changed

- **Whisper transcription moved to `asyncio.to_thread`.** The blocking faster-whisper inference call inside `_transcription_loop` was running synchronously on the event loop, which stalled Discord gateway processing and caused interaction timeouts. It now runs in a thread pool via `asyncio.wait_for(asyncio.to_thread(...), timeout=10.0)`.
- **SQLite store migrated from `aiosqlite` to synchronous `sqlite3 + asyncio.to_thread`.** `aiosqlite` runs its own background thread and dispatches coroutines back to the event loop; under load this introduced deadlocks when the event loop was busy. Every store method now delegates to a plain synchronous helper via `asyncio.to_thread`, eliminating the background-thread dependency entirely.
- **`session-join` and `session-end` now defer immediately.** Both commands call `interaction.response.defer(ephemeral=True)` as their first statement before any async work, preventing Discord interaction timeouts on slow operations like voice channel connection.
- **`recorder.stop()` runs in a thread.** `stop_listening()` on the voice-recv client joins its audio receive thread, which could block the event loop for several seconds. It now runs via `asyncio.to_thread` with a 3-second timeout.
- **`bot.store.end_session` wrapped with a 5-second timeout.** A stalled database write can no longer prevent the session-end confirmation from reaching the user.
- **`call_soon_threadsafe` + `put_nowait` replaces `run_coroutine_threadsafe` in the recorder.** Audio queue puts from the voice-recv audio thread are now scheduled via `loop.call_soon_threadsafe(queue.put_nowait, ...)`, which is simpler and avoids creating a coroutine object per audio packet.

### Fixed

- **Session-end interaction timeout.** The combination of a synchronous Whisper call and a blocking thread join in `stop_listening()` could stall the event loop long enough for Discord to expire the interaction before the bot responded.

---

## [0.1.0] - 2026-05-15

### Added

- **Initial release.**
- **DAVE E2EE audio decryption.** Decrypts Discord end-to-end encrypted voice audio using the DAVE protocol before Opus decoding.
- **Per-user voice capture.** `recorder.py` decodes Opus per Discord user, buffers roughly 2 seconds of PCM per speaker, and enqueues chunks for transcription.
- **Whisper transcription.** `transcriber.py` wraps faster-whisper. Each audio chunk is downsampled from 48 kHz stereo to 16 kHz mono and transcribed. Returns text and a confidence score derived from segment log-probability.
- **SQLite session store.** `store.py` persists sessions and per-speaker transcript lines in a local SQLite database. No network dependency, no third-party retention.
- **`/session-join` command.** Joins a voice channel, creates a session record, and starts the transcription loop.
- **`/session-end` command.** Stops recording, disconnects from the voice channel, and closes the session.
- **`/session-list` command.** Displays all sessions for the server as a Discord embed with per-session Delete and Transcribe buttons.
- **`/session-delete` command.** Deletes a session and all its transcript lines by session ID.
- **Session IDs as `YYYYMMDD_HHMMSS` timestamps.** Session IDs are human-readable timestamp strings. Collisions within the same second are resolved by appending a counter suffix.
- **Transcribe button.** Sends the full transcript to `claude-sonnet-4-6` and returns a structured markdown summary (Session Overview, Key Events, NPC Interactions, Player Decisions, Unresolved Threads, DM Notes for Next Session) as an ephemeral file attachment.
- **`player_lookup.py`.** Loads a player-to-character mapping from the main Soulogos `memory.sqlite` so transcript lines can be attributed to character names instead of Discord display names.
