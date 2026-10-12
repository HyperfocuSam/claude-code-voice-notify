# claude-code-voice-notify

Hear when Claude Code needs you, and which project it is about.

When a session finishes, a jingle plays, then a short voice line:

- "Billing is done."
- "Front end needs you." (the reply ended with a question, or Claude is waiting on a permission prompt)

Useful when several sessions run in different terminals: the voice tells you which one to look at.

## How it works

- A `Stop` hook and a `Notification` hook (permission prompts) call `notify.py`.
- The hook forks a detached player and exits at once, so Claude is never slowed down.
- The player waits a few seconds. If Claude carried on working (another Stop hook blocked, or a background agent woke the session), it stays silent.
- The project is picked by scoring your keywords over the last 40 turns of the transcript, recent turns weighted higher. No match: the fallback name ("Claude is done.").
- Clips are generated **once**, ahead of time. No API call happens per reply, so it is instant and free to run.
- Two sessions finishing together take turns instead of talking over each other.

## Setup

Requires Python 3.9+. Playback uses `afplay` (macOS) or `paplay` / `ffplay` / `mpg123` / `aplay` (Linux).

```bash
git clone https://github.com/<you>/claude-code-voice-notify
mkdir -p ~/.config/claude-voice-notify
cp claude-code-voice-notify/examples/{config,projects}.json ~/.config/claude-voice-notify/
# edit projects.json: your projects, their keywords, how to say their names
```

Generate the clips with any provider. Bring your own key; nothing is bundled.

```bash
cd claude-code-voice-notify
python3 generate_clips.py --provider say                                   # macOS voices, free
python3 generate_clips.py --provider elevenlabs --voice <voice_id>         # needs ELEVENLABS_API_KEY
python3 generate_clips.py --provider openai --voice alloy                  # needs OPENAI_API_KEY
python3 generate_clips.py --provider elevenlabs --voice <id> --env-file .env   # keys from a file
```

Add the hooks to `~/.claude/settings.json` (all projects) or `.claude/settings.json` (one project). See `examples/settings.json`; point the path at your clone. Restart Claude Code, or review them in `/hooks`.

## Configuration

Everything lives in `$VOICE_NOTIFY_HOME` (default `~/.config/claude-voice-notify`):

| File | Purpose |
|---|---|
| `config.json` | provider, voice, fallback name, line templates, jingle path, settle delay |
| `projects.json` | `key -> {keywords, spoken}`; respell names the voice gets wrong ("A.P.I.") |
| `clips/` | generated audio, `<key>__done.*` and `<key>__needs.*` |
| `muted` | if this file exists, the hook stays silent |

Added a project? Re-run `generate_clips.py`; existing clips are kept unless `--force`. Set `"jingle": null` for voice only.

## Limits

- Project detection is keyword matching, so it can guess wrong. Pick distinctive keywords.
- "Needs you" on `Stop` is a heuristic: the last reply ends with `?`.
- Background agents that are still running when Claude pauses can still produce an early "done".

## License

MIT
