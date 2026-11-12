# forklab — Coding-Agent Intervention Lab
## Final Architecture & Execution Roadmap (v1.1)

v1.1 adds the progress log and the decisions made while building. The plan itself (§0–§6) is unchanged, except for the §1.1 disk-quota precondition added in PR #2.

---

## Progress (as of 2026-11-11)

### Done and merged

| Unit | PR | What it established |
|---|---|---|
| Phase 0: Inspect restore spike | #1 | Inspect can carry a fork prefix ending in a substituted tool result (Q1), and the substituted text reaches the provider payload unchanged (Q3). The check used a local stub endpoint, not the live API. Restoring a workspace with full metadata works with a tar plus setup script, but not with `Sample.files` alone (Q2). Full write-up: `spikes/phase0_inspect_restore/FINDINGS.md`. |
| Per-call container executor and host snapshots | #2 | §1.1 and §1.2 are built as `src/forklab/executor.py` and `src/forklab/snapshot.py`. Each isolation property is checked by a test against a real container. Snapshots stream file contents and refuse anything they cannot restore exactly. |
| Fork transcript builder | #3 | §1.3 is built as `src/forklab/fork.py`. The recording must be a valid, ordered tool-call transcript. Any non-text content in the prefix, thinking blocks included, is refused. |

### Open

| Unit | PR | Status |
|---|---|---|
| CI on Linux with Docker | #4 | Green on its first run: 126 tests passed, none skipped, on Ubuntu 24.04 with Docker 28.0.4. Waiting for review. |

### Phase 1 checklist (§4)

Built:
- [x] Phase 0 spike (§1.2 restore decision made)
- [x] Executor with the §1.1 isolation flags
- [x] Snapshot manifest, content-addressed store, restore with an exact MATCH check
- [x] Fork transcript built at the post-tool boundary (no re-execution)
- [x] Replay check on a restored clone (one test only; not yet a 20-run measurement)

Not built yet:
- [ ] The closed tool set: `read_file`, `write_file`, `list_dir`, `run_tests`
- [ ] Recorder: capture S_before and S_after around each tool call during a live run
- [ ] Fork runner: restore S_after_t, build the prefix, resume the agent
- [ ] Task fixture and oracle screen (20/20 pass, 20/20 fail)
- [ ] Arms (b) and (c), plus the junit-tuple comparator
- [ ] Isolated oracle runner, integrity scan, adjudication
- [ ] Report: categories, Wilson intervals, missingness bounds
- [ ] **Disk quota on `/workspace` and `/out`. This must be in place before any untrusted trial.**
- [ ] Pilot run

### Decisions made while building

1. **Inspect's own sandbox is not used for tool calls.** It keeps one long-lived container per sample, running as root. Inspect still drives the model and the agent loop, but tools call forklab's own executor, which starts a fresh container for every call.
2. **The container runs as the runner's uid:gid**, not the 1000:1000 written in §1.1. That way files written to the workspace are owned by the runner, so snapshots can compare ownership exactly.
3. **A start marker separates "docker failed" from "tool failed".** Docker's own exit codes (125–127) overlap with tools' exit codes, so a launcher inside the container writes a marker file before running the call. No marker means an infrastructure failure.
4. **Container lifecycle is create, then start, then remove by ID and confirm removal.** If removal can't be confirmed, the call fails.
5. **Each call gets a fresh output directory, and the host never edits a live workspace.** On Docker Desktop for macOS, reusing a shared folder after the host deletes a file can break the next container.
6. **Thinking blocks are refused in fork prefixes** until a live API probe shows that dropped blocks can be detected (Phase 0 finding: Inspect asks the API to *drop* mismatched thinking blocks rather than fail).
7. **Retries are counted per HTTP attempt.** Even with `max_retries=0`, the provider SDK made 3 attempts on a server error, so the §3 cost reservations must count attempts.

### Open questions and risks
- **Thinking blocks generated after a fork:** untested. Settling this needs a live probe, which needs a configured provider and a budget.
- **Disk exhaustion:** not prevented yet. See the precondition in §1.1.
- **Platforms tested:** macOS arm64 with Docker Desktop 29.8.0, and Ubuntu 24.04 with Docker 28.0.4 (in CI). Rootless Docker is untested.

---

## 0. Purpose, estimand, claim status

**Purpose.** forklab measures how a coding agent's task outcome changes when one tool result is replaced in a controlled way. Each measurement is at a single fork point and under a fixed model configuration.

**Primary estimand.** The estimand is defined for:
- a fork point F = (task, recorded trajectory, step t);
- a model M, identified by its exact ID;
- a temperature T, a step cap K and a token cap.

For each arm X:

> Δ_X = P(success | arm X) − P(success | arm a), estimated over **all randomized trials**.

A trial counts as **success** only when the hidden oracle passes **and** the agent caused no contract violation. Infrastructure failures are treated as missing outcomes. They are reported by arm and covered by sensitivity bounds (§1.7).

| Status | Claim |
|---|---|
| Supported by prior work, not by us | Manipulating tool outputs changes agent behaviour (AgentDojo and the prompt-injection literature). Provider sampling is nondeterministic. |
| Hypothesis H-I1 | Inspect can restore a mid-trajectory prefix with a substituted tool result and needs no custom loop. |
| Hypothesis H-R1 | Under the contract, 100% of restores match exactly on every declared-relevant field. The replay check matches recorded output in at least 95% of runs. |
| Hypothesis H-E1 | At the chosen F, the misleading arm lowers success by at least 20 points relative to the original arm. |
| Hypothesis H-E2 | At the same F, the placebo arm is equivalent to the original within ±δ. |
| Hypothesis H-T | Effects depend on temperature. This is a separate experiment with its own locked plan, run after the primary. |
| Hypothesis H-G | Effects generalize across fork points and tasks. Phase 3 only. |

**We have no experimental support yet for any hypothesis above.**

**Never claimed:**
- deterministic execution;
- full equivalence of environments;
- detection of all time dependence;
- general causal attribution;
- that the oracle is secure against an adversary;
- transfer beyond the configuration actually tested.

---

## 1. System architecture

### 1.1 Execution model: a disposable container per tool call

Each branch has its own workspace: a host directory owned by the runner. **Every** tool call (`read_file`, `write_file`, `list_dir`, `run_tests`) runs in a fresh container:

```
docker run --rm --read-only --network none --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 256 --memory 2g --cpus 2 \
  --tmpfs /tmp:rw,size=256m --user 1000:1000 --hostname forklab \
  --env-file fixed.env \
  -v <branch_ws>:/workspace:rw -v <call_out>:/out:rw \
  <image@sha256:...> <tool-cmd>
```

This design gives four properties:
- **Processes cannot outlive a call.** Removing the container kills every process in its PID namespace.
- **Workspace symlinks resolve inside the container, never on the host.** Tools run inside the container. The host snapshots with `lstat` and never follows links.
- **Only `/workspace` persists.** `/tmp` is a tmpfs, so anything written there is discarded by design.
- **The hidden tests never enter the image or any agent workspace.**

`fixed.env` sets `PYTHONHASHSEED=0`, `PYTHONDONTWRITEBYTECODE=1`, `TZ=UTC` and `LANG=C.UTF-8`.

`run_tests` runs `pytest -p no:cacheprovider -p no:randomly --junitxml=/out/junit.xml`. The agent receives stdout and stderr, size-bounded. The junit file goes to `/out`, which is outside the workspace.

`list_dir` returns entries sorted by name.

**What this contract controls:** image, network, writable paths, process lifetime, resources, environment variables, hash seed, and the bytecode and pytest cache.

**What it does not control:** wall clock, kernel version, CPU timing and scheduling, `readdir` order seen by agent-written code, `/proc` contents, and provider-side state.

**What it cannot detect** (stated explicitly):
- Network *attempts*. They fail under `--network none`, but nothing logs them.
- Transient writes to the tmpfs.
- Time-dependent behaviour.
- Side channels.

Optional syscall tracing (Phase 3) narrows some of these gaps. It does **not** close them.

**Precondition before untrusted trials: bounded disk.** The current executor mounts `/workspace` and `/out` as plain bind mounts with **no storage quota**. Agent code can fill the host disk or the Docker VM disk. Neither the timeout nor the memory limit bounds filesystem writes. The executor is therefore **not safe against disk exhaustion**.

Before any trial runs agent-written code, `/workspace` and `/out` must be quota-backed, for example by a fixed-size loopback filesystem, an XFS project quota, or a size-limited volume. A test must show that writing past the quota fails inside the container and leaves the host unaffected. Until then, executor runs are limited to trusted fixtures.

### 1.2 Snapshots and their timing

For tool call t:

1. The agent emits tool call t, and the runner records it.
2. The runner takes **S_before_t** while no container is attached to the workspace.
3. The call runs in a disposable container. The runner captures the result R_t, the exit code, the wall time and `/out`.
4. The container is removed, and the runner takes **S_after_t**.
5. R_t is returned to the agent.

**Snapshot.** A snapshot is a manifest built with `lstat` over every entry. Each entry records:
- path, type, mode, uid and gid;
- size and `mtime_ns`;
- symlink target, if any;
- sha256, for regular files.

Blobs go into a content-addressed store, and an SQLite index maps (trajectory, step, before/after) to a manifest hash. ctime, atime, inode number and `readdir` order are recorded for diagnosis only. They cannot be restored or are unstable.

**Restore(S) into a fresh directory:**
1. Create the entries in sorted order and write the blobs.
2. Set mode and owner.
3. Set `os.utime(ns=…)`. Directories are set last, deepest first.
4. Take a new snapshot and compare it with S.

**Relevant fields.** By default, every restorable field is relevant: path, type, mode, uid, gid, size, sha256, symlink target and `mtime_ns`. Equality must be **exact**.

A field may be dropped from the relevant set only when the experiment spec does both of these:
- (a) states why the field is irrelevant;
- (b) passes a **perturbation test**: change the field in a disposable clone of S_before_t, re-run call t, and confirm the normalized output is unchanged.

A perturbation test is evidence, not proof.

**Fidelity result:**
- **MATCH:** every declared field is exactly equal. The restore is eligible.
- **MISMATCH:** the trial is an infrastructure failure and becomes a missing outcome. There is no partially valid class.

### 1.3 Forking at the post-tool boundary

Branch i in arm X is built in four steps:

1. **Workspace.** Restore S_after_t into a fresh branch directory, which must verify as MATCH.
2. **Transcript.** Take the messages up to and including the assistant's tool call t. Append a tool-result message whose content is `substitution_X`. For arm a, that content is R_t verbatim.
3. **No re-execution.** Call t is **not** executed again on the branch.
4. **Resume.** The agent continues, and later calls run under the §1.1 model inside the branch workspace.

**Replay check.** This check measures how faithfully the environment is restored. It is separate from agent behaviour, and it never touches a branch workspace.
- Restore S_before_t into a **disposable clone**.
- Execute the recorded call t.
- Compare the normalized output with R_t.
- Compare the resulting manifest with S_after_t on the declared fields.

The check runs 20 times per fork point, and the match rate is reported. Normalization rules, such as stripping durations and temporary paths, are declared in the spec.

### 1.4 Arms (matched and declared in advance)

All three arms derive from the junit XML of the real call t.
- **(a) Original:** R_t verbatim.
- **(b) Misleading:** identical junit tuples, except the failing assertion's `file:line` points at another real file and line in the repo. The length is within ±5% of R_t.
- **(c) Placebo:** a deterministic transform that reorders independent tests and changes formatting. The set of (test id, status, message, file:line) tuples is identical to (a).

"Placebo-equivalent" means equivalent **under this structural definition**, which is a modelling choice. Ordering and salience are intentionally what the placebo changes.

A comparator checks every arm. All arm blobs are hashed into the spec before the first trial.

### 1.5 Hidden oracle: treated as untrusted code execution

The oracle runs after the agent finishes, in five steps:

1. **Copy.** Copy the final workspace into a fresh oracle directory, never the branch directory. Copy the hidden tests into a separate directory.
2. **Integrity scan.** Before the oracle runs, scan the workspace for:
   - `conftest.py`, `sitecustomize.py`, `usercustomize.py` and `*.pth` files;
   - changes to pytest configuration in `pytest.ini`, `pyproject.toml`, `setup.cfg` or `tox.ini`;
   - files whose names shadow stdlib modules or hidden-test modules.

   Any hit is a **contract violation caused by the agent**, so the trial counts as a fail. The fixture is designed so that a correct fix never needs these files.
3. **Run.** The oracle uses the same isolation flags as §1.1. Both the workspace and the hidden tests are mounted `:ro`, and `/out` is the only writable mount. There are no environment secrets, no other host mounts, and a hard timeout. The command is:

   `pytest --noconftest -p no:cacheprovider --rootdir=/hidden --junitxml=/out/oracle.xml /hidden`
4. **Verdict.** The host parses `oracle.xml` with a size cap. **The agent never sees oracle output or the verdict**, and trials share no memory.
5. **Adjudication, declared in advance.**
   - A clean pass or a clean assertion failure is final.
   - An error, crash, timeout or collection error triggers up to 2 re-runs in fresh containers.
   - If the runs agree, that is the verdict. A consistent timeout counts as a fail.
   - If they disagree, the oracle is **ambiguous**, and the trial becomes a missing outcome.

**Residual risk:**
- Docker shares the host kernel, so it is not a strong adversarial boundary.
- Agent code runs in the same process as the tests. It can forge a verdict by monkeypatching pytest or through side effects at import time.
- The scan catches common vectors, not all of them.
- **The verdict itself is forgeable.** The pytest process runs agent code, so that code can write its own `oracle.xml`, rewrite results, or exit with a chosen status.

**Isolation and verdict trust are separate problems.** A VM backend (gVisor `runsc` or Firecracker) protects the host. It does **not** make the verdict trustworthy, because the forging code runs inside the same process that reports the verdict.

**Policy:**
- The pilot and all Phase 1–2 experiments use **non-adversarial tasks only**: fixtures where the agent has no instruction or incentive to game the tests.
- A trustworthy verdict needs an **out-of-process oracle**. Here the verdict is computed by a process that never imports or executes agent code. Examples: black-box tests over stdio, the CLI or HTTP, or assertions evaluated in a parent process on outputs a child process produces. This is a separate Phase 3 investigation, not an assumed property.

### 1.6 Outcome taxonomy (every randomized trial gets exactly one category)

- **success:** the oracle passes and there is no violation.
- **fail:** any of the following:
  - the oracle fails;
  - the step cap or token cap is reached;
  - the agent caused a violation: a tool outside the set, a protected-file hit from the scan, or a resource-limit kill (OOM or pids) during a tool call that ran agent code.
- **missing (infrastructure):** any of the following:
  - provider errors that remain after the retry budget;
  - Docker or runtime errors;
  - restore MISMATCH;
  - an ambiguous oracle;
  - a halt from the budget kill-switch.

Retries keep the same trial ID and arm. The arm is never drawn again.

### 1.7 Analysis

- **H-E1:** Δ_b, two-sided, with a Newcombe interval.
- **H-E2:** Δ_c, tested with TOST at a margin δ declared in advance.
- **Multiple hypotheses:** Holm correction across H-E1 and H-E2.
- **Missing outcomes:** report the complete-case estimate and worst-case/best-case bounds. To get the bounds, set every missing outcome in each arm to the extreme that most favours, then most opposes, the hypothesis. A conclusion is reported only if it holds across the bounds. Otherwise the result is "**inconclusive (missingness)**".
- **Report contents:** every randomized trial by arm and category, plus a test for differential missingness.
- **Locking the plan.** The spec, the analysis-code hash and the git commit form a **locally locked analysis plan**. The word "preregistered" is used only with an **external timestamped record**, such as an OSF registration or an OpenTimestamps proof, made before the first confirmatory trial.

### 1.8 Data flow

1. **`record`** runs the agent under the §1.1 model and writes the trajectory, S_before and S_after for every call, and R_t with its exit code and timing.
2. **`screen`** runs the oracle 20× on the reference solution, which must pass 20/20, and 20× on the buggy state, which must fail 20/20. It also runs the 20× replay check at candidate fork points.
3. **`fork`** builds the arm blobs, runs the comparator checks and any perturbation tests, and writes and locks the spec.
4. **`gate`** projects cost by simulation (§3) and refuses to proceed if the projection exceeds the budget.
5. **`run`** schedules trials in randomized, time-interleaved blocks. Each trial runs: restore S_after_t → MATCH check → inject the transcript → resume → integrity scan → oracle → categorize. Spending is tracked live, with a hard kill-switch at budget B.
6. **`report`** runs the locked analysis, labels each hypothesis supported, refuted or inconclusive, and lists the limitations it observed.

---

## 2. Tech stack

**Runtime and tooling:**
- Python 3.12 managed with `uv`, and pydantic for spec schemas.
- Docker Engine with image digests pinned.
- The stdlib `os`, `hashlib` and `tarfile` for manifests, the content-addressed store and restores.
- SQLite as the index.

**Agent loop:** Inspect AI for model providers, the agent loop and `.eval` logs. A custom loop is used only if the spike fails, and it keeps Inspect's model API. Adapter contract tests are required either way:
- an identical prefix serializes to byte-identical request payloads;
- tool-call IDs are preserved across the injection;
- the injected tool-result message validates against the provider's schema;
- the stop and step-cap semantics match the spec.

**Statistics:** `statsmodels` (`proportion_confint`, `tost_proportions_2indep`, `power_proportions_2indep`) and a numpy simulator for power and cost.

**Model:** one pinned API model, with its exact ID and request timestamps logged. Temperature is fixed for the primary experiment. Provider prompt caching is used for the shared prefix. We assume caching does not change the output distribution, and this is listed as an assumption.

---

## 3. Sample size and cost gate

**Sample size.** These are normal-approximation planning values with α=0.05 and power 0.8. The simulator must confirm them before any confirmatory run.

| Test | Assumption | N per arm |
|---|---|---|
| Superiority for H-E1 | p_a=0.70, p_b=0.50 | ≈ 91 |
| TOST for H-E2 | δ=0.08, true Δ=0, p=0.5 | ≈ 670 |
| TOST for H-E2 | δ=0.08, true Δ=0, p=0.8 | ≈ 430 |
| TOST for H-E2 | δ=0.15, true Δ=0, p=0.5 | ≈ 190 |

If the true difference is small but not zero, the required N grows. Fork points are chosen from **separate calibration runs** with p_a between 0.3 and 0.8. Calibration data never enter the confirmatory analysis.

**Cost model.**

> C_total = C_pilot + C_calibration + Σ_arms Σ_trials c_i · (1 + retries_i) + C_oracle_compute

The per-trial cost c_i is resampled **per arm** from the pilot distribution, because arms can produce trajectories of different lengths. Infrastructure retries are resampled at the rate observed in the pilot. Invalid actions by the agent are outcomes, not overhead, but their trajectory cost is still counted.

**Gate.** The runner refuses to start unless the simulated **95th percentile of C_total** is at most B. `--pilot` overrides this.

**Limits of the gate.** It is an estimate, not a guarantee. The pilot may not be representative, and prices or rate limits can change.

**Kill-switch with reservations.** A switch that only reads live usage cannot enforce an exact cap. Requests already in flight still bill, and the provider may report usage late. So the runner uses admission control:
- Before sending a model request, it reserves that request's **worst-case cost**: full input tokens at the uncached price plus `max_tokens` of output.
- It sends the request only if `spent + outstanding_reservations + this_reservation ≤ B`.
- When the actual usage arrives, the reservation is released and the actual cost is recorded.

**Possible overshoot is stated, not hidden.** Overshoot can still occur through pricing errors, provider billing that differs from reported usage, or non-model costs such as compute. Every report states the reserved worst case, the actual spend, and any overshoot.

A halted experiment is reported as **stopped for budget**. It is analysed under the locked plan and will most likely be inconclusive.

**If the gate fails,** take one of these options and lock a new plan that records the change:
- widen δ openly;
- move the fork point later;
- test superiority only and report equivalence as "not tested";
- use a cheaper model for method development only.

N is never cut quietly.

---

## 4. Phased roadmap

### Phase 0: Inspect restore spike (≤4 h, counted inside the 48 h)
The spike answers three questions:
1. Can a Sample take a message list with earlier assistant tool calls and tool results as input?
2. Can per-sample setup restore a workspace before the solver runs?
3. Can the first resumed turn carry the substituted result?

**Exit:** a written yes or no for each question, and a decision between Inspect solver and custom loop. A failed spike does not reopen the project choice.

### Phase 1: 48-hour vertical slice (mechanics only, no inferential claims)

| Work item | Hours |
|---|---|
| Phase 0 spike | 4 |
| Fixture: tiny Python repo with one bug, visible and hidden tests, pinned image, 20/20 oracle screen | 5 |
| Executor: disposable container per call, four tools, `/out` | 6 |
| Manifest, content-addressed store, restore and MATCH check | 7 |
| Recorder with S_before/S_after per call | 5 |
| Fork runner: restore S_after_t and inject the transcript | 6 |
| Arms (b) and (c), hand-built, plus the junit-tuple comparator | 3 |
| Oracle runner: isolation, adjudication, basic integrity scan | 4 |
| Replay check (20 runs at one fork point) | 2 |
| Report: CSV, per-arm category table, Wilson intervals, missingness bounds | 3 |
| Pilot of 5 trials per arm (15 total) and write-up | 3 |
| **Total** | **48** |

**The hours are an estimate, not a commitment.** There is no slack, so overruns are likely.

**Never cut to meet the deadline:**
- the per-call container isolation flags;
- the disk quota on `/workspace` and `/out`, once it exists (until then, no untrusted trials);
- the S_before/S_after snapshots and the MATCH check;
- the oracle's isolation and the integrity scan;
- outcome categorization.

**Cut order if behind:**
1. Trial count, down to a minimum of 3 per arm.
2. Analyses: drop the bounds and intervals and leave only the category table.
3. UI and automation: replace CLI commands with documented manual steps.
4. Replay-check repetitions, down to a minimum of 5 (never zero).

**If any safety boundary is incomplete, the experiment is BLOCKED.** It runs no trials and makes no claims, even pilot ones. The report names the missing control.

**Excluded from Phase 1:** power simulation, gate enforcement (cost is measured only), parallelism, external timestamping, multiple tasks, perturbation tests, and retries beyond one.

**Exit criteria:**
- All 15 branch restores are MATCH. Any mismatch is explained and categorized as missing.
- The replay check's match rate is reported. Either it is at least 19/20, or every mismatch has a named cause.
- The oracle screen shows 20/20 pass on the reference and 20/20 fail on the buggy state.
- Every randomized trial appears in the report under exactly one category.
- Tokens, cost and wall time per trial are measured **by arm**.
- The report is stamped **"PILOT: no inferential claim"**.
- A written list records every unsupported effect observed.

### Phase 2: Core engine (about 2–3 weeks)

**Scope:**
- The power and cost simulator, the enforced gate and the live kill-switch.
- Locked analysis plans, plus an external timestamp for any confirmatory run.
- 5 to 10 screened tasks and calibration runs for choosing fork points.
- Perturbation tests for any field the spec declares irrelevant.
- Parallel containers, scheduling in time-interleaved randomized blocks, and the full retry policy.
- Differential-missingness tests and Holm correction.

**Exit criteria:**
- One externally preregistered, gate-approved experiment at one fork point, with H-E1 and H-E2 each labelled supported, refuted or inconclusive (bounds included).
- Fidelity and replay statistics across at least 500 restores.
- Every missing trial attributed to a cause.

### Phase 3: Advanced capabilities

**Scope:**
- **Sensitivity map:** placebo and misleading interventions at every tool boundary, using group-sequential designs with alpha spending.
- **Generalization (H-G):** a hierarchical model (mixed-effects logistic or Bayesian) across fork points and tasks. This is the only place cross-task claims can be made.
- **H-T:** a temperature experiment with its own locked plan.
- **Cross-model comparison.**
- **Wider contract:** restricted bash, with `strace -f -e trace=%file,%network` as a *partial* detector of contract violations. eBPF is used only if overhead requires it, and every violation fails closed.
- **Stronger isolation:** a gVisor or Firecracker backend for the oracle. This protects the host only.
- **Out-of-process oracle investigation:** design and test a verdict path that never runs in a process executing agent code. Validate it against a planted suite of verdict forgeries before any adversarial task is used.
- **Interaction search:** ddmin over sets of observations, once single-observation effects can be measured reliably.

**Exit criteria:**
- A sensitivity map over at least 3 tasks, analysed under the locked plan.
- The detector catches every member of a **planted-violation suite**: hidden network calls, a lingering daemon, writes outside the workspace, conftest injection and time-dependent tests. This shows coverage of *that suite only*.

---

## 5. Primary failure modes and mitigations

**1. The restored environment is not faithful, so forks are silently invalid.**

*Causes:* metadata drift, state outside the workspace, leftover processes, caches written by tools, and time or host dependence.

*Mitigations:*
- a disposable container per call, a read-only root, and the workspace as the only state that persists;
- exact equality on every declared field, with exclusions only through a justified, perturbation-tested declaration;
- forking from S_after_t with no re-execution;
- the replay check on a separate clone of S_before_t;
- the cannot-detect list (§1.1) published in every report.

**2. The outcome measurement is compromised by a flaky or tampered oracle.**

*Mitigations:*
- the 20/20 screen;
- an isolated oracle container with read-only mounts, `--noconftest`, no secrets and no network;
- the integrity scan;
- adjudication rules declared in advance;
- ambiguous results treated as missing, with bounds;
- residual in-process tampering risk stated, and a microVM upgrade path.

**3. Inference is underpowered, confounded or unaffordable.**

*Causes:* a non-equivalent placebo, a length or salience mismatch between arms, provider drift, differential missingness, multiple comparisons, selection bias in the fork point, and the cost of equivalence testing.

*Mitigations:*
- comparator-checked arms and length matching;
- time-interleaved randomization;
- analysis of all randomized trials, with bounds;
- Holm correction;
- calibration data kept separate from confirmatory data;
- power by simulation;
- "inconclusive" as a first-class result;
- the simulated-quantile cost gate and the live kill-switch.

---

## 6. Remaining limitations (not solved by this roadmap)

1. **Narrow scope of inference.** Every result is conditional on one fork point, model ID, temperature, step cap and point in time. Silent model updates on the provider side can invalidate a run between blocks. Interleaving reduces this risk but does not remove it.
2. **Equivalence is expensive.** A tight placebo margin needs hundreds of trials per arm. Early confirmatory work will probably test superiority only, or use wide margins.
3. **Restores are incomplete.** ctime, inode numbers, `readdir` order, the kernel and host, wall clock and CPU timing are not reproduced. Matching manifests does not prove the environments are equivalent.
4. **Detection has blind spots.** Network attempts, transient tmpfs writes, time dependence and side channels go undetected. Phase 3 tracing narrows these gaps and does not close them.
5. **The oracle is not secure against an adversary.** Agent code in the pytest process can forge its own XML or verdict, and escape through the shared kernel remains possible. A VM protects the host but does not make the verdict trustworthy. Until an out-of-process oracle is validated, the design suits non-adversarial tasks only.
6. **The placebo is defined by a model.** "Equivalent" means equal junit tuples. Other channels, such as ordering and emphasis, are the very thing being varied, so a different definition could give different conclusions.
7. **Prompt caching is assumed neutral.** We assume provider prefix caching does not change the output distribution, and we have not verified it.
8. **External validity is limited.** Small Python repos with pytest oracles and a closed tool set are far from real agent workloads that use bash.
9. **The cost gate is an estimate, and the cap is not exact.** Reservations limit overshoot but cannot rule it out: pricing errors, billing that differs from reported usage, and non-model costs can still push spend past B. The kill-switch bounds spend, not statistical power. A budget-halted run will usually be inconclusive.
10. **Local locks are not preregistration.** Without an external timestamp, the plan lock proves only internal consistency.
11. **The schedules are estimates.** The 48-hour figure and the Phase 2 duration are estimates, not commitments. Overruns are absorbed by cutting trial count, analyses, UI or automation, never isolation or integrity controls.
