---
applyTo: "**"
description: "Auto-commit after every completed coding cycle, with a clear, accurate commit message."
---

# Auto-commit on coding-cycle completion

When you finish a **coding cycle** — a self-contained change (feature, fix, refactor) that is
implemented AND validated — commit it automatically before ending your turn. Do not wait to be asked.

## When to commit
- After the change is complete and verified: syntax/type checks pass (`python3 -c "import ast; ast.parse(...)"`
  or `py_compile` for Python; `get_errors` for TS), and any quick local checks are green.
- One logical change per commit. If a turn produced several unrelated changes, make several commits.
- Do **not** commit half-finished or non-compiling work. If validation fails, fix it first (or leave it
  uncommitted and say so).

## What to stage
- Stage only the files that belong to this change: prefer `git add <specific paths>` over `git add -A`.
- **Never** stage secrets, credentials, `.env` files, large build artifacts, or unrelated in-progress files.
  Inspect `git status --porcelain` and `git diff --staged --stat` before committing.
- If unfamiliar/unexpected files are present, leave them alone and mention them rather than committing them.

## Commit message format
Use Conventional Commits with an accurate scope and an informative body:

```
<type>(<scope>): <concise summary in imperative mood>

- what changed and why (1–4 bullets)
- note any migrations, new deps, or follow-ups (e.g. deploy, run git-update.sh)
```

- `type`: `feat` | `fix` | `refactor` | `docs` | `chore` | `perf` | `test`.
- `scope`: the area touched — e.g. `costs`, `notifications`, `cloud-api`, `web`, `appliance`, `models`.
- Summary ≤ ~72 chars, imperative ("add", "fix", not "added"/"fixes").
- The body must reflect what actually changed — never a generic "update files".
- Call out DB migrations (model + `db.py` ALTER), new `requirements.txt`/package deps, and doc updates.

Run the commit with a heredoc so the body is preserved, e.g.:

```sh
git add cloud/app/cloud_costs.py cloud/app/api/costs.py
git commit -F - <<'EOF'
fix(costs): scope cost attribution per hyperscaler provider

- an AWS bill was split across Azure nodes (and vice versa); scope nodes/storage by provider
- force-refresh replaces the current hour's samples so new resource ids apply immediately
EOF
```

## Safety
- **Commit locally only. Do NOT `git push`** — pushing is a shared/hard-to-reverse action that needs
  explicit user confirmation (it also triggers the production deploy via `git-update.sh`).
- Never use `--no-verify`, `git commit --amend` on already-pushed commits, `git reset --hard`, or force ops.
- If a pre-commit hook fails, fix the underlying issue — do not bypass it.
- After committing, briefly state the commit subject so the user knows what landed.
