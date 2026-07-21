## Synthermia At A Glance

Synthermia is a terminal MIDI visualizer with optional SoundFont playback. Run it with the project's tool environment:

```bash
mise x -- uv run main.py
```

Running without a MIDI opens the curses config screen. `main.py` owns CLI options and startup; it should stay a thin coordinator.

### Find The Right Code

| Need | Start here |
| --- | --- |
| CLI option, loading flow, startup errors | `main.py` |
| MIDI parsing, tempo conversion, note/control spans | `synthermia/midi.py` |
| Large-file packed loader and background loading | `synthermia/packed.py`, `src/lib.rs` |
| Playback loop, seeking, pedal spans, live MIDI | `synthermia/playback.py` |
| Frame drawing and terminal colors | `synthermia/render.py` |
| FluidSynth, channel routing, audio output | `synthermia/audio.py` |
| Config screen, pickers, profiles, MP3 export | `synthermia/tui.py` |
| Saved settings and shared config helpers | `synthermia/config.py` |
| SoundFont preset scanning and instrument labels | `synthermia/soundfont.py` |
| Shared data classes and constants | `synthermia/models.py` |

The normal file-loading path is `main.py` -> `packed.load_selected_playback_data()` -> either Python MIDI parsing or the Rust packed loader -> `playback.playback()` -> `render.render_frame()` and optional audio. Large files may keep building in the background, so changes to loading data must work for both complete and partial packed sources.

### Feature Rules

- Add a user setting in all of its real homes: CLI parsing in `main.py`, saved config in `config.py`, TUI state/field/save/return in `tui.py`, and every load or export path that uses it.
- Keep Python and Rust MIDI results aligned. If a change affects parsing, spans, diagnostics, or packed queries, update `synthermia/midi.py` and `src/lib.rs` together.
- `tui.py` intentionally keeps the config screen helpers inside `run_config_screen`. Extend that flow there unless the behavior is also used outside the TUI.
- Instrument options include their SoundFont path internally. Do not reduce them to bank/program alone: two SoundFonts can have the same program number.
- The render loop is performance-sensitive. Do loading or whole-file work before playback or in the packed loader, not once per frame.

### Local Data And Checks

- `.synthermia_last_config.json` and `.synthermia_profiles.json` are personal settings. `midis/`, `soundfonts/`, `target/`, and `synthermia_native.so` are local or generated. Never add them to Git.
- Use `mise x -- uv run main.py --help` for a quick packaging/import check. For Python-only edits, also run `python3 -m py_compile main.py synthermia/*.py`.
- For Rust changes, run `cargo check` or `cargo build` through the active Rust toolchain, then test the packed path with `mise x -- uv run ...`.

## Understand the Goal

* If the user gives examples or specific instances, always ask what the goal or proper generalization of those examples is. Write it down in one or two sentences before moving ahead. If it is ambiguous or you cannot quite tell, ask the user instead of charging ahead with changes.
* When making changes, always keep their goal in mind, not just the specific subtask you are working on. Use the goal to decide when to expand or simplify the code.

## Engineering Approach

High-quality code solves the requested problem correctly with the fewest necessary concepts and execution paths.

Always seek the minimal coherent change that fixes the problem. Stay on the side of simplifying instead of adding more and more lines of code. Prefer the smallest coherent design over the smallest possible diff.

When fixing a bug, first ask, “What code is causing this?” instead of “What code should I add?” Identify the root cause and fix it as succinctly as possible while keeping the code maintainable and understandable. Prefer removing a cause over adding an exception; do not build more guardrails and workarounds around the root issue.

Do not write defensive or fallback code that hides programming errors; it does not solve the root problem and only increases debugging cost. Validate untrusted input at the boundary without repeatedly checking valid internal state. Fail loudly and immediately when an internal invariant is broken.

Code should:

* fit the existing design and put behavior where a reader would expect to find it;
* have one clear source for each rule or piece of state;
* express the program's real cases directly;
* use ordinary control flow and established project conventions;
* be understandable without comments that merely translate the code;
* test observable behavior, including only failure cases that can really occur;
* prefer a direct implementation over a reusable abstraction until there are multiple real uses.

Before finishing, ask:

* Can any new code, branch, type, or layer be removed?
* Does every handled case occur in the real program?
* Is this behavior defined in exactly one place?
* Does the change make the program's overall design clearer?

## Communication

Prefer simple, concrete, plain language. Use jargon only when it adds meaning or significantly shortens the sentence. For example, “the module's contract” conveys “the agreed-upon inputs and outputs of the module” more briefly. “Identify the authoritative owner of this feature” adds nothing to “Find the code that controls the feature” and should be avoided.

Be concise and to the point. Do not write fifteen paragraphs for a simple question when one or two convey the important information. Expand when the user asks or when necessary to avoid leaving out important information. Avoid repetition, especially within the same text.

This applies to documentation as well. Write only what is necessary to understand the point easily. If relevant extra detail would obscure the main document, put it in another file and link to it. The extra file is not an excuse to be overly wordy.

## Humility

If you are unable to locate the cause of a bug or properly implement a feature while following these principles, state that clearly to the user.

## Commits

Commit every meaningful change with a clear message. Use [Conventional Commits](https://www.conventionalcommits.org/); a type prefix is required on every commit.

| Type       | When to use                                         |
| ---------- | --------------------------------------------------- |
| `fix`      | Any bug fix—root cause corrected in code            |
| `feat`     | New behavior visible to users or callers            |
| `refactor` | Code restructure with no behavior change            |
| `chore`    | Tooling, dependencies, or submodule pointer updates |
| `docs`     | Documentation only                                  |
| `style`    | Formatting only, such as Prettier or whitespace     |
| `test`     | Tests only                                          |
| `ci`       | CI/CD pipeline changes                              |

### Commit Body

The subject line names the change. The body explains the *why* and *how*—the information that cannot be recovered from reading the diff alone. Write as many paragraphs as the change warrants; never pad with filler, but never truncate a real explanation to keep it short.

A good body answers:

* What was wrong or missing, and why it mattered?
* What was the root cause, not just the visible symptom?
* What does the fix do mechanically, and why is that approach correct?
* Was any non-obvious invariant, edge case, or alternative considered and rejected?

One-line bodies are acceptable only for genuinely trivial changes, such as formatting, a typo, or a single-word rename. A body is required for any logic change, bug fix, or new behavior.

### `fix` Commits

* Every bug fix must use `fix:` or `fix(scope):`; there are no exceptions.
* Describe what was wrong, not just what changed: `fix(erp): missing await caused sync to silently drop items`, not `fix(erp): add await`.
* The body must state the root cause explicitly. “Fixed the bug” is not a root cause.
* If the fix is speculative because the root cause is unclear, use `fix(tentative):` and explain the hypothesis.

## Documentation

Document information that a maintainer or user needs and cannot easily infer
from the code. This includes public behavior, setup and operating instructions,
architectural constraints, important invariants, and the reasons behind
non-obvious decisions.

Do not narrate the code, explain self-evident behavior, repeat information from
another document, or create a new file when an existing one is the natural
home. Keep one source of truth and link to it where necessary.

Update documentation when a change affects public behavior, APIs, configuration,
commands, architecture, or an established invariant. Remove or correct
documentation made stale by the change.

Comments should explain why something is necessary, not restate what the code
does.

Before adding documentation, ask:

- Who needs this information, and what will it help them do?
- Is it already documented elsewhere?
- Would clearer code, naming, or tests make the explanation unnecessary?
- Is this the shortest complete explanation?
