# Soulogos Session

Real-time voice transcription for tabletop RPG sessions on Discord.

Soulogos Session is a companion bot to [Soulogos](https://github.com/), the NPC voice bot for Dungeon Masters. Where Soulogos gives voice to your NPCs, Soulogos Session listens. It joins a voice channel, transcribes everything your table says in real time, stores it per speaker in a local database, and generates AI session documents at the end of the night.

## What it is and why to use it

Running a TTRPG session is busy work. You are narrating, adjudicating rules, voicing NPCs, and tracking initiative all at once. Taking good notes on top of that is hard, and the details that matter for next week (the name a player gave an innkeeper, the deal they struck with a hag, the thread they swore to chase down) are exactly the ones that slip away.

Soulogos Session removes the note-taking burden. It captures the full spoken record of your session so you can stay present at the table:

- **Captures everything said** in a Discord voice channel, attributed per speaker.
- **Stores transcripts in SQLite** on your own machine, so the record is yours and persists across sessions.
- **Generates AI documents** after the fact using Claude: a structured DM debrief, a player-facing recap, and a raw timestamped transcript.

Paired with Soulogos (the NPC voice bot), it forms a complete session toolkit: one bot speaks for your world, the other remembers it.

## Features

- **DAVE E2EE support.** Decrypts Discord's end-to-end encrypted audio (DAVE protocol) so transcription works on voice channels with E2EE active.
- **Per-user Whisper transcription.** Audio is decoded and buffered per speaker, then transcribed with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), so every line is attributed to the right person.
- **TTRPG vocabulary bias.** An initial prompt containing D&D terms, spell vocabulary, and campaign-specific proper nouns is passed to Whisper to improve transcription accuracy on fantasy content.
- **Local SQLite storage.** Sessions and transcript lines live in a local SQLite database. No cloud storage, no third-party retention.
- **Session names.** Each capture can be tagged with a human-readable name (e.g. "Crown of the Oathbreaker Session 6") that appears in the session list and in the headers of all generated files.
- **Claude AI documents.** Three distinct AI outputs available per session: a structured DM debrief, a player-facing prose recap, and a generic session summary.
- **Dual channel routing.** DM documents (debrief, transcript, summary) post to a private prep-notes channel. Player-facing recaps post to the player-visible session-log channel.
- **Customizable prompts.** The debrief and recap prompts are plain text files on disk, easy to edit without touching code.
- **Discord slash commands.** Start, stop, list, and delete captures with simple slash commands.

## Requirements

- **Python 3.12+**
- **A Discord bot token** with the voice and guilds intents enabled
- **An Anthropic API key** for AI document generation
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
| `ANTHROPIC_API_KEY` | Your Anthropic API key, used to generate AI session documents. |
| `SUMMARIES_PATH` | Directory where generated `.md` files are written. Defaults to `data/summaries`. |
| `SESSION_DB_PATH` | Where this bot writes its own `session.sqlite`. Defaults to `data/session.sqlite`. |

See also: Channel Configuration and Prompt Files sections below.

Run the bot:

```bash
python -m soulogos_session
```

## Commands

| Command | What it does |
| --- | --- |
| `/capture-join [channel] [name]` | Joins a voice channel and starts transcribing. Defaults to your current voice channel if none is given. The optional `name` argument tags the session (e.g. "Crown of the Oathbreaker Session 6"). Also posts an ephemeral Pause/Resume control panel. |
| `/capture-pause` | Pauses the active recording. The bot stays in the voice channel but drops incoming audio at the sink until resumed. Ephemeral. |
| `/capture-resume` | Resumes a paused recording. Ephemeral. |
| `/capture-end` | Stops transcribing, leaves the voice channel, and closes out the session. |
| `/capture-list` | Lists recording sessions for the server as a Discord embed with action buttons for each session. |
| `/capture-delete <session_id>` | Deletes a session and all of its transcript lines by session ID. |

## Buttons

Each session in the `/capture-list` embed has five buttons. Discord allows up to five buttons per row, so each session occupies one row. Up to four sessions are shown, with a single "Delete All Sessions" button on the bottom row.

### Delete

Deletes that session and all its transcript lines, then refreshes the embed in place. If no sessions remain after the delete, the embed is replaced with a "No sessions found" message.

### Transcribe

Sends the full transcript to Claude (`claude-sonnet-4-6`) with a generic DM-focused system prompt. Produces a structured markdown summary covering: Session Overview, Key Events, NPC Interactions, Player Decisions, Unresolved Threads, and DM Notes for Next Session. Posts the file to **#prep-notes** (the DM-only channel) and sends it ephemerally to you.

### Export

Writes the raw timestamped transcript to a `.md` file. Each line is formatted as `[HH:MM:SS] **Speaker:** text`. Posts the file to **#prep-notes** and sends it ephemerally to you. Use this to have a permanent record of what was said before running any AI processing.

### Condense

Generates a structured session debrief using the Crown summary prompt (see Prompt Files). Covers ten sections: what happened, canon facts locked, NPC state changes, arc beats per PC, threads opened, threads closed, items and resources, location at session end, next-session pressure, and carry-in knowledge state. For long sessions (over 3000 words), the transcript is processed in sections and then stitched into one unified output.

Condense is the first stage of the structured-log chain: it always regenerates the debrief from the raw transcript and overwrites the stored structured log at `data/logs/session_{id}_structured.md`, then posts the file to **#prep-notes** and sends it ephemerally to you. Run it again any time to rebuild the log.

### Recap

Generates a player-facing prose recap using the Crown recap prompt. 200-300 words in second person, no mechanical detail, no DM-only information.

Recap is the second stage of the chain: it reads the stored structured log (`data/logs/session_{id}_structured.md`) rather than the raw transcript, so the recap stays consistent with the Condense debrief. If no structured log exists yet (Condense was never run), Recap silently generates one from the transcript first, without posting it. Posts the recap file to **#session-log** (the player-visible channel) and sends it ephemerally to you.

### Delete All

One button at the bottom of the embed. Deletes all sessions for this server in a single operation and refreshes the embed to show "No sessions found."

## Recording Controls

When you run `/capture-join`, the bot posts an ephemeral panel with **Pause Recording** and **Resume Recording** buttons, visible only to you (the command is used in a player-visible channel). Pausing drops incoming audio packets at the sink level so nothing reaches the transcription queue, while the bot stays connected to the voice channel. The same toggle is available via the `/capture-pause` and `/capture-resume` slash commands.

## Channel Configuration

Two channel IDs control where AI-generated documents are posted:

| Variable | Default | Channel |
| --- | --- | --- |
| `DM_CHANNEL_ID` | `1499171448043081911` | The DM-only prep-notes channel. Receives transcripts, debriefs, and generic summaries. |
| `PLAYER_CHANNEL_ID` | `1499170547601506355` | The player-visible session-log channel. Receives player-facing recaps only. |

Set these in your `.env` file to match your server's actual channel IDs:

```
DM_CHANNEL_ID=your_prep_notes_channel_id
PLAYER_CHANNEL_ID=your_session_log_channel_id
```

## Prompt Files

The Condense and Recap buttons are driven by plain text prompt files stored in `data/prompts/`. Both files are loaded at bot startup. If a file is missing or empty, the bot falls back to a built-in generic prompt.

### `data/prompts/crown_summary_prompt.txt`

Used by the Condense button. The default content is a structured debrief template for the Crown of the Oathbreaker campaign, with ten labeled sections, output target of 400-600 words, and instructions to flag uncertain content with `[VERIFY]` and suspected transcription errors with `[WHISPER UNCLEAR]`.

### `data/prompts/crown_recap_prompt.txt`

Used by the Recap button. The default content instructs Claude to write a 200-300 word flowing prose recap in second person, covering only what the players experienced, with no DM-only information and no mechanical detail.

### Customizing prompts

Edit either file directly. The full text of the file becomes the system prompt sent to Claude. Changes take effect on the next bot restart. To use different prompts for different campaigns, either edit the files between campaigns or point to different files via the `SUMMARY_PROMPT_PATH` and `RECAP_PROMPT_PATH` environment variables.

| Variable | Default |
| --- | --- |
| `SUMMARY_PROMPT_PATH` | `data/prompts/crown_summary_prompt.txt` |
| `RECAP_PROMPT_PATH` | `data/prompts/crown_recap_prompt.txt` |

## Architecture

The codebase is small and split by responsibility:

- **`recorder.py`** handles voice receive. It decrypts DAVE E2EE audio, decodes Opus per user, and buffers PCM into roughly 2-second chunks before handing each chunk to a queue. Audio callbacks run on a background thread; queue puts are scheduled back onto the event loop via `loop.call_soon_threadsafe`.
- **`transcriber.py`** wraps faster-whisper. Each chunk is downsampled to 16 kHz mono and transcribed with a TTRPG vocabulary bias. Returns text and a confidence score.
- **`store.py`** is the persistence layer. Every database operation delegates to a plain synchronous helper via `asyncio.to_thread`, which keeps database work off the event loop and avoids the deadlocks that async SQLite drivers can introduce.
- **`bot.py`** ties it together. A long-running `asyncio` task consumes audio chunks from the queue, runs the blocking Whisper call in a thread pool with a 10-second timeout, and writes results to the store. It also defines the slash commands and all five session-list buttons.
- **`config.py`** loads configuration from environment variables. Channel IDs, prompt file paths, and output directories are all configurable here.

The flow at a glance:

```
voice channel -> recorder.py (decrypt, decode, buffer) -> queue
queue -> bot.py transcription loop -> transcriber.py (Whisper) -> store.py (SQLite)

/capture-list buttons:
  Export   -> plain transcript .md -> #prep-notes (file) + DM (ephemeral)
  Condense -> structured log .md via Claude -> data/logs/ (stored) -> #prep-notes (file) + DM (ephemeral)
  Recap    -> recap .md via Claude (from structured log) -> #session-log (file) + DM (ephemeral)
  Transcribe -> summary .md via Claude -> #prep-notes (file) + DM (ephemeral)
```

## Known limitations

- **discord-ext-voice-recv is inactive.** Voice receive on Discord is not officially supported by discord.py, and the `discord-ext-voice-recv` extension this bot relies on is effectively discontinued. It works today, but it is not actively maintained.
- **Risk of breakage on Discord protocol changes.** Because voice receive depends on undocumented behavior, a change to Discord's voice or DAVE protocol can break audio capture with little warning. Treat this bot as a useful tool rather than a guaranteed-stable service, and expect to pin dependency versions.
- **Transcription quality depends on the model and hardware.** Smaller Whisper models are fast but less accurate. Crosstalk, background noise, and accents all reduce accuracy. The transcript is a strong aid, not a perfect record.
- **Long sessions use multiple Claude calls.** Transcripts over 3000 words are processed in sections. This uses more API tokens and adds latency, but keeps the input within Claude's practical context limits for this use case.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the full version history.

## Version

v0.1.3
