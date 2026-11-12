# forklab

**A lab for testing how AI coding assistants react to what they see.**

## The idea in plain language

AI coding assistants ("agents") work in a loop. They run a command, such as the project's tests, read the output, decide what to do next, and repeat. Whether they end up fixing the bug depends heavily on what those outputs said along the way.

forklab asks a simple question: **if one of those outputs had said something different, would the agent still have succeeded?**

To answer it, forklab does something like rewinding a recording:

1. **Record.** Let an agent work on a small coding task. Save every step, along with an exact copy of the project's files after each step.
2. **Rewind to one moment.** Pick a single step, for example the moment the agent read a test report. Put the files back exactly as they were at that moment.
3. **Swap one thing.** Replace what the agent saw at that step with a controlled alternative:
   - **the original** report, unchanged;
   - a **misleading** report that points at the wrong file;
   - a **"placebo"** report that says exactly the same thing, just reordered and reformatted.
4. **Replay many times.** Let the agent continue from that moment dozens or hundreds of times for each version.
5. **Compare.** Check each attempt against hidden tests the agent never sees, and compare how often each version succeeded.

The placebo is the key. If changing only the formatting moves the results as much as changing the meaning, then forklab is measuring noise, not information, and it says so. A result of "inconclusive" is allowed and reported honestly.

### Why the extra care?

Rewinding has to be exact, or the comparison is meaningless. So forklab:
- runs every command the agent issues in a **fresh, locked-down container**: no internet, no admin rights, and nothing left running afterwards;
- copies the project files **exactly**, including permissions and timestamps, and refuses anything it can't copy faithfully;
- **fails safe**: if it can't prove something was done correctly, it stops instead of guessing.

## Where things stand (2026-11-11)

The foundations are built and tested. forklab cannot run a real experiment yet.

| Piece | Status |
|---|---|
| Plan (roadmap) | Agreed: [docs/roadmap.md](docs/roadmap.md) |
| Check that the agent toolkit we use (Inspect) can "rewind" | Done. See [findings](spikes/phase0_inspect_restore/FINDINGS.md) |
| Locked-down container runner | Done |
| Exact file snapshots and restore | Done |
| Building the rewound conversation | Done, and it refuses anything it can't replay faithfully |
| Automatic testing on Linux (CI) | Passing; waiting for review |
| Agent's tools, recorder, replay runner, hidden-test checker, reports | Not started |

**Important limitations right now**
- **Disk usage is not capped.** Agent code could fill the disk, so forklab must only run trusted test projects until a disk limit is added.
- **Thinking blocks are refused.** Some AI models hide "thinking" in their replies, and replaying those exactly hasn't been verified yet. Recordings that contain them are refused.
- **No real AI calls have been made yet.** All tests use a local stand-in for the AI service, so nothing has been spent.

The roadmap's progress section has the full details.

## For developers

You need Python 3.12, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync
uv run pytest -q
```

Tests marked `docker` need a running Docker daemon. They are skipped locally if Docker isn't running, and they must pass in CI.

### Layout
- `src/forklab/executor.py`: runs one command in a fresh, locked-down container.
- `src/forklab/snapshot.py`: exact workspace snapshots, storage, restore and verification.
- `src/forklab/fork.py`: builds the rewound conversation with the swapped tool result.
- `spikes/phase0_inspect_restore/`: the Phase 0 feasibility spike and its evidence.
- `docs/roadmap.md`: the architecture, the plan and the progress log.
