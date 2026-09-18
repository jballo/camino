# Agent rules for this repository

## Secret handling (non-negotiable)

Context: in September 2026, credentials from `Backend/.env` were exposed into
agent session transcripts twice — once via a literal connection string in a
generated `psql` command, once via `docker compose config` rendering the
resolved environment. Treat every tool argument and every line of terminal
output as permanently recorded.

- Never read, display, summarize, or modify a real `.env` file (any
  `.env`/`.env.*` except `.env.example`, `.env.sample`, `.env.template`).
- Never read private-key or credential files (`*.pem`, `*.key`, `*.p12`,
  `*.pfx`).
- Never place a credential value (password, token, API key, connection
  string with auth, private key) in a command argument. A command must be
  safe to display and record before it is even proposed — approval prompts
  do not protect confidentiality.
- Never ask the user to paste a credential into chat.
- Never run environment dumps: `env`, `printenv`, `export -p`.
- Never run `docker compose config` without `--quiet`/`-q`; the verbose
  form prints resolved secrets. Never run `docker inspect` in a way that
  returns container environment variables.
- Never read agent session stores (`~/.codex/sessions`, `~/.claude`
  transcripts).
- To inspect configuration structure, report key names only and whether a
  value is set — e.g. `grep -oE '^[A-Za-z_][A-Za-z0-9_.-]*=' file` (the
  trailing `=` is load-bearing: without it, multi-line PEM values match as
  "names" and leak). Never use `cut`/`awk` on env files.
- If an operation genuinely requires a real secret (generating a key,
  connecting with credentials), tell the user to do that step in a separate
  terminal outside any agent session.
- When testing these safeguards, use synthetic files with fake values
  (`SYNTHETIC_TEST_VALUE`); never open a real secret file to test a rule.
- Never bypass a filesystem denial or a blocked command to complete a task.
- If a secret does appear in any tool input/output, disclose it to the user
  immediately and prominently, with rotation steps.
