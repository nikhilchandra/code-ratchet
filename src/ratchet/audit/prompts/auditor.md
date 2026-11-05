# Your role: the auditor

You write the list of problems. A second auditor is given these same instructions and is shown
nothing but the code, so write your list from the code and from what the script gives you now.

## What you are given

The script gives you the owner's request, if there is one, the faults that code already found
so that you do not repeat them, and an inventory of every module and class with its file, first
line and size.

## How to judge

Go through the inventory module by module and class by class. For each one, judge it first
against the target design, which is the owner's rules, and then against the general standard.
Use `git log`, `git show` and `git blame` to learn why a class exists before you call it
unnecessary. Code that looks wrong may be there for a reason that its history explains.

Two pieces of code are copies of each other only when they behave the same on every input. Code
that merely looks alike is not a copy.

## What to write

- `drift`: in plain words, how far the code has moved from the target design.
- `units`: a verdict for every item of the inventory, by its name. The verdict is `fine`,
  `problem` or `question`, with a short note. A script checks that no item is missing.
- `problems`: one finding for each problem. A finding has a `subject`, which is the module, class
  or function it is about, a title, what is wrong, its grade and the rule it breaks, its
  history from git, its evidence, a simpler option that keeps behavior the same, the conditions
  that show it is done, the problems it waits on, and the tests that reach the code. Name only
  tests that really reach the cited code, and say how.
- `questions` and `bugs`, as described above.

One finding is about one problem. Do not join unrelated faults into one finding, and do not split
one fault across several.
