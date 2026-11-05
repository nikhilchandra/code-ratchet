# The general standard

These checks apply to any Python project. Each has a stable id, so a finding can name the check
it breaks. A check marked "Found by code" is also found by a script that reads the source, so
agents need not hunt for it line by line.

## Conflicts between checks and rules

- The project's own rules above come first. A check never overrides a rule the owner wrote.
- A deliberate exception that the rules describe is never raised as a problem.
- A rule that is too vague to apply is raised as a question for the owner, not as a problem.
- "Keep the duplication until three uses agree" applies to code that looks alike. The same fact
  written twice is always an S7 fault.

## Structure

- **S1. One job per module.** A module's job can be said in one sentence without "and". A module
  that mixes unrelated jobs is split, and one that only forwards to another is removed.
- **S2. No import cycles and no hidden imports.** Modules form a one-way chain of imports. An
  import inside a function to dodge a cycle hides the cycle, so it is a fault. Found by code.
- **S3. Every class earns its place.** A class serves a requirement that a function or an existing
  class does not already meet. A class that only connects two other classes is a fault.
- **S4. No pass-through code.** A method or function that only calls another one with the same
  arguments, or a wrapper around a single call, is removed.
- **S5. One place to change a value.** A default, limit, path or name that a reader may need to
  change is defined once, where the reader would look for it.
- **S6. Dependencies point one way.** A low-level module never imports a higher-level one, and a
  module uses only the parts it needs.
- **S15. Methods live with the data they read and write.** A method belongs to the class that owns
  what it reads and writes. A method that mostly reaches into another object's state belongs on
  that object.

## Duplication

- **S7. One fact, one place.** The same fact written twice, such as a list of names, a format or a
  rule, is a fault even when the two copies look different.
- **S8. Copies of behavior.** Two pieces of code are copies only when they behave the same on
  every input. Look-alike code with different behavior is not a copy.

## Unnecessary code

- **S9. No dead code.** Unused functions, parameters, imports, branches and options are removed.
- **S10. Inheritance only for a true "is a".** Sharing code between unrelated classes is done by
  calling a function or holding an object, not by a base class.
- **S18. Nothing speculative.** An option, hook or parameter that no caller uses, or that every
  caller sets the same way, is removed.

## State and effects

- **S11. No global state.** A `global` statement, a module-level mutable value that functions
  change, and a mutable default argument are faults. Found by code.
- **S12. Side effects stay at the edges.** Reading files, printing, the clock, the network and
  processes happen in a few places. The logic between them takes values and returns values.
- **S13. No hidden changes to arguments.** A function either returns its result or changes the
  object it is given, and its name and docstring say which.

## Errors and types

- **S14. Errors are not swallowed.** A bare `except` and an `except ...: pass` are faults. A caught
  error is handled, reported or raised again. Found by code.
- **S16. Named shapes for data.** Data that moves between functions has a named type. Loose
  dictionaries and tuples whose keys or positions must be remembered are faults.

## Readability

- **S17. No guessed names.** Dispatch through `getattr`, strings that name functions, and other
  calls whose target a reader cannot find through an import or a type are faults.
- **S19. Names say what a thing is.** A name describes its value or job in plain words, without
  abbreviations that a newcomer must decode.
- **S20. A junior developer can follow it.** A function does one thing at one level of detail,
  and a newcomer can tell where each value comes from and change it without breaking something
  far away.
- **S21. Comments are true and follow the rules.** Every comment and docstring follows the
  project's comment rules, and none is left untrue by a change.
