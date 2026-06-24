# Soulogos Session

Real-time voice transcription for tabletop RPG sessions on Discord.

Soulogos Session is a companion bot to [Soulogos](https://github.com/), the NPC voice bot for Dungeon Masters. Where Soulogos gives voice to your NPCs, Soulogos Session listens. It joins a voice channel, transcribes everything your table says in real time, stores it per speaker in a local database, and lets you generate an AI session summary with a single button press once the session ends.

## What it is and why to use it

Running a TTRPG session is busy work. You are narrating, adjudicating rules, voicing NPCs, and tracking initiative all at once. Taking good notes on top of that is hard, and the details that matter for next week (the name a player gave an innkeeper, the deal they struck with a hag, the thread they swore to chase down) are exactly the ones that slip away.

Soulogos Session removes the note-taking burden. It captures the full spoken record of your session so you can stay present at the table:

- **Captures everything said** in a Discord voice channel, attributed per speaker.
- **Stores transcripts in SQLite** on your own machine, so the record is yours and persists across sessions.
- **Generates AI summaries** after the fact using Claude, turning a raw transcript into a structured recap with key events, NPC interactions, player decisions, and unresolved threads.

Paired with Soulogos (the NPC voice bot), it forms a complete session toolkit: one bot speaks for your world, the other remembers it.

## Features

- **DAVE E2EE support.** Decrypts Discord's end-to-end encrypted audio (DAVE protocol) so transcription works on voice channels with E2EE active.
- **Per-user Whisper transcription.** Audio is decoded and buffered per speaker, then transcribed with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), so every line is attributed to the right person.
- **Local SQLite storage.** Sessions and transcript lines live in a local SQLite database. No cloud storage, no third-party retention.
- **Claude AI summaries.** Generate a structured markdown recap of any session on demand, written for a Dungeon Master planning the next game.
- **Discord slash commands.** Start, stop, list, and delete captures with simple slash commands, plus an inline button to produce a summary.

## Requirements

- **Python 3.12+**
- **A Discord bot token** with the voice and guilds intents enabled
- **An Anthropic API key** for session summaries
- **ffmpeg** available on your system path (required by the voice stack)
- **discord-ext-voice-recv** for receiving voice (installed as a dependency; see Known limitations)

## Installation

Clone the repository and set up a virtual environment:

```bash
git clone <your-repo-url> soulogos-session
cd soulogos-session

python3 -m venv .venv
source .venv/bin/activate        # on Windows: .venv\Scripts\activate

pip install -e .
```

Copy the example environment file and fill it in:

```bash
cp .env.example .env
```

Configure the fields in `.env`:

| Variable | Description |
| --- | --- |
| `DISCORD_BOT_TOKEN` | Your Discord bot token from the Discord Developer Portal. Required. |
| `WHISPER_MODEL` | faster-whisper model size: `tiny`, `base`, `small`, `medium`, `large-v2`, `large-v3`. Larger is more accurate and slower. Defaults to `base`. |
| `WHISPER_DEVICE` | Inference device: `cpu`, `cuda`, or `auto`. Defaults to `cpu`. |
| `SOULOGOS_DB_PATH` | Path to the main Soulogos `memory.sqlite` used for player and character lookups. |
| `ANTHROPIC_API_KEY` | Your Anthropic API key, used to generate session summaries. |
| `SUMMARIES_PATH` | Directory where generated summary `.md` files are written. Defaults to `data/summaries`. |

There is also a `SESSION_DB_PATH` field that controls where this bot writes its own `session.sqlite` (defaults to `data/session.sqlite`).

Run the bot:

```bash
python -m soulogos_session
```

## Commands

| Command | What it does |
| --- | --- |
| `/capture-join [channel]` | Joins a voice channel and starts transcribing. Defaults to your current voice channel if none is given. |
| `/capture-end` | Stops transcribing, leaves the voice channel, and closes out the session. |
| `/capture-list` | Lists recording sessions for the server as an embed, showing session ID, start time, end time, and line count. Each session gets a Delete button and a capture-transcribe button. |
| `/capture-delete <session_id>` | Deletes a session and all of its transcript lines. |

### The capture-transcribe button

Each session in the `/capture-list` embed has a **capture-transcribe** button. Pressing it:

1. Fetches all transcript lines for that session.
2. Sends the formatted transcript to Claude (`claude-sonnet-4-6`) with a DM-focused system prompt.
3. Produces a structured markdown summary with these sections: Session Overview, Key Events, NPC Interactions, Player Decisions, Unresolved Threads, and DM Notes for Next Session.
4. Writes the result to `session_{session_id}_summary.md` under your `SUMMARIES_PATH`.
5. Posts the file back to you privately (ephemeral) as an attachment.

## Architecture

The codebase is small and split by responsibility:

- **`recorder.py`** handles voice receive. It decrypts DAVE E2EE audio, decodes Opus per user, and buffers PCM into roughly 2-second chunks before handing each chunk to a queue. Audio callbacks run on a background thread, so work is pushed back to the event loop using thread-safe scheduling.
- **`transcriber.py`** wraps faster-whisper. It takes a PCM chunk and returns transcribed text with a confidence score.
- **`store.py`** is the persistence layer. It uses synchronous `sqlite3` wrapped in `asyncio.to_thread()` for every operation, which keeps database work off the event loop and avoids the background-thread deadlocks that an async SQLite driver can introduce.
- **`bot.py`** ties it together. A long-running `asyncio` task consumes audio chunks from the queue, runs the blocking Whisper call through `asyncio.to_thread()` with a timeout so it can never starve the event loop, and writes results to the store. It also defines the slash commands and the summary button.

The flow at a glance:

```
voice channel -> recorder.py (decrypt, decode, buffer) -> queue
queue -> bot.py transcription loop -> transcriber.py (Whisper) -> store.py (SQLite)
/capture-list button -> bot.py -> Claude -> summary .md file
```

## Known limitations

- **discord-ext-voice-recv is inactive.** Voice receive on Discord is not officially supported by discord.py, and the `discord-ext-voice-recv` extension this bot relies on is effectively discontinued. It works today, but it is not actively maintained.
- **Risk of breakage on Discord protocol changes.** Because voice receive depends on undocumented behavior, a change to Discord's voice or DAVE protocol can break audio capture with little warning. Treat this bot as a useful tool rather than a guaranteed-stable service, and expect to pin dependency versions.
- **Transcription quality depends on the model and hardware.** Smaller Whisper models are fast but less accurate. Crosstalk, background noise, and accents all reduce accuracy. The transcript is a strong aid, not a perfect record.

## Version

v0.1.2
