---
name: session-end
description: Wrap up the current Claude session in ≤ 5k tokens. Use when user says "end session", "wrap up", or before /clear/compact. Updates .agents/active.md (small Write OK), edits ONE section of SESSION_HANDOFF.md (Edit, NOT Write), creates a checkpoint in .agents/sessions/ ONLY if milestone, emits paste-ready Resume Prompt. Hard cap: 50 lines for active.md, 200 for checkpoint, NEVER rewrite old session blocks.
---

# /session-end — minimal-token version

Wrap session into 3 files in ≤ 5k tokens.

## Hard caps (BLOCKING — exceed = redo)
- `.agents/active.md` ≤ 50 lines (frontmatter + 5-bullet body + next-action)
- `SESSION_HANDOFF.md`: edit ONLY `## Current State` + insert ONE new entry (≤ 30 lines) + replace Resume Prompt block. NEVER rewrite older sessions.
- Checkpoint `.agents/sessions/YYYY-MM-DD-<slug>.md` ≤ 200 lines. Long lessons → link to v-log-archive.md.

## Steps

1. **Gather** (1 bash call):
   ```
   git log --oneline -5; git status --short; npx vitest run 2>&1 | tail -3
   ```

2. **Edit `.agents/active.md`** (Write OK — small file):
   - Frontmatter: updated_at, status, branch, last_commit, tests, production_url, production_commit, firestore_rules_version
   - Body — 4 sections only: `## State` (3 bullets), `## What this session shipped` (≤ 8 bullets, link to checkpoint), `## Next action`, `## Outstanding user-triggered actions`
   - Decisions: 3-6 ONE-LINE items max. Full reasoning → checkpoint.

3. **Update `SESSION_HANDOFF.md`** (Edit, NOT Write):
   - Edit `## Current State` block (deploy state, last commit, tests)
   - Insert new `### Session YYYY-MM-DD ...` block above prior entry (≤ 30 lines, link to checkpoint)
   - Edit `## Resume Prompt` code block in place
   - DO NOT rewrite older sessions, archive blocks, or footer.

4. **Checkpoint** (only if milestone: feature shipped, phase closed, V-entry logged):
   - `.agents/sessions/YYYY-MM-DD-<short-slug>.md` ≤ 200 lines
   - Sections: Summary (1-3 sentences), Current State (5 bullets), Commits (code block), Files Touched (names only, no diffs), Decisions (1-line each — full reasoning to v-log-archive.md), Next Todo, Resume Prompt
   - NO code blocks > 10 lines. Patterns belong in v-log-archive.md.

5. **Commit + push** (1 bash call):
   ```
   git add .agents/active.md SESSION_HANDOFF.md .agents/sessions/*.md && \
   git commit -m "docs(agents): EOD YYYY-MM-DD <one-line>" && \
   git push origin {branch}
   ```

6. **Emit Resume Prompt** as ONE text message ≤ 30 lines:
   ```
   Resume {project} — continue from {date} EOD.

   Read in order BEFORE any tool call:
   1. CLAUDE.md
   2. SESSION_HANDOFF.md (master={sha}, prod={sha})
   3. .agents/active.md ({N} tests)
   4. .claude/rules/00-session-start.md (iron-clad + V-summary)
   5. (if milestone) .agents/sessions/<slug>.md

   Status: master={sha}, {N} tests pass, prod={sha} LIVE
   Next: {one specific action OR "idle"}
   Outstanding (user-triggered): {1-3 bullets}
   Rules: no deploy without "deploy" THIS turn (V18); V15 combined; Probe-Deploy-Probe
   /session-start
   ```

## Anti-patterns (BLOCKING)

- NEVER `Write` a full handoff/active when `Edit` of one section suffices.
- NEVER duplicate V-entry detail in active.md AND checkpoint AND handoff — pick ONE (checkpoint), link from others.
- NEVER rewrite older session blocks — they're frozen.
- NEVER dump full V-entry body into commit message — link to v-log-archive.md.
- NEVER include code blocks > 10 lines in active.md / handoff. Code lives in src/ + tests.

## Success

Total tokens ≤ 5k. Tomorrow's chat reading `.agents/active.md` knows: branch + commit + tests + prod state + next action in ≤ 50 lines. Resume Prompt fits one message.
