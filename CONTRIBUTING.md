# Contributing

## Pull request process

### Branch naming

Use descriptive branch names prefixed by category:

- `feat/<topic>` — new functionality
- `fix/<topic>` — bug fixes
- `docs/<topic>` — documentation-only changes
- `debt/<ws-number>-<topic>` — tech-debt workstreams from `docs/plans/initial-debt-and-questions.md`

### Before opening a PR

1. **Run the app locally** and confirm it starts without error:
   ```sh
   ./run.sh
   ```
2. **Run the tests:**
   ```sh
   uv run python -m pytest
   ```
3. **Check documentation obligations.** If your change involves:
   - A non-obvious design choice → write or amend an ADR in `docs/adr/`
   - New future work → update the relevant plan in `docs/plans/`
   - A validated/invalidated hypothesis → resolve or create a spike in `docs/spikes/`

### PR description

Include:

- **What** — one-line summary of the change.
- **Why** — link to the ADR, plan workstream, or spike that motivates it.
- **How to verify** — commands or steps a reviewer can run to confirm the change works.

### Review and merge

- Every PR targets `main`.
- At least one approving review before merge (self-merge acceptable for documentation-only changes).
- Squash-merge is the default. Use a merge commit only when the branch history is intentionally structured (e.g. a multi-ADR sequence).
- The merge commit message should be a clean single-line summary; GitHub's default squash message (PR title) is fine.

### After merge

Merging to `main` triggers the deploy pipeline (GitHub Actions → ACR → AKS). Monitor the workflow run for failures. Rollback if needed:

```sh
kubectl set image deployment/cqc-deployment cqc-container=<acr>/<image>:<previous-sha> -n cqc
```

### What doesn't need a PR

- Typo fixes in documentation (commit directly to `main`).
- Handoff creation/deletion (these are ephemeral by design).
