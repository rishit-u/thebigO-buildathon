# Builders Buildathon: Blast Radius, progress summary

*Status as of Sat 21 Nov 2026, 07:30 ET. Submissions close Sun 22 Nov, 11:00 ET.*

## 1. The task

Each sqlglot commit (all based on pinned commit `eb11aec4`) comes with a patch. Our container chooses which tests to run and splits them over at most 8 workers.

**Score per commit:**
- the recorded seconds of the busiest worker;
- plus 2 s for each worker used;
- plus 300 s if the selection misses a test the patch breaks;
- plus the container's CPU time.

The board takes the middle of 3 runs.

| | Practice | Final |
|---|---|---|
| Commits | 40 | about 60 unseen ones, including kinds of change not in practice |
| Submissions | 1 per hour | — |

**Rules:**
- Not allowed: answers worked out ahead of time and looked up by patch, meaning no table from a patch, or from a kind of edit, to the tests it breaks.
- Allowed: maps built from the pinned commit (coverage, imports, fixtures).

**Git rules:**
- Claude pushes only to `claude/blast-radius`, never to `main`, and never force-pushes.
- Rishit decides merges and submissions, and submits by full commit hash.

## 2. How the selector works

`selector.py` reads `map.json.gz`, a map built from runs of the pinned commit's test suite. For each patch it picks tests as follows.

1. **Coverage per line.** Each changed line selects the tests that ran it. The coverage runs were fixed so that this also sees:
   - work done in process pools;
   - setup code shared by a test class;
   - fixture files that tests read;
   - runtime caches.
2. **Statement kind.**
   - A **control-flow edit** (if/elif/return…) selects every test that entered the function.
   - **Import-time code** reaches the users of the module.
   - **Registry changes** (imports, classes, `_sql_names`, `__init__.py`) select every test.
3. **Symbol-level maps.**
   - Readers of scalar class attributes (v7).
   - Lambda bodies.
   - Instantiation (MRO) sets.
4. **Value-level maps** (v9/v10). A profiling pass records, per test, what each comparison and and/or condition actually evaluated. Swapping `is`/`==` or `<`/`<=`, or reshaping an and/or, then selects only the tests where the result could change.
5. **Worker split.** Longest test first onto the least-loaded worker, trying 1 to 8 workers and keeping the cheapest. This split was checked to equal the lower bound.
6. **Safety net.** Any exception means "run every test": a wide answer only costs time, while a crash rejects the run.

**Fast (aggressive) mode** is the default since v11; `BR_SAFE=1` gives the safe v10 behaviour. It adds three rules:
- **Giant tests are kept only for edits in their own code areas:**

  | Test | Recorded time | Kept for edits in |
  |---|---|---|
  | `test_max_nodes` | 28.8 s | parser and tokenizer files |
  | `test_execute_tpcds` | 21.5 s | executor, planner, optimizer, serde, the Python and DuckDB code, TPC-DS fixtures |
  | `test_generator_memory` | 6.7 s | `generator.py`, `dialect.py` |

  If a selection covers more than 80% of all tests, the giants are kept anyway (v12.1).
- Run-everything fallbacks shrink to the tests that ran the changed files.
- **Class-level reach** (v12): an edit inside a class body selects only the tests that created an object of that class (or of an enclosing class). Before, it selected every user of the module.

## 3. Versions and results

All versions are on `claude/blast-radius`.

| Version | Commit | Idea | Sample (s) | Practice (s) |
|---|---|---|---|---|
| v1 | `1ae2bd4` | coverage baseline | 331.5 | 729.4 |
| v4 | `356f344` | function entry, member dependencies | | 246.3 |
| v8 | `f021e23` | runtime caches; last version without value maps | | 239.3 |
| v10 | `67924582` | value-level maps for and/or (safe candidate) | 123.3 | 233.3 |
| v11.1-fast | `3dc98eb9` | aggressive mode plus review fixes | 120.1 | not yet reported |
| v12-fast | `82645cdd` | class-level reach | 113.1 | — |
| **v12.1-fast (latest)** | **`c01a5522fc7d3befcda7bb54652624d94d9c5a73`** | keeps the giant tests for very broad edits | **113.1** | — |

Every version in the table had 0 misses on the sample.

- **Cost of random edits** (estimated cost per edit; no tests run): v10 7.59 s, v11.1-fast 6.03 s, v12-fast 5.75 s, v12.1-fast 5.81 s.
- **Gap to the top:** the practice leader was at 131.5 s. Practice barely moved between v8 and v10, even though the sample improved 30%. So practice holds kinds of commit the sample does not have.

## 4. Testing so far

- **Equivalence:** every instrumented test run gave the same outcomes as a normal run (1621 passed, 25454 subtests passed).
- **Mutation labs:** random edits, with the truth taken from running the full test suite or from every test that can reach the file. They covered control flow, scalar attributes, lambdas, operator swaps and and/or changes.
- **Class-body lab for v12:** 14 random deletions of dict, set, list or tuple entries in class bodies, checked against the full suite.
  - Class-level reach caused 0 misses.
  - One edit found a miss from the older v11 giant-area rule: deleting an entry of `Generator.__slots__` broke 460 tests, including `test_execute_tpcds`. v12.1 fixes this.

## 5. Open questions and risks

- **Compliance (Rishit's call).** ChatGPT notes that the value-level map is keyed by operator family, which is close to "a kind of edit". The map is built only from runs of the pinned commit and never sees a patch, but a question to the organizers would settle it. v8 (`f021e23`) is the last version without the value map.
- **Fast-mode risk.** One miss costs 300 s, and the final has kinds of change not in practice. The miss count reported for the v11.1-fast practice submission is the key signal.
- **Class-reach gaps:**
  - a class attribute read without any instance of the class;
  - data derived at import and stored outside the class;
  - `getattr` by string at import.
- **Paused:** the dict-entry value map (data can be copied and merged in ways the map can't trace) and an in-container fork-server runner (small gain: 174.8 vs 176.1 s on the sample).
- **Biggest remaining cost:** `test_execute_tpcds`. The patches' changed lines are run by 56 or 57 of the 57 TPC-DS queries it executes, so splitting it does not help.

## 6. Next steps

1. **Submit v12.1-fast** (`c01a5522fc7d3befcda7bb54652624d94d9c5a73`) to practice when the hourly slot allows, and check the miss count.
2. **If practice shows misses:** widen the giant-test areas, or fall back to safe mode.
3. **Keep cutting cost:** module-level constants through their static uses, then the TPC-DS share.
4. **Before Sunday 11:00 ET, Rishit picks the final commit.**
   - Fast candidate: v12.1-fast.
   - Safe candidate: v10 with `BR_SAFE` behaviour.
   - Without value maps: v8.

## 7. Collaboration notes

- **Discussion with ChatGPT/Codex:** `~/Documents/Codex/2026-11-20/discussion.md`, append-only, with check-ins every 10 minutes. The file on the Mac keeps going back to older copies, probably because of iCloud sync. The complete log is `docs/discussion.md` on the branch.
- **Rebuilding the map:** `tools/build_map_docker.sh` rebuilds it inside the pinned image, running the coverage, profiling and class-reach passes.
