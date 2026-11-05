# Scenarios

Each heading is one start-to-finish path through ratchet. The steps name the functions on the path in the order they run. The "Tests:" line names the tests that run the path from its entry function to its last step, or says "none yet". Where a test covers only part of a path, the line says which part.

## ratchet init

1. `ratchet.cli.main` sends the command to `ratchet.cli.run_init`, which asks the owner questions only when there is a terminal.
2. `ratchet.init_project.init_project` refuses a folder that is not a git repository or has uncommitted changes.
3. `ratchet.init_project._write_templates` writes config.toml and rules.md, never over a file that exists.
4. `ratchet.records.init_records` makes .ratchet its own repository.
5. `ratchet.init_project._add_ignore_line` adds .ratchet to .gitignore only when .gitignore lacks a line for it, and commits it. A project that tracks .ratchet is moved over first, with the owner's consent. The question and the "stopped tracking" line say how to restore the files: `git -C .ratchet restore .` in this clone, or copy the folder over in another clone.
6. `ratchet.records.commit_records` makes the first records commit.

Tests: tests/test_init_project.py::test_the_init_command_prints_what_it_did (through `ratchet.cli.main`, and a second run changes nothing), tests/test_init_project.py::test_a_new_project_gets_files_a_repository_and_an_ignore_line (through `ratchet.init_project.init_project`), tests/test_init_project.py::test_the_init_command_moves_a_tracked_project_and_prints_the_restore_note (through `ratchet.cli.main`), tests/test_init_project.py::test_the_warning_says_how_to_restore_the_records, tests/test_init_project.py::test_a_yes_run_reports_how_to_restore_the_records, tests/test_init_project.py::test_tracking_needs_consent_when_there_is_no_terminal and tests/test_init_project.py::test_a_tracked_project_that_already_ignores_ratchet_gets_one_ignore_line (all four through `ratchet.init_project.init_project`).

## The first audit

1. `ratchet.cli.run_audit` loads the project with `ratchet.project.load_project` and calls `ratchet.audit.audit_run.start_audit`.
2. `ratchet.audit.audit_run.start_audit` refuses while another audit waits, while rules.md is the template, or before init, then builds the graph with `ratchet.audit.audit_graph.build_audit_graph`. It deletes the copies that earlier halted audits left behind, and deletes its own copy when it halts. Tests: tests/test_audit_run.py::test_a_halted_audit_deletes_its_copy, tests/test_audit_run.py::test_an_audit_sweeps_the_copies_of_earlier_halted_audits and tests/test_code_copy.py::test_remove_copy_refuses_a_folder_that_is_not_a_ratchet_copy.
3. `ratchet.audit.audit_graph.build_audit_graph.prepare` checks for a clean tree and an ignored .ratchet, writes the code findings and the inventory, and makes the copy of the code with `ratchet.audit.code_copy.make_copy`. A history that `ratchet.audit.code_copy.make_copy` refuses, or a toml block in rules.md that `ratchet.target_design.read_target_design` cannot read, stops the audit, and `ratchet.cli.run_audit` prints one `ratchet: ...` line and exits with 1 (tests/test_cli.py::test_audit_that_refuses_the_history_prints_one_line and tests/test_cli.py::test_audit_with_a_malformed_rules_block_prints_one_line).
4. `ratchet.audit.audit_graph.build_audit_graph.audit` asks the auditor. `ratchet.audit.audit_graph.build_audit_graph.review` asks list reviewers A and B together.
5. `ratchet.audit.audit_graph.build_audit_graph.group` makes items, groups them, and asks the pair judge about the pairs that rules cannot settle.
6. `ratchet.audit.audit_graph.build_audit_graph.check_citations_step` fixes or removes citations that do not match the copy.
7. Questions gate: `ratchet.audit.audit_graph.build_audit_graph.questions` writes questions.md and the job waits. `ratchet.audit.audit_run._answer_questions` collects answers with `ratchet.owner_terminal.ask_questions`, or resumes with none when there is no terminal.
8. `ratchet.audit.audit_graph.build_audit_graph.check` asks the checker, applies its verdicts and checks the citations again. It halts with an `AuditError` when the copy went missing while the job waited at the questions gate, tested by `tests/test_audit_graph.py::test_a_copy_deleted_at_the_questions_gate_halts_the_check`.
9. Draft gate: `ratchet.audit.audit_graph.build_audit_graph.draft` gives out ids and writes draft.md, then the job waits. `ratchet.audit.audit_run._approve_draft` shows the draft with `ratchet.owner_terminal.review_draft`. With no terminal the owner runs `ratchet audit --approve`, which calls `ratchet.audit.audit_run.settle_audit`.
10. `ratchet.audit.audit_graph.build_audit_graph.publish` checks the draft with `ratchet.audit.list_markdown.check_draft` against `ratchet.audit.audit_graph.done_ids` (an entry carried under its own id may keep a done id), writes the list, seeds its ledger with `ratchet.refactor.ledger.save_ledger`, adds settled rules to rules.md, and commits with `ratchet.records.commit_records`.

Tests: tests/test_audit_run.py::test_with_no_terminal_the_audit_waits_and_can_be_approved (`start_audit`, the draft gate, then `settle_audit` to publish) and tests/test_audit_run.py::test_the_owner_approves_in_the_terminal (the terminal route to publish). Neither raises a question, so neither stops at the questions gate. That gate is run through `start_audit` only up to the gate, by tests/test_audit_run.py::test_approving_an_audit_cut_off_at_the_questions_stops_at_the_draft (which then approves with no answers), and with owner answers only from the graph, by tests/test_audit_graph.py::test_the_questions_gate_waits_and_the_owners_answers_reach_the_checker. tests/test_audit_live.py::test_a_first_audit_publishes_a_list_within_the_cap runs `start_audit` and `settle_audit` with real agents. It is marked live, spends tokens, and is not skipped by default.

## A re-audit

1. `ratchet.audit.audit_run.start_audit` builds the graph as for a first audit. `ratchet.audit.audit_graph.build_audit_graph.prepare` finds an existing list with `ratchet.refactor.problem_list.find_problem_lists`, so the audit is a re-audit.
2. `prepare` works out when the old list was published with `ratchet.problem_history.publish_time`, and writes evidence hints with `ratchet.audit.evidence_hints.build_evidence_hints`.
3. `ratchet.audit.audit_graph.build_audit_graph.audit` runs `ratchet.audit.audit_graph.build_audit_graph.audit_again`. F, the blind audit, and R, the rechecker, are asked together with `ratchet.agent_calls.ask_together`. F is given no list, ledger or earlier answer.
4. `audit_again` rejects a blind audit that touched paths outside the copy, and asks R again for old ids that have no usable citation or subject.
5. The merge: `ratchet.audit.audit_graph.build_audit_graph.group` merges F's findings with R's checks of the old problems, and the pair judge joins the pairs the rules cannot settle. `ratchet.audit.grouping.finish_groups` keeps only its own item in a group whose old problem is done or gone. The other items of the group come back as a new group for the checker, with a note that names the old id.
6. `ratchet.audit.audit_graph.build_audit_graph.check_citations_step` and `ratchet.audit.audit_graph.build_audit_graph.check` run as in a first audit. The checker marks the entries that continue an old problem.
7. `ratchet.audit.audit_graph.build_audit_graph.draft` keeps an old problem's id and sets its status, such as reopened or blocked. It lists the done and gone problems under Done in the draft with `ratchet.audit.list_markdown.render_draft`.
8. `ratchet.audit.audit_graph.build_audit_graph.publish` seeds the new ledger with a carried event for each carried problem, then a reopened or abandoned event where the status says so, and commits the records.

Tests: tests/test_audit_graph.py::test_a_reaudit_runs_the_blind_audit_and_the_rechecker_and_skips_the_rest (through the draft gate) and tests/test_audit_graph.py::test_a_fresh_finding_with_a_reopened_problem_is_split_and_the_old_id_stays (through publish). Both start at `ratchet.audit.audit_graph.build_audit_graph` with `graph.start` and `graph.resume`, not at `start_audit`, and no test runs a re-audit through `start_audit`. tests/test_audit_graph.py::test_findings_grouped_with_a_done_problem_reach_the_checker_and_the_done_list_is_published runs the split and the Done section through publish, in the same way.

## A problem marked done by refactor, then reopened by a re-audit

1. A refactor run commits a job, and `ratchet.refactor.ledger.record_job` marks the problem done in the ledger of its list.
2. A later re-audit follows the re-audit path above. The rechecker finds the problem is not fixed, and the draft gives it the status reopened.
3. `ratchet.audit.audit_graph.build_audit_graph.publish` keeps its id and writes carried and reopened events in the new ledger.
4. `ratchet.problems_view.show_history` reads the problem's events from both lists as one timeline, through `ratchet.problem_history.problem_history`.

Tests: tests/test_audit_graph.py::test_a_problem_done_in_the_old_ledger_is_reopened_and_published runs the re-audit half from a ledger marked done by hand, starting at `ratchet.audit.audit_graph.build_audit_graph` with `graph.start` and `graph.resume`, not from a refactor run. tests/test_audit_graph.py::test_a_reopened_problem_keeps_its_id_with_carried_and_reopened_events and tests/test_audit_graph.py::test_the_history_reads_as_one_timeline_across_both_lists run the re-audit half only, and the old list in them is made by a first audit, not by a refactor run. tests/test_job_loop.py::test_jobs_run_until_the_planner_finds_nothing_left runs the refactor half only.

## A refactor run

1. `ratchet.cli.main` loads the project with `ratchet.project.load_project` and calls `ratchet.refactor.job_loop.run_refactor`.
2. `ratchet.refactor.job_loop.run_refactor` refuses a tree with changes, runs the suite once with `ratchet.pytest_runner.run_pytest` and refuses to go on if it fails. It then asks `ratchet.pytest_runner.has_pytest_cov` whether pytest-cov loads in the project's test environment. When it does not, the run says once "coverage is not checked", and the test step skips coverage.
3. It finds the newest list with `ratchet.refactor.problem_list.find_latest_problem_list` and loads the ledger with `ratchet.refactor.ledger.load_ledger`.
4. For each job it starts the graph from `ratchet.refactor.job_graph.build_job_graph`. `ratchet.refactor.job_graph.build_job_graph.plan` asks the planner for one step, which now names the functions it changes, and the job ends there if the planner has nothing left.
5. `ratchet.refactor.job_graph.build_job_graph.super_plan` builds the pathway map of those functions with `ratchet.pathways.build_map` and `ratchet.pathways.describe`, and asks the super-planner, with the plan and rules.md. A revise goes back to plan with the super-planner's notes, at most 2 times (`ratchet.refactor.settings.Caps.revisions`), and the next revise undoes the job as abandoned. An approve goes to write-tests when it lists tests, and otherwise to implement.
6. `ratchet.refactor.job_graph.build_job_graph.write_tests` asks the test writer for the listed tests. `ratchet.refactor.written_tests.find_write_faults` refuses a change to any file but a new test file, and a skip, and the tests must pass on the code as it is. A refused write is undone with `ratchet.git.Repo.undo` and goes back to the test writer, at most 2 times (`ratchet.refactor.settings.Caps.test_rewrites`), and the next refusal undoes the job as abandoned. The written tests become frozen paths of the implementer.
7. `ratchet.refactor.job_graph.build_job_graph.implement` asks the implementer to make the change.
8. `ratchet.refactor.job_graph.build_job_graph.check_map` maps the functions that the diff changes with `ratchet.pathways.functions_of_diff`. Module-level code is not a function, and the plan's functions and the ones the super-planner approved earlier in the job are known. Any other function goes to `ratchet.refactor.job_graph.build_job_graph.review_map`, which asks the super-planner with the plan, those functions' pathway map, the diff and rules.md. An approve is kept in the job's `approved` list and goes on. A revise goes back to implement with the super-planner's notes, counted as a rejection, so the third refusal undoes the job as abandoned.
9. `ratchet.refactor.job_graph.build_job_graph.check_rules_step` runs `ratchet.refactor.rule_checks.check_rules` on the change, which also puts back any written test that the implementer changed, renamed or deleted.
10. `ratchet.refactor.job_graph.build_job_graph.test` runs the suite and compares it with the baseline. When coverage is on, it runs the suite with pytest-cov and `ratchet.pytest_runner.find_untested_lines` lists the lines in src that the change adds and no test runs. A src .py file that the coverage report does not hold at all, such as one under a folder with no `__init__.py`, counts every added line that is not blank and not a comment. Untested lines go back to implement the way a test failure does, sharing its count and cap.
11. `ratchet.refactor.job_graph.build_job_graph.review` asks the reviewer. An accept goes on, and an accept with comment fixes goes through `ratchet.refactor.job_graph.build_job_graph.fix_comments` first, which refuses a fix to a written test. A reject goes back to implement.
12. `ratchet.refactor.job_graph.build_job_graph.commit` commits the change, with the written tests.
13. `ratchet.refactor.ledger.record_job` and `ratchet.refactor.ledger.save_ledger` record the job. When the run ends, `ratchet.refactor.job_loop.run_refactor.save_records` commits the ledger with `ratchet.records.commit_records`.

Tests: tests/test_job_loop.py::test_jobs_run_until_the_planner_finds_nothing_left and tests/test_job_loop.py::test_a_run_commits_its_ledger_to_the_records_repository. Both start at `ratchet.refactor.job_loop.run_refactor` and run plan, super-plan, implement, test, review, commit and the ledger, with a super-planner that approves and asks for no tests. tests/test_job_loop.py::test_a_job_with_written_tests_commits_them runs the write-tests step too, from `run_refactor` to the commit that holds the written test. tests/test_job_graph.py covers each arrow of the new steps: the approve, the revise and its cap, the written tests that fail on the current code and the cap on rewrites, and the frozen paths. None of them goes through `ratchet.cli.main`.

Tests for the map check and coverage: tests/test_job_graph.py::test_a_function_outside_the_plan_goes_to_the_super_planner_and_its_approval_goes_on, tests/test_job_graph.py::test_a_refused_function_goes_back_to_implement_with_the_notes, tests/test_job_graph.py::test_three_refused_map_reviews_undo_the_job, tests/test_job_graph.py::test_an_approved_function_is_not_reviewed_again and tests/test_job_graph.py::test_a_module_level_change_does_not_start_a_map_review cover the map check at the graph. tests/test_job_graph.py::test_a_line_no_test_runs_goes_back_to_implement and tests/test_job_graph.py::test_three_coverage_failures_undo_the_job cover coverage at the graph. tests/test_job_loop.py::test_an_untested_line_goes_back_from_run_refactor, tests/test_job_loop.py::test_a_run_without_pytest_cov_says_once_that_coverage_is_not_checked (the missing pytest-cov case) and tests/test_job_loop.py::test_a_function_outside_the_plan_is_reviewed_from_run_refactor run them from `run_refactor`. tests/test_pytest_runner.py covers `ratchet.pytest_runner.has_pytest_cov`, the coverage run, `ratchet.pytest_runner.find_untested_lines` (including a file missing from the report) and the report.

## A refactor job that halts

1. A step raises, such as an agent that gives no answer.
2. `ratchet.refactor.job_loop.run_refactor` catches it. If the tree has changes it calls `ratchet.git.Repo.undo`, so the job leaves no half-made change.
3. The ledger keeps the job_started events that `ratchet.refactor.job_graph.build_job_graph.plan` saved, so the problem stays open.
4. `ratchet.refactor.job_loop.run_refactor.save_records` commits the ledger, and the error is raised again.
5. `ratchet.cli.main` prints it on one line and returns 1.

A job that reaches three test failures (which include runs with lines that no test runs) or three rejections (which include refused map reviews), a third revise from the super-planner, or a third refused test write does not halt. `ratchet.refactor.job_graph.build_job_graph.undo` undoes it with `ratchet.git.Repo.undo`, which also drops any written test not yet committed, the job ends as abandoned, and the run goes on. A planner that stops after a revise ends the job the same way, and the job keeps the last plan that the super-planner saw, so `ratchet.refactor.ledger.record_job` records its title and problems. A problem that an earlier plan started and a revised plan dropped gets a sent_back event and stays open.

Tests: tests/test_job_loop.py::test_a_job_that_halts_part_way_is_undone, tests/test_job_loop.py::test_a_halted_job_leaves_job_started_in_the_ledger_file and tests/test_job_loop.py::test_a_halted_job_commits_the_records. They run `run_refactor` up to the raised error, not `ratchet.cli.main`, so step 5 is untested. tests/test_job_loop.py::test_a_job_abandoned_at_the_revise_cap_lets_the_run_go_on runs an abandoned job from `run_refactor` to the next job's commit. tests/test_job_graph.py::test_the_third_revise_abandons_the_job, tests/test_job_graph.py::test_three_refused_test_writes_abandon_the_job and tests/test_job_graph.py::test_a_stop_after_a_revise_keeps_the_last_plan run the other caps at the graph, not through `run_refactor`.

## ratchet reopen

1. `ratchet.cli.main` sends the command to `ratchet.cli.run_reopen`, which loads the project and calls `ratchet.reopen.reopen_problem`.
2. `ratchet.reopen.reopen_problem` refuses a project with no records repository, an empty reason, no list, or an id that is not a problem on the newest list.
3. It finds the list with `ratchet.refactor.problem_list.find_problem_lists` and `ratchet.refactor.problem_list.split_entries`.
4. It loads the ledger, sets the problem open, clears its sendbacks and adds a reopened event by the owner.
5. `ratchet.refactor.ledger.save_ledger` saves the ledger and `ratchet.records.commit_records` commits it.
6. `ratchet.cli.run_reopen` prints which list changed.

Tests: tests/test_reopen.py::test_the_command_reopens_and_prints_errors_on_one_line (through `ratchet.cli.main`, to the printed line) and tests/test_reopen.py::test_reopening_commits_the_ledger_in_the_records_repository (through `ratchet.reopen.reopen_problem`, to the commit).

## ratchet problems and --history

1. `ratchet.cli.main` sends the command to `ratchet.cli.run_problems`, which refuses --history with no problem and --all with a problem.
2. With no id, `ratchet.problems_view.show_lists` prints the newest list, or every list with --all.
3. With an id, `ratchet.problems_view.show_problem` prints the entry from the newest list that holds it.
4. With --history, `ratchet.problems_view.show_history` calls `ratchet.problem_history.problem_history`, which follows carried_from back through the older lists.
5. `ratchet.problems_view.describe_event` words each event, and `ratchet.cli.run_problems` prints the text.

Tests: tests/test_cli.py::test_problems_shows_the_newest_list (steps 1 and 2, through `ratchet.cli.main`). --history is not run through `ratchet.cli.main` except for its refusal in tests/test_cli.py::test_problems_errors_print_one_line, so the history path from the command line is none yet. `ratchet.problems_view.show_history` is run directly by tests/test_problems_view.py::test_the_history_prints_one_exact_line_per_event and tests/test_reopen.py::test_the_history_shows_the_reopen.
