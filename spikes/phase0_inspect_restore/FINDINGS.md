# Phase 0 spike: can Inspect host a forklab fork?

Timebox was 4 h; the spike took about 1 h, plus a review fix that made the pass criteria strict. Run on 2026-11-11.

## How to reproduce

Docker must be running. From the repo root:

```bash
uv sync
uv run python spikes/phase0_inspect_restore/spike.py
```

The script exits 0 only if `verdict.evaluate` passes (see "Pass criteria" below). It writes:
- `evidence/results.json`: the verdict, per-sample checks, restore diffs, request paths, and environment versions.
- `evidence/first_turn_request.json`: the exact first resumed request body, as sent by Inspect's Anthropic provider.
- `evidence/logs/`: the Inspect `.eval` logs (git-ignored).

**Regression tests for the evaluator** (offline, no Docker). These check that a missing, failed or malformed sample can never pass:

```bash
uv run pytest spikes -q
```

**End-to-end negative control.** This makes one sample's stub return HTTP 500. The run must exit 1, and it writes to `evidence/negative_control/`:

```bash
uv run python spikes/phase0_inspect_restore/spike.py --fail-sample tar_restore_b
```

## Method

- **Model path.** Inspect's real Anthropic provider (`anthropic/claude-sonnet-5-5`, `streaming=False`) pointed at a local stub HTTP endpoint. The stub records every request body. No API key is used and nothing is spent.
- **Fork.** `Sample.input` is the prefix: system, user, an assistant turn calling `run_tests` (id `toolu_fork_0001`), and a tool message carrying a substituted result. The substituted text has trailing spaces, a non-ASCII arrow, a tab and no trailing newline.
- **Stub behaviour.** If the last message is the forked tool result, the stub answers with a live `run_tests` call. Otherwise it answers `done`.
- **Restore.** A 10-entry synthetic workspace (dirs, an empty dir, files with modes 0644/0600/0755, binary content, a relative symlink, nanosecond mtimes, uid/gid 1000) is built as a PAX tar with explicit nanosecond `mtime` pax headers. It is restored in two ways:
  - `tar_restore_a` and `tar_restore_b`: the tar is passed through `Sample.files` as a data URI, and `Sample.setup` runs `tar --numeric-owner --same-owner -xpf … -C /workspace`.
  - `files_only`: each file is written through `Sample.files` alone, with no setup.
- **Measurement.** The first solver step runs an lstat manifest inside the container, before the agent acts. It compares type, mode, uid, gid, size, mtime_ns, sha256 and link target against the spec. Symlink mode is excluded because Linux ignores it.
- **Sandbox.** Inspect's `docker` sandbox with `python:3.12-slim` pinned by digest, `network_mode: none`.
- **Attribution.** Each sample runs in its own `eval` with its own stub endpoint on its own port. Every request is therefore attributed to exactly one sample.

### Pass criteria (`verdict.py`)

The spike passes only if:
- exactly the three expected samples produced a record, with none missing and none extra; and
- every sample passes every check below.

**Per-sample checks:**
- The sample recorded no error, and the eval status is `success`.
- The manifest was captured.
- The stub received exactly two requests, both on `/v1/messages`.
- Request 1 holds the fork prefix: the `tool_use` IDs and `tool_result` IDs are both exactly `[toolu_fork_0001]`, and the last message is the user's `tool_result`.
- Request 1's substituted text equals the expected string **after JSON decoding**.
- Request 2's last `tool_result` is `toolu_live_0001`, with text exactly `FAIL: add(2, 3) == -1\n`.
- The final completion is `done`.
- For the tar samples only: the restore diff is empty.

**Reported as observations, not required:**
- Whether the raw JSON bytes are identical across samples.
- Whether the `files_only` restore is exact.

## Results

| Question | Answer | Evidence |
|---|---|---|
| Q1: Can `Sample.input` carry a prior assistant tool call and its tool result? | **Yes** | Each first-turn request has `tool_use` id `toolu_fork_0001` followed by a `tool_result` with the same id. The message shape is user → assistant[text, tool_use] → user[tool_result]. |
| Q2: Can per-sample setup restore a workspace before the solver runs? | **Yes, with tar plus `setup`. No, with `files` alone.** | The tar restore gives 0 diffs over 10 entries × 8 fields in both samples, on each of 3 runs. `files_only` gives 28 diffs: uid/gid become root, mtimes become write time, two modes are lost, and the empty dir and symlink are missing. |
| Q3: Does the first resumed request carry the substituted result? | **Yes: the text is equal after JSON decoding** | In all 3 samples, the decoded `tool_result` text equals the substituted string exactly, and it is the last message. |

All three samples passed every required check: each had exactly 2 requests, no errors, and the expected live result and completion. The negative control (`--fail-sample tar_restore_b`) exited 1, with Q1, Q3 and the loop check all false and the other two samples still passing.

**Other observations:**
1. **Raw JSON bytes were identical.** We track this separately from Q3. The first-turn request bodies had identical raw bytes across all 3 samples in a run, and across at least 4 separate runs, all with sha256 `86777c07…a56a9a`, using the same library versions. Sample IDs do not appear in the payload. This is an observation and not a requirement: a fork stays faithful if serialisation changes but the decoded content is equal.
2. **More HTTP attempts than configured.** In the negative control, the failing sample's stub received **3** requests despite `GenerateConfig(max_retries=0)`. The likeliest explanation is the Anthropic SDK's own default retries; the cause is not confirmed. This matters for roadmap §3: cost reservations must count HTTP attempts per model call, not one per call.
3. **The resumed loop works in the restored workspace.** In every sample, the agent's live `run_tests` call executed there and returned `FAIL: add(2, 3) == -1\n`. The stub then received that live result on the second turn.
4. **Inspect adds prompt-cache breakpoints by itself.** `cache_control: ephemeral` appears on the system block, the tools, and the last assistant block (the forked `tool_use`).
5. **Inspect changes the request based on the model name.** For Sonnet-5.5-class names it adds `thinking: {type: adaptive, block_binding: {prefix_mismatch_behavior: drop_block}}` plus a beta header. The logic is in `inspect_ai/model/_providers/anthropic.py`, `apply_thinking_block_binding`. **This matters for forklab.** If a replayed prefix differs from the one that produced recorded thinking blocks, the API drops those blocks silently instead of returning an error. A fork could therefore lose reasoning context without anyone noticing. This breaks the roadmap's fail-closed rule. It is **not tested here**, because the spike prefix contains no thinking blocks.

## Decision needed: Inspect's sandbox model vs roadmap §1.1

The restore was verified against **Inspect's** docker sandbox. That sandbox is one long-lived compose container per sample, and it runs commands as root by default. Roadmap §1.1 requires something different:
- a **disposable container per tool call**;
- a read-only root;
- uid 1000;
- the workspace as a **host directory**.

Inspect's built-in sandbox does not provide that.

**Recommendation:**
- Keep Inspect for the agent loop, the model provider and logs. Q1 and Q3 are verified, so **no custom loop is needed**.
- forklab's tools call forklab's own per-call executor (`docker run --rm …` against a host workspace directory) instead of `sandbox()`.
- Restore becomes host-side: write the tar or manifest into a fresh host directory, then verify. `Sample.setup` is not used.
- The tar and manifest code from this spike carries over.

A custom Inspect `SandboxEnvironment` provider is the alternative. It adds more surface for no gain we can see yet.

## Limitations: what this spike did not verify

- **Stub only, no live API.** We did not verify that the real API accepts this payload: the tool ID format, the thinking-binding beta and the cache_control placement.
- **No thinking blocks in the prefix.** The `drop_block` risk above is open.
- **One provider.** Only the Anthropic provider path was exercised.
- **Restore fidelity on a narrow base.** It rests on 10 synthetic entries on one platform (macOS arm64, Docker Desktop 29.8.0 Linux VM).
  - ctime, atime and inode numbers were not compared.
  - Symlink mode was excluded.
  - tar ran as root with `--same-owner`.
- **Host-side capture untested.** The expected manifest is built from the spec, not from lstat of a real host directory.
- **Restore duration not measured.** The whole run took 27 s wall time for 3 samples, including container start.
- **Byte-identity has narrow scope.** It held across at least 4 runs with identical library versions only. It is not established across Inspect or SDK upgrades.
- **Image digest is multi-arch.** The pinned digest is the multi-arch index. The platform-specific manifest digest is not pinned.
- **Not under §1.1 flags.** The sandbox used none of the §1.1 isolation flags (read-only root, dropped capabilities, uid 1000, resource limits).

## Proposed follow-ups

1. **Thinking-block mismatch: unresolved, so forklab fails closed.** Until a live probe has run, forklab must refuse to fork any trajectory whose replayed prefix contains `thinking` or `redacted_thinking` blocks, and must never claim a fork is faithful if blocks could be dropped. The live probe (fork a recorded trajectory that contains thinking blocks and observe whether the API reports dropped blocks) is **deferred** until a provider is configured and a spend budget is set. Offline contract tests on request payloads continue in the meantime.
2. **Phase 1 executor.** Build the §1.1 executor and host-side snapshot and restore, reusing `snapshot.py`.
