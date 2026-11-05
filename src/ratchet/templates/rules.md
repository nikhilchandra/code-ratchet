<!-- ratchet: template. Delete this line once the essential questions are answered. -->
# Rules for this project

This file says what the code must become. Every agent reads it before it reads the code.
Answer in plain sentences. A question marked (essential) must be answered before an audit can
run. A question marked (optional) may be left alone. Write "Don't know" when you do not know,
and the agents will raise it as a question instead of guessing.

## 1. What the project is for

- (essential) In two or three sentences, what does this project do, and who uses it?
- (optional) What will it never do?

## 2. The parts

- (essential) Which parts should the code be divided into? Name each part and say what it is
  responsible for.
- (optional) Which part is the most likely to change, and which should almost never change?

## 3. Who may use whom

- (essential) For each part, which other parts may it use? A part may always use itself.
- (optional) Is there a part that nothing else may use?

## 4. The outside world

- (essential) Which parts may touch the outside world: files, the network, a database, the
  clock, the screen, or the user's input?
- (optional) Which parts must never do so, and so must be testable with no setup?

## 5. Deliberate exceptions and code to leave alone

- (optional) Where does the code break the rules above on purpose? Name the two sides and say
  why.
- (optional) Which code is generated or copied from elsewhere, so that it should not be
  judged?

## Choosing a pattern

When a part is hard to describe, pick the first line that fits.

- It holds data and the rules about that data: a plain class that keeps its methods with its
  data.
- It does one job with no state: a module of functions.
- It joins other parts and holds no rules of its own: a thin layer that only calls them.
- It touches the outside world: keep it small, and keep the decisions elsewhere.

## Machine-readable rules (optional)

The block below lets a script check the rules above without an agent. Replace the example names
with real ones, or delete the block. Each part maps to one dotted module prefix or a list of
them, and the most specific prefix decides which part a file belongs to.

- `[parts]` names each part.
- `[may_use]` must name every part. A part may always use itself.
- `[may_touch_outside_world]` lists the parts that may do so, and may add `modules` to the
  built-in list below.
- `[[exceptions]]` ties each deliberate exception from section 5 to the code check, with
  `from`, `to` and `why`.
- `[skip]` lists the modules of generated or vendored code.

The built-in list of effects is:

- imports of `subprocess`, `socket`, `http`, `urllib`, `requests`, `httpx`, `sqlite3`,
  `shutil`, `threading`, `tkinter`, `PySide6`, `PyQt5` and `PyQt6`
- calls to `open`, `print` and `input`
- calls of `.now()`, `.today()` and `time.time()`
- the file methods of `pathlib`

A plain `import os`, `import pathlib` or `import datetime` is not counted, since these are mostly
used for types and paths.

```toml
[parts]
core = "example.core"
cli = "example.cli"

[may_use]
core = []
cli = ["core"]

[may_touch_outside_world]
parts = ["cli"]

[skip]
modules = []
```

## Decided in audits

Rules that audits settled with the owner are added below.
