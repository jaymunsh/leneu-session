# leneu-session

Keep long Claude Code work moving across `/clear`, without writing handoffs by hand.

한국어 문서: [README-ko.md](README-ko.md)

Long tasks outlive a single context window. Waiting for auto-compact means summarizing at the moment you have the most to lose, and writing a handoff yourself means remembering to do it. This plugin does the bookkeeping while you work: it records every turn, saves a structured checkpoint at regular intervals, and restores the latest one when you run `/clear`.

```
/leneu-init                        once per project
  ... work as usual ...            turns and checkpoints are recorded for you
"/clear is recommended"            the plugin suggests it, you decide when
/clear
.                                  any short input, and work continues
```

> **Status: 0.1.0, early.** The hooks, checkpoints and resume logic are covered by a self-test and were exercised against real `claude -p` runs. The full interactive flow (`/clear`, then `.`) has not yet had a long real-world trial, so please report anything odd.

## Contents

- [Install](#install)
- [The flow in detail](#the-flow-in-detail)
- [Commands](#commands)
- [Configuration](#configuration)
- [What gets created](#what-gets-created)
- [How it works](#how-it-works)
- [Safety and privacy](#safety-and-privacy)
- [Known limitations](#known-limitations)
- [Coming from jay-session](#coming-from-jay-session)
- [Turning things off](#turning-things-off)
- [Development](#development)

## Install

```bash
claude plugin marketplace add jaymunsh/leneu-session
claude plugin install leneu-session@leneu-session
```

Then run `/reload-plugins`, since commands and hooks are loaded at that point and `/clear` does not reload them. You install it once, not per repository. It requires `python3` (3.8 or newer) and has no other dependencies.

To update later:

```bash
claude plugin marketplace update leneu-session
claude plugin update leneu-session@leneu-session
```

The first line refreshes the cloned repository and the second refreshes what is installed. Run both, then `/reload-plugins`.

To try it without installing:

```bash
git clone https://github.com/jaymunsh/leneu-session.git
claude --plugin-dir ./leneu-session
```

**Use the full command names, or pick from the menu.** Type `/leneu` and choose the command, or type `/leneu-session:leneu-stop` in full. Short names such as `/leneu-stop` were unreliable in testing: one attempt returned `Unknown command`. Check the running command before pressing Enter. This README uses short names for readability.

## The flow in detail

```
── Session 1 ──────────────────────────────────────────
  > /leneu-init                 turn it on for this project

  (you work)                    each turn is appended to today's log

  ~100K tokens of new context   Claude writes a checkpoint in one short
                                extra turn and replies "체크포인트 저장"
                                ("checkpoint saved")

  past 30% of the window        the checkpoint reply also recommends /clear

  > /clear                      you type this, at a point that suits you

── Session 2 ──────────────────────────────────────────
  (the latest checkpoint and every turn after it
   are already in context)

  > .                           Claude checks the repository still matches,
                                then carries on with the next action
```

**You can run `/clear` whenever you like.** The recommendation is only a prompt. The end of a task is the best time, even if no recommendation has appeared.

**What you still do yourself:** type `/clear`, and send one message afterwards. No hook can clear a session, and a new session waits for input before Claude responds. If your message is unrelated to the earlier work, Claude ignores the restored record.

### What a checkpoint contains

Checkpoints list what the next session needs to act, not a story of what happened. Claude writes these sections, skips any that don't apply, and keeps the total under 6,000 characters:

- The current task's requirements and your corrections, **quoted verbatim**
- Confirmed facts: paths, commands, values, errors
- Changed files and whether they were verified
- Decisions: what was chosen, what was rejected, and why
- Dead ends: what was tried and why it failed
- Next actions: one to three, with file paths

Dead ends matter most. `git log` tells the next session what changed, but only this record tells it which approach was abandoned.

### What happens on each kind of session start

| Start | What Claude receives |
|---|---|
| `/clear` | The latest checkpoint or handoff, plus the turns after it |
| New session | The same, with its age. If it's over 24 hours old, Claude asks whether to continue before acting on it |
| After compaction | Only the location of the record, since the compaction summary is already there and work is still in progress |
| Resume or fork | Nothing, since the conversation is already present |
| `claude -p` | Nothing. Turns are still logged, but no checkpoints are requested |

Injected text is capped at 8,000 characters. An oversized checkpoint keeps its beginning and end, because the next actions come last, and gets a note pointing to the full record.

### Asking about past work

No command is needed. The bundled skill handles these:

- *"How did we fix that timeout last week?"* searches the logs.
- *"What did I get done this week?"* summarizes by topic.
- *"Log this one"* adds a manual entry for something the hook can't see, such as a reason or a constraint.
- *"Where did I leave off?"* finds the latest record, checks it against the repository, and briefs you.

## Commands

| Command | What it does |
|---|---|
| `/leneu-init` | Turns the plugin on for this project. Re-enables a stopped project and keeps its records. |
| `/leneu-stop` | Turns it off. Records are kept. |
| `/leneu-handoff [focus]` | Writes a handoff now and gives you a prompt to paste. |
| `/leneu-brief [--test]` | Writes a document explaining the whole project to an outside reader. |

### `/leneu-handoff`: a manual handoff

Checkpoints are automatic, so you only need this when you want a record right now, or when you're continuing somewhere the hooks aren't running, such as another agent or machine. The final output is one line, with an absolute path:

```
/Users/me/projects/foo/.leneu-session/handoffs/handoff-20261109-1430.md 읽고 검증한 뒤 이어서 진행해줘.
```

(The prompt is Korean for "read this, verify it, and continue.") You can narrow it with an argument: `/leneu-handoff auth refactor only`. A new handoff also resets the checkpoint interval, so you won't get a duplicate checkpoint right after it. Running it in a project that was never initialized creates the folder but leaves automatic recording off.

### `/leneu-brief`: explaining the project to someone else

`/leneu-handoff` passes on *the work in progress*. `/leneu-brief` explains *the project itself* to a reader who has never opened the repository, and writes `.leneu-session/project-brief.md`.

- **It stands alone.** The reader may not have the repository, so it doesn't rely on pointers like "see the README".
- **It labels its evidence.** Each claim is marked `[실행]` (ran it), `[코드]` (read the code) or `[기록]` (from logs or conversation), so a guess doesn't get passed along as fact.
- **It uses your history.** Decisions and dead ends come from the logs and handoffs, then get checked against the repository. A decision that has since been reversed is recorded as "it used to be this, now it's that".
- **It updates in place.** Later runs read only what changed since the recorded commit. Architecture decisions and pitfalls are always carried forward, because they can't be recovered from the repository.
- **`--test` checks for gaps.** A subagent reads only the brief, tries to answer how to run the project and where to start, and any gaps get filled in. This costs extra tokens, so it's off by default.

## Configuration

Create `.leneu-session/config.json` if you need to. Every key is optional, and invalid values fall back to the defaults.

| Key | Default | Meaning |
|---|---|---|
| `context_window` | `1000000` | Context window size in tokens. **It can't be detected automatically.** |
| `clear_recommend_pct` | `30` | Recommend `/clear` at the end of the first turn past this percentage of the window, then at each later checkpoint. |
| `checkpoint_every_pct` | `10` | Request a checkpoint each time context grows by this percentage of the window. |

With the defaults, a checkpoint is requested about every 100K tokens, and the recommendation appears at the end of the first turn past 300K. Hooks only run between turns, so one long turn can overshoot the threshold.

**If you use a 200K window, change this.** Otherwise auto-compact may fire before any recommendation appears:

```json
{"context_window": 200000, "clear_recommend_pct": 45}
```

### Why these defaults

Checkpoints are written at intervals because the record has to exist *before* something goes wrong, and an interrupted session may never get another chance. They're written by the session already running because it holds the full context and reads it from the prompt cache, at roughly a tenth of the input cost of starting a separate one.

## What gets created

Nothing until you run a command that needs it.

```
<your project>/.leneu-session/
├── .gitignore              # contains *, so the whole folder stays out of git
├── logs/YYYY-MM-DD.md      # turns and checkpoints, append-only, kept forever
├── handoffs/handoff-*.md   # from /leneu-handoff, newest 10 kept
├── project-brief.md        # from /leneu-brief, updated in place
├── config.json             # optional
├── hook-errors.log         # hook failures, reported at next session start
├── hook-errors.old.log     # failures already reported
├── disabled                # present while /leneu-stop is in effect
├── .state.json             # read position and token baseline per session
└── .pending-checkpoint.md  # brief stop for a checkpoint on its way to the log
```

A log looks like this, with its labels in Korean (`요청` request, `결과` result, `변경` changed, `도구` tools):

```markdown
## 2026-11-09 14:02 turn
**요청:** Fix the bug in add()
**결과:** Changed the subtraction to addition. Tests pass.
**변경:** /path/to/calc.py
**도구:** Read, Edit, Bash

## 2026-11-09 14:15 checkpoint tokens=147000 session=6f74a221
**원본:** /path/to/transcript.jsonl
### 다음 액션
1. ...
```

Logs split by day and are never rewritten. Each turn keeps up to 400 characters of the request and the last 600 of the result, because conclusions come at the end. The `**원본:**` line points to the full transcript if you need more.

## How it works

One Python script, four hooks.

| Hook | Fires when | What it does |
|---|---|---|
| `Stop` | Each turn ends | Logs new turns, moves a finished checkpoint into the log, and asks for a new checkpoint when one is due |
| `SessionStart` | A session starts | Recovers a checkpoint left behind by an interrupted session, injects the context from the table above, reports hook errors, and enables new git worktrees |
| `PreToolUse` (`Write`) | Before a file write | Auto-approves only the plugin's own checkpoint and handoff files |
| `PreCompact` | Before compaction | Notes where the uncompacted transcript lives |

Some design decisions worth knowing:

- **Context size comes from the transcript.** Hooks aren't told the context percentage, so the script sums the last main response's `input_tokens`, `cache_creation_input_tokens` and `cache_read_input_tokens`. Subagent usage is excluded, and a drop in the total is treated as compaction.
- **Checkpoints use `additionalContext`, not `decision: block`.** A block gets stored in the transcript as a hook error, and models then wrote about that "error" in their checkpoints.
- **Two steps keep the log format safe.** Claude writes to a temporary file, and the hook adds the header and timestamp when moving it into the log. Any `#` or `##` headers in the body get demoted so they can't look like new log sections.
- **Hooks never block your work.** Every failure is caught, logged and reported later, and the script always exits 0. A failed `/clear` restore is reported right away.
- **A malformed transcript doesn't stop recording.** The read position is saved even when a step fails, so the same turns aren't logged repeatedly.
- **If the read position is unknown, only the last turn is logged.** This covers a damaged state file, turning the plugin on mid-session, and forks. Logs can't be edited, so a missing turn is better than duplicated or unwanted ones.
- **The script waits briefly for the transcript.** The last response may not be on disk when `Stop` fires, so it waits up to 2 seconds. Otherwise token counts lag by a turn and results come out empty.
- **Git worktrees are handled for you.** The folder is gitignored and so missing from new worktrees. If the main checkout has the plugin on, a new worktree gets its own folder and a copy of the config. Worktrees don't share logs.
- **The project is found by walking up from the working directory,** stopping at the nearest `.git` or your home folder.

## Safety and privacy

- **Off by default.** Nothing is recorded in a project until you run `/leneu-init`. It refuses to run in your home folder, where it would apply to every folder below it.
- **Stays local.** The plugin sends nothing anywhere itself. The restored record does enter your Claude conversation as context.
- **Excluded from git.** The folder contains its own `.gitignore`. Logs can quote your prompts and results, so keep it that way unless you mean to share them.
- **Narrow auto-approval.** Only `.pending-checkpoint.md` and direct `handoff-*.md` children of `handoffs/` are approved, and paths are resolved first so symlinks can't point elsewhere. Your own deny and ask rules still apply.
- **Limited secret masking.** Hook-written records mask common formats: `sk-…`, `ghp_…`, `github_pat_…`, `AKIA…`, `Bearer …` and `xoxb-`/`xoxp-…`. A password pasted without a recognizable format is not masked. Manual handoffs rely on an instruction to Claude not to include secrets.
- **A committed `.leneu-session/` turns the plugin on.** The folder's presence is the switch, so a repository that commits it activates the plugin and has its contents injected, much like a `CLAUDE.md`.
- **Your settings are untouched.** Hooks ship inside the plugin, and `settings.json` isn't changed.

## Known limitations

- **`/clear` is manual.** No hook can run it.
- **Resuming takes one input.** A new session waits for you, so `.` or any short message is required.
- **Checkpoints are visible and cost tokens.** Each one adds a short turn. Adjust `checkpoint_every_pct` if that's too frequent.
- **Concurrent sessions aren't distinguished.** With the same folder open in several terminals, `/clear` may read another terminal's checkpoint. Checking the repository on resume reduces the risk. Subagents and worktrees aren't affected.
- **`context_window` is manual.** The hook input doesn't include it.
- **Non-interactive detection relies on an undocumented value.** `CLAUDE_CODE_ENTRYPOINT` is `sdk-cli` under `claude -p`. If it's missing, the session is treated as interactive.
- **Interrupting and then clearing loses that turn.** `Stop` doesn't fire after Esc, so running `/clear` right after leaves the interrupted turn out of the log.
- **Other agents aren't supported yet.** Claude Code comes first, with Codex and others possible later. `/leneu-handoff` already produces a plain file any agent can read.

Skill and command instructions are written in Korean. Claude replies in whatever language you use, though some fixed lines and log labels stay Korean.

## Coming from jay-session

leneu-session succeeds [jay-session](https://github.com/jaymunsh/jay-claude).

| | jay-session | leneu-session |
|---|---|---|
| Handoff | Manual `/jay-new` | Automatic checkpoints, with `/leneu-handoff` when you want one |
| After `/clear` | Injects a path, then Claude reads the file | Injects the record and later turns directly |
| `/clear` timing | Your call | A recommendation at a configurable threshold |
| Log | Monthly `devlog.md` | Daily files, always part of the resume |
| Location | `.claude/session/` | `.leneu-session/` |

Disable jay-session when you switch, because running both means overlapping hooks. Existing `.claude/session/` files aren't migrated and stay where they are.

```bash
claude plugin disable jay-session@jay-claude
```

## Turning things off

```
/leneu-stop                 stop recording here and keep the records
/leneu-init                 turn it back on
```

Turns made while it's off are never logged, including after you turn it back on. To delete everything for a project, remove its `.leneu-session/` folder. To remove the plugin:

```bash
claude plugin uninstall leneu-session@leneu-session
```

This removes the hooks and commands. Files already written to your projects stay.

## Development

```bash
python3 scripts/session_hook.py --selftest
claude plugin validate .
```

All the logic and its tests live in `scripts/session_hook.py`. The design is in [docs/superpowers/specs](docs/superpowers/specs/2026-11-09-leneu-session-design.md) and the implementation plan in [docs/superpowers/plans](docs/superpowers/plans/2026-11-09-leneu-session.md), both in Korean.

## License

MIT
