# Phase 0 spike: can Inspect host a forklab fork?

Timebox was 4 h; the spike took about 1 h. Run on 2026-11-11.

## How to reproduce

Docker must be running. From the repo root:

```bash
uv sync
uv run python spikes/phase0_inspect_restore/spike.py
```

The script exits 0 only if Q1, Q2 (tar restore) and Q3 all pass. It writes:
- `evidence/results.json`: verdicts, per-sample restore diffs, environment versions.
- `evidence/first_turn_request.json`: the exact first resumed request body, as sent by Inspect's Anthropic provider.
- `evidence/logs/`: the Inspect `.eval` log (git-ignored).

## Method

- **Model path.** Inspect's real Anthropic provider (`anthropic/claude-sonnet-5-5`, `streaming=False`) pointed at a local stub HTTP endpoint. The stub records every request body. No API key is used and nothing is spent.
- **Fork.** `Sample.input` is the prefix: system, user, an assistant turn calling `run_tests` (id `toolu_fork_0001`), and a tool message carrying a substituted result. The substituted text has trailing spaces, a non-ASCII arrow, a tab and no trailing newline.
- **Stub behaviour.** If the last message is the forked tool result, the stub answers with a live `run_tests` call. Otherwise it answers `done`.
- **Restore.** A 10-entry synthetic workspace (dirs, an empty dir, files with modes 0644/0600/0755, binary content, a relative symlink, nanosecond mtimes, uid/gid 1000) is built as a PAX tar with explicit nanosecond `mtime` pax headers. It is restored in two ways:
  - `tar_restore_a` and `tar_restore_b`: the tar is passed through `Sample.files` as a data URI, and `Sample.setup` runs `tar --numeric-owner --same-owner -xpf … -C /workspace`.
  - `files_only`: each file is written through `Sample.files` alone, with no setup.
- **Measurement.** The first solver step runs an lstat manifest inside the container, before the agent acts. It compares type, mode, uid, gid, size, mtime_ns, sha256 and link target against the spec. Symlink mode is excluded because Linux ignores it.
- **Sandbox.** Inspect's `docker` sandbox with `python:3.12-slim` pinned by digest, `network_mode: none`.

## Results

| Question | Answer | Evidence |
|---|---|---|
| Q1: Can `Sample.input` carry a prior assistant tool call and its tool result? | **Yes** | Each first-turn request has `tool_use` id `toolu_fork_0001` followed by a `tool_result` with the same id. The message shape is user → assistant[text, tool_use] → user[tool_result]. |
| Q2: Can per-sample setup restore a workspace before the solver runs? | **Yes, with tar plus `setup`. No, with `files` alone.** | The tar restore gives 0 diffs over 10 entries × 8 fields in both samples, on each of 3 runs. `files_only` gives 28 diffs: uid/gid become root, mtimes become write time, two modes are lost, and the empty dir and symlink are missing. |
| Q3: Does the first resumed request carry the substituted result byte for byte? | **Yes, at the payload level** | In all 3 samples, the `tool_result` text equals the substituted string exactly, and it is the last message. |

**Other observations:**
1. **Payloads are byte-identical.** The first-turn request bodies were identical across all 3 samples in a run. They were also identical across 3 separate runs, all with sha256 `86777c07…a56a9a`, using the same library versions. Sample IDs do not appear in the payload.
2. **The resumed loop works in the restored workspace.** In every sample, the agent's live `run_tests` call executed there and returned `FAIL: add(2, 3) == -1\n`. The stub then received that live result on the second turn.
3. **Inspect adds prompt-cache breakpoints by itself.** `cache_control: ephemeral` appears on the system block, the tools, and the last assistant block (the forked `tool_use`).
4. **Inspect changes the request based on the model name.** For Sonnet-5.5-class names it adds `thinking: {type: adaptive, block_binding: {prefix_mismatch_behavior: drop_block}}` plus a beta header. The logic is in `inspect_ai/model/_providers/anthropic.py`, `apply_thinking_block_binding`. **This matters for forklab.** If a replayed prefix differs from the one that produced recorded thinking blocks, the API drops those blocks silently instead of returning an error. A fork could therefore lose reasoning context without anyone noticing. This breaks the roadmap's fail-closed rule. It is **not tested here**, because the spike prefix contains no thinking blocks.

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
- **Byte-identity has narrow scope.** It held across 3 runs with identical library versions only. It is not established across Inspect or SDK upgrades.
- **Image digest is multi-arch.** The pinned digest is the multi-arch index. The platform-specific manifest digest is not pinned.
- **Not under §1.1 flags.** The sandbox used none of the §1.1 isolation flags (read-only root, dropped capabilities, uid 1000, resource limits).

## Proposed follow-ups

1. **Live thinking-block probe.** Needs an API key and a small spend. Record a short trajectory with thinking blocks, fork it, and check whether the response reports dropped blocks. If drops cannot be detected, forklab should set `extra_body.thinking` to make mismatches fail closed, if the API supports that. Otherwise disable thinking for confirmatory runs and record that choice in the spec.
2. **Phase 1 executor.** Build the §1.1 executor and host-side snapshot and restore, reusing `snapshot.py`.
