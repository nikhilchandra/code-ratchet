# code-ratchet reference

The full details behind the README: every command, how to set up a project, the records
repository, how to write rules.md, and the settings and output of audits and refactor runs.

## Commands

- **`ratchet refactor`** improves a project's structure while keeping its behavior the same.
  A planner picks the next step from the project's problem list. A super-planner checks the
  plan against `rules.md` and the pathway map of the functions it changes, and may ask for
  tests. A test writer writes those tests, which must pass on the current code and then stay
  frozen. An implementer makes the change. A map check sends any function, class or module
  code (`package.module.<module>`) the plan did not name back to the super-planner, and, when
  pytest-cov loads, a coverage check sends back any line the change adds to src that no test
  runs. The test suite and code checks guard the change, and a reviewer judges it. A step that
  passes is committed, and one that fails is undone.
- **`ratchet init`** sets up a project: it writes `.ratchet/config.toml` and `.ratchet/rules.md`
  from templates and makes `.ratchet` its own repository.
- **`ratchet audit -p "..."`** writes the problem list that `refactor` works through. Agents
  audit the project itself, unable to read `.ratchet`, and the owner approves the draft before
  it becomes a list.
- **`ratchet problems`** reads the problem lists and the history of each problem. It costs
  nothing.
- **`ratchet reopen P7 "reason"`** marks a finished problem as open again.
- **`ratchet feature`** and **`ratchet debug`**. *Planned for later.*

## Setting up a project

ratchet reads everything about a project from a `.ratchet` folder at the project's root.
Agents may never change anything in it.

```
.ratchet/
  config.toml                    the test command and the files agents may change
  rules.md                       what the code must become
  comments.md                    optional: the project's rules for comments
  problems-20261026-14h32m.md    a problem list, named after the minute it was made
  state/                         ratchet's own records
```

Run `ratchet init` once to create this folder. It never overwrites an existing `rules.md`.

A small `config.toml`:

```toml
test_command = ["uv", "run", "pytest", "-q"]
editable = ["src/**", "tests/**"]
frozen = ["**/*.feature"]

[refactor.caps]
rejections = 3
```

**Problem lists.** ratchet works on the newest list, chosen by the date and time in its name.
A new list never replaces an old one: write a new file, and carry over any problem from the
old list that is still unfinished. Each problem needs a heading that starts with its id:

```markdown
## P19. The controller is too big

Evidence, and a simpler option.
```

**The ledger.** Each problem list has its own ledger in `.ratchet/state/<list name>/`. It
records every job and each problem's status, so a second run on the same list carries on
where the first stopped, and a new list starts fresh.

## The records repository

`.ratchet/` is its own git repository, and the project's `.gitignore` holds `.ratchet/`. Inside
it, the problem lists, `config.toml`, `rules.md`, `comments.md` and every
`state/<list name>/ledger.json` are tracked. Everything else under `state/` is ignored.
ratchet commits there when it publishes a list, when `reopen` runs, when it writes the config
or `rules.md`, and when a refactor run ends.

`ratchet init` also moves an existing project over. It refuses when the working tree has
changes. When the project's git tracks `.ratchet`, it asks first, or needs `--yes` with no
terminal, and then untracks the folder, adds the ignore line and makes one commit. The ignore
line is added only when `.gitignore` lacks one. The files stay on disk in this clone, but
checking out an older commit can replace or delete them, and pulling that commit into another
clone deletes them there. In this clone, `git -C .ratchet restore .` brings them back from the
records repository. Another clone has none, so copy the `.ratchet` folder over from this one. A
second run changes nothing. `audit` and `reopen` refuse until init has run, and `refactor` only warns.

## Writing rules.md

`rules.md` says what the code must become. The template has five sections, with the essential
questions marked, and its first line is a marker that the audit refuses to start under:

```
<!-- ratchet: template. Delete this line once the essential questions are answered. -->
```

An optional TOML block lets a script check the rules with no agent. It holds `[parts]` (each
part maps to dotted module prefixes), `[may_use]` (which must name every part),
`[may_touch_outside_world]`, `[[exceptions]]` (with `from`, `to` and `why`) and `[skip]` for
generated or vendored code. Rules that audits settle with the owner are added under
"Decided in audits".

## Running an audit

```bash
ratchet audit -p "look at the views"      # audit the code, then ask the owner
ratchet audit --fresh                     # audit as a first audit, ignoring the old list
ratchet audit --approve                   # publish the draft of the audit that waits
ratchet audit --discard                   # throw that draft away
```

The first audit writes a list. Later audits are re-audits: they check every old problem
against the code as it stands, and keep each old id on the problem that continues it.

In a terminal the owner answers the audit's questions and then reads a summary of the draft,
with `[a]pprove, [e]dit, [d]iscard, [l]ater`. With no terminal the audit stops at its draft,
`.ratchet/state/audits/<audit name>/draft.md`, and waits for `--approve` or `--discard`. Only
one audit may wait at a time, and `--approve` is refused when HEAD has moved since the audit
began.

Agents work in the project. Their file tools are confined to it and denied `.ratchet`,
`.git` and the `hide` patterns. All but the duplicate judge may run read-only git (status, diff,
log, show, blame), and a command with `--no-index`, `--contents`, `--output`, `-C`,
`--git-dir`, `--work-tree`, `-c` or `--exec-path` is refused before it runs. After each turn
its log is checked: a call that got through halts the audit, and a refused call is only noted.

When `.ratchet` appears anywhere in the project's git history (any branch, tag, stash or
reflog entry), a re-audit skips the blind audit and runs the auditor in its place beside the
old list checker. It shows the note "The blind audit was skipped. .ratchet was committed to git
history in <commit>, so the old problem lists can still be read there." and marks the
published list "Not blind". A first audit shows no note.

**One run at a time.** Only one graph, refactor or audit, runs on a project at a time, under
the lock file `.ratchet/state/graph.lock`. A second run is refused with a message that names
the job, such as "Audit job-3 is waiting for your answers. Finish or discard it first." An
audit waiting at its questions or draft keeps the lock. A run stopped by an error or Ctrl-C
undoes its work, marks the job halted and releases the lock. A run that died without a word
(a power cut, a killed process) is rolled back by the next run, which says "The last refactor
(job-12) stopped without finishing. Its changes were undone." (for an audit, "It was
halted.") and starts clean. Nothing is resumed.

**The format of an entry.** A published list gives each problem a heading, then field lines,
then its text, evidence, simpler option, "Done when" and tests:

```markdown
### P7. Strip handling is written twice

Status: open
Sources: L1, A2
Grade: stated (Breaks: "a fact is written in one place")
Subject: views.strip.draw
Carried from: problems-20261102-14h05m P7
After: P3
```

`Status` is new, open, reopened or blocked. `Grade` is stated, inferred or standard. `Carried
from` appears only for an entry that continues an old one, and `After` only for one that waits
on another. The list also holds "Questions for the owner" (ids `Q1`, each with an `Answer:`
line that the next re-audit reads), "Dropped" (ids `D1`) and "Bugs noticed".

**Settings.** The `[audit]` section of `config.toml`:

```toml
[audit]
hide = []                       # path patterns denied to the agents' file tools in the project
max_pairs = 40                  # most pairs the duplicate judge is asked about
max_cost_usd = 40.0             # the most one audit may spend, at list price

[audit.auditor]                 # opus, $10, 3600 s
[audit.rules_reviewer]          # opus, $6, 2400 s
[audit.standard_reviewer]       # opus, $6, 2400 s
[audit.old_list_checker]        # opus, $6, 2400 s
[audit.duplicate_judge]         # sonnet, $1, 600 s
[audit.final_judge]             # opus, $6, 2400 s
```

Before each agent step the audit checks that what it has spent plus that step's budget stays
within `max_cost_usd`, and halts when it would not.

## Reading problems

```bash
ratchet problems                # the newest list: id, title, status and grade
ratchet problems --all          # every list, oldest first
ratchet problems P7             # the full entry from the newest list that holds P7
ratchet problems P7 --history   # one line for each event, across every list it was carried in
ratchet reopen P7 "the vote is repeated again"
```

Each problem's ledger records dated events: opened, carried, job started, committed, done,
sent back, abandoned and reopened. A history follows `Carried from` back through the older
lists, so it reads as one timeline.

## Running a refactor

On a clean working tree whose test suite passes:

```bash
ratchet refactor --max-jobs 5             # up to five jobs, each one commit or undo
ratchet refactor --focus P19              # ask the planner to take P19 first
```

The run stops when the planner finds nothing left, after `--max-jobs` jobs, or on any error.
Each job's prompts, diffs, test logs and reports are kept in the list's state folder. When the
run ends, its ledger is committed in the records repository, and ratchet prints how many commits
have landed since the list was published. Once three or more jobs have been committed on the
list, across every run on it, ratchet also suggests `ratchet audit`.

**Settings.** The `[refactor]` sections of `config.toml`:

```toml
[refactor.caps]
rejections = 3                  # rejections in a row after which a job is undone
test_failures = 3               # test failures after which a job is undone
sendbacks = 2                   # blocked or replanned jobs after which a problem is set aside
revisions = 2                   # sendbacks from the super-planner to the planner
test_rewrites = 2               # refused test writes sent back to the test writer

[refactor.super_planner]        # opus, $3, 1800 s
[refactor.test_writer]          # sonnet, $5, 3600 s
```

The next sendback past `revisions` or `test_rewrites` abandons the job, and the run goes on.
