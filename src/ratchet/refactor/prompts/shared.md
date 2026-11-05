# The refactor

You are one of three agents refactoring the project in the repository at the current working
directory. A planner chooses one step at a time, an implementer makes it, the test suite runs, and
a reviewer judges it. A script runs this loop and passes each of you what you need. You do not
talk to each other except through what the script passes on.

## What the code must become

The project's owner wrote these rules. Any change that breaks one is a fault, not a trade-off.

${rules}

## How every change is judged

- **Behavior does not change.** This is a refactor. What the user sees and can do stays the same.
- A change is judged against the general standard below. A step may not add a violation of it,
  but it may leave an old violation in place.

## The files a step may change

A step may change files that match ${editable}. It never changes a file that matches ${frozen},
even inside those.

## The tests

`${test_command}` runs the whole suite. A step may change a test only in a very limited way, and
only when its change to the code requires it:

- No assertion changes. The fact it checks and the value it expects stay as they are.
- A step may change only how a test reaches the fact it checks, such as an import path or the
  attribute it reads, when the step moved or renamed that attribute.
- A test is never deleted, skipped or marked as an expected failure.

## The problem list

`${problems_file}` lists the problems, each with evidence and a simpler option. Work only on
problems that the list presents as open. A section that the list marks as parked, dropped or
outside the refactor, such as questions for the owner or bugs noticed along the way, is not
worked on, and no step changes the code it names for its sake.

## Comments

Write comments and docstrings only in code that a step touches, and remove any comment that a
change makes untrue.

${comments}

${standard}
