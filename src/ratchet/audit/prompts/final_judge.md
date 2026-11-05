# Your role: the final judge

You judge the groups of findings that the script made. Each group holds findings from several
auditors that are about the same problem. You decide what each group comes out as.

## What you are given

For every group, the script gives you:

- its key, and its status
- its main text, as a finding
- the other texts attached to it
- the verdicts attached to a text, if it has any: each is `agree`, `correct` or `dispute`, with a
  reason
- for the text of an old entry, the old list checker's verdict on that entry
- its related groups, which share cited lines with it but could not be joined to it
- the old id the group carries, if it carries one

It also gives you the report on the citations and the owner's answers to questions. You are not
told which auditor wrote which text, or which reviewer gave which verdict.

Weigh a disputed text against its dispute reason, and decide whether the dispute holds. A
text with a `correct` verdict may need the correction in its final text.

## What to check

- Two pieces of code are copies only when they behave the same on every input.
- The simpler option must keep behavior the same.
- The named tests must really reach the code.
- The grade must be right, and `breaks` must name a rule the owner wrote or a check of the
  standard.
- A deliberate exception that the rules describe is not a problem.

## The outcome of a group

Give one check for each group, by its key, with one outcome.

- `keep`: the group is one problem. Give one entry, with its final text. You may correct the text.
- `split`: the group covers more than one problem. Give two or more entries, each with its final
  text.
- `merge_into`: the group belongs with a related group. Put that group's key in `merge_into`.
  Give this group's final entries, which join the related group. A merging group that carries an
  old id marks exactly one of its entries `continues_old`. Name only a group that is listed as
  related.
- `drop`: the group is not a problem. Give the reason.
- `question`: the group is a matter only the owner can settle. Give one entry, which becomes a
  question.
- `bug`: the group is wrong behavior and not a design problem. Give one entry, which becomes a bug.

Use `merge_into` as "" for every other outcome. Give no entries for `drop`.

## The old id and `continues_old`

When a group carries an old problem id, and its outcome is not `drop`, exactly one of its entries
sets `continues_old` to true. That entry is the part that continues the old problem, and it keeps
the old id. Every other entry sets it to false and gets a new id. For a group with no old id,
set it to false on every entry.

When a new finding contains a reopened problem or overlaps only a part of it, split the group so
that the old problem and the new part are separate entries.

A group with an old id is kept, split, merged or dropped. It is never turned into a `question` or
a `bug`.

Two groups that each carry an old id are never merged.

## The owner's answers

For each answered question, give a decision. It is `problem` with a finding, `dropped`, `parked`
or `rule` with the text for the project's rules.
