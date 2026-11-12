# forklab

forklab forks a recorded coding-agent trajectory at a single tool-result boundary and restores the environment to that point. It then replaces the tool result with a controlled substitute, resumes the agent many times, and compares pass rates on a hidden oracle across predeclared arms.

- Plan: [docs/roadmap.md](docs/roadmap.md)
- Phase 0 spike (Inspect restore): [spikes/phase0_inspect_restore/FINDINGS.md](spikes/phase0_inspect_restore/FINDINGS.md)

## Setup

Requires Python 3.12, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync
```
