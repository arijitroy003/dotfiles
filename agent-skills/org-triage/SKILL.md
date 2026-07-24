---
name: org-triage
description: Fan-out triage of a GitHub org (or repo list) — discover repos, analyze contribution gaps, open quick-win PRs, file issues, triage stale tickets, and monitor outcomes. Use when asked to find contribution opportunities, audit an org, or do an open-source contribution blitz.
---

# Org Triage

Systematic fan-out workflow for triaging a GitHub organization, finding contribution opportunities, and shipping quick-win PRs at scale.

Accepts an **org name** or an explicit **repo list**. Each phase can run independently or end-to-end.

---

## Inputs

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `ORG` | yes (unless REPOS given) | — | GitHub org name, e.g. `dbt-labs` |
| `REPOS` | no | all non-archived repos in ORG | Comma-separated `owner/repo` list to scope the run |
| `MAX_REPOS` | no | 60 | Cap on repos to analyze (sorted by stars desc) |
| `MIN_PRS` | no | 50 | Target number of PRs to open. Keep analyzing and fixing repos until this target is reached or all repos are exhausted. Open multiple PRs per repo when fixes are logically independent (e.g. CI fix vs docs fix). |
| `TRACKER_PATH` | no | `./{ORG}-pr-tracker.md` | Where to write the tracker file |
| `CRON_INTERVAL` | no | `2d` | How often the monitoring cron checks PR status |
| `WORK_DIR` | no | — | Local directory for cloning repos. **If not provided, ask the user before starting.** Never default to `/tmp`. |
| `DRY_RUN` | no | false | Discovery + analysis only, no PRs or issues |

---

## Model Routing

Route each phase to the cheapest model that can handle its complexity. Use adaptive routing based on repo size — most repos don't need Opus for analysis.

| Phase | Model | Effort | Rationale |
|-------|-------|--------|-----------|
| Discovery | haiku | low | `gh api` calls, no judgment |
| Deep Analysis (small repos, <500 files) | sonnet | high | Sufficient for gap detection in typical repos |
| Deep Analysis (large repos, 500+ files or complex languages) | opus | high | Needed for subtle bugs in large codebases |
| Synthesis | sonnet | high | Cross-repo pattern recognition — sonnet handles this well |
| Quick-Win PRs | sonnet | medium | Mechanical grep → edit → commit → push |
| Meaningful PRs | sonnet | high | Changes already specified by analysis |
| Issue Filing | sonnet | medium | Writing issue bodies from structured findings |
| Stale Triage | haiku | low | Formulaic comments with evidence from analysis |
| Monitoring | haiku | low | `gh pr view` + status parsing |

**Adaptive routing:** During discovery, fetch repo size via `gh api` (`size` field, in KB). Repos under 10MB with <500 files use Sonnet for analysis; larger or polyglot repos use Opus. This saves ~40% on analysis cost for typical orgs.

When using the Workflow tool, pass `model` and `effort` to each `agent()` call. When using subagents via the Agent tool, pass the `model` parameter.

---

## Before Starting

If `WORK_DIR` is not provided in the arguments, **ask the user** which directory to use for cloning and working on repos before proceeding to Phase 1. Suggest the current working directory as default.

## Autonomy

Once `WORK_DIR` is confirmed, run all phases autonomously without pausing for confirmation. You have **full permissions** inside `WORK_DIR` — clone, create, edit, delete, commit, and push freely. Do not ask before writing files, creating directories, or running commands scoped to `WORK_DIR`. The goal is end-to-end execution: discovery → analysis → fixes → PRs → tracking, with no human-in-the-loop unless a decision genuinely cannot be resolved from the code or skill instructions.

## Setup

Before launching any workflow agents, create `${WORK_DIR}/.claude/settings.json` so subagents inherit the permissions they need. Skip if the file already exists.

```json
{
  "permissions": {
    "allow": [
      "Bash(gh api:*)",
      "Bash(gh repo *)",
      "Bash(gh issue *)",
      "Bash(gh pr *)",
      "Bash(gh search *)",
      "Bash(gh label *)",
      "Bash(gh release *)",
      "Bash(git *)",
      "Bash(grep:*)",
      "Bash(find:*)",
      "Bash(ls:*)",
      "Bash(cat:*)",
      "Bash(head:*)",
      "Bash(wc:*)",
      "Bash(sort:*)",
      "Bash(python3:*)",
      "Bash(mkdir:*)"
    ]
  }
}
```

Run this as a sequential pre-step in the orchestrator (not as a subagent). This ensures all downstream agents — workflow fan-outs, PR agents, triage agents — execute without permission prompts.

### Git Signing Config

Set git signing config once in `${WORK_DIR}/.gitconfig` so all cloned repos inherit it automatically — don't repeat `git config` in every fix agent:

```bash
git config --file "${WORK_DIR}/.gitconfig" user.name "${GH_USERNAME}"
git config --file "${WORK_DIR}/.gitconfig" user.email "${GH_EMAIL}"
git config --file "${WORK_DIR}/.gitconfig" user.signingkey "${GPG_KEY}"
git config --file "${WORK_DIR}/.gitconfig" commit.gpgsign true
```

Then clone with `GIT_CONFIG_GLOBAL="${WORK_DIR}/.gitconfig"` so each repo picks it up.

---

## Phase 1: Discovery `model: haiku, effort: low`

**Goal:** Build the target repo list with metadata.

**Incremental re-run:** If `scan-results.json` exists from a previous run, load it. Skip repos with no new commits since `last_scanned_at`. In Phase 3, skip findings that already match an open PR in `pr-tracker.json`.

```bash
# Fetch all repos, paginated — include size for adaptive model routing
gh api "orgs/${ORG}/repos" --paginate \
  --jq '.[] | select(.archived == false) | select(.size > 0) | {name, full_name, stargazers_count, language, default_branch, updated_at, size}'
```

Sort by `stargazers_count` desc. Cap at `MAX_REPOS`.

**Pre-filter:** Skip repos with `size == 0` (empty/placeholder repos). These waste an analysis agent and produce no actionable findings.

Then for each repo, fetch labels that signal contribution-friendliness:

```bash
gh issue list -R "${ORG}/${REPO}" --label "good first issue" --state open --json number,title,labels
gh issue list -R "${ORG}/${REPO}" --label "help wanted" --state open --json number,title,labels
```

**Output:** A JSON array of repo objects (empty repos with `size == 0` are excluded):

```json
[{
  "name": "repo-name",
  "full_name": "org/repo-name",
  "stars": 1234,
  "language": "Python",
  "default_branch": "main",
  "size": 4500,
  "good_first_issues": 3,
  "help_wanted_issues": 7
}]
```

The `size` field (KB) is used for adaptive model routing in Phase 2 — repos under 10MB use Sonnet, larger repos use Opus.

---

## Phase 2: Deep Analysis `model: opus, effort: high`

**Goal:** Fan out one agent per repo. Each agent clones (shallow) and analyzes.

Spawn up to `MAX_REPOS` agents. When using the Workflow tool, prefer `pipeline(repos, analyzeStage, fixStage, prStage)` over `parallel(analyze all) → parallel(fix all)`. Pipeline eliminates the barrier — Repo A can get its PR opened while Repo T is still being analyzed.

### Per-Repo Agent Instructions

Clone shallow, then inspect:

```bash
gh repo clone "${FULL_NAME}" "${WORK_DIR}/${NAME}" -- --depth 1
```

Analyze the following dimensions and return the schema below.

### Analysis Schema

Each agent returns JSON matching this structure:

```json
{
  "repo": "org/repo-name",
  "stars": 1234,
  "language": "Python",
  "health": {
    "has_readme": true,
    "has_contributing": true,
    "has_license": true,
    "has_security_md": false,
    "has_ci": true,
    "ci_system": "GitHub Actions",
    "has_tests": true,
    "test_framework": "pytest",
    "has_changelog": true,
    "has_codeowners": false
  },
  "quality": {
    "readme_quality": "good|fair|poor",
    "docs_staleness": "current|stale|very_stale",
    "dependency_freshness": "current|outdated|very_outdated",
    "type_hints_coverage": "full|partial|none|n_a",
    "linter_configured": true
  },
  "gaps": [
    {
      "type": "missing_file|typo|broken_link|stale_dep|ci_gap|security|docs|test_gap|dx",
      "severity": "low|medium|high",
      "file": "path/to/file",
      "line": 42,
      "description": "What is wrong",
      "fix_difficulty": "trivial|easy|medium|hard",
      "fix_description": "What to do"
    }
  ],
  "easy_contributions": [
    {
      "gap_index": 0,
      "category": "quick_win|bug_fix|security|dx|docs|test",
      "title": "Short PR title",
      "description": "What to change",
      "files": ["path/to/file"],
      "estimated_diff_lines": 15,
      "confidence": "high|medium|low"
    }
  ],
  "stale_issues": [
    {
      "number": 123,
      "title": "Issue title",
      "created_at": "2024-01-15",
      "last_activity": "2024-03-20",
      "likely_resolved": true,
      "reason": "Why we think it is stale or resolved",
      "suggested_action": "close|comment|leave"
    }
  ],
  "open_issues_count": 45,
  "recent_pr_velocity": "active|moderate|slow|stale",
  "contribution_requirements": {
    "has_cla": false,
    "changelog_tool": "changie|towncrier|manual|none",
    "changelog_format": "description of format if applicable",
    "branch_naming": "fix/*, feat/*, etc.",
    "ci_approval_required": false,
    "required_labels": []
  }
}
```

### Dedup: gaps vs easy_contributions

Each `easy_contributions` entry MUST reference a `gap_index` (0-based index into the `gaps` array). Do NOT duplicate the same finding as both a gap and an easy_contribution with different descriptions — the gap is the problem, the easy_contribution is the proposed fix for that gap. If a gap has no actionable fix, omit it from easy_contributions. If a fix doesn't map to a gap, add the gap first.

### What Makes a Good First Contribution

Prioritize in this order:

1. **Trivial fixes** (typos, broken links, wrong version refs) — highest merge rate, builds trust
2. **Missing community files** (SECURITY.md, .editorconfig, CODEOWNERS) — low risk, high visibility
3. **CI improvements** (pin actions, add missing checks, upgrade runners) — maintainers love these
4. **Dependency updates** (patch/minor bumps with passing CI) — clear value, easy review
5. **Dead code removal** — provably safe deletions, clean diffs
6. **Documentation fixes** (stale examples, missing flags, broken cross-refs) — always welcome
7. **Test additions** for untested code paths — substantive but low-controversy
8. **Bug fixes** with clear reproduction — only if you can write a regression test

**Avoid as first contributions:**
- Refactors (even "obvious" ones) — too subjective for a first PR
- Feature additions — need maintainer buy-in first
- Style-only changes — noise
- Massive multi-file PRs — hard to review from unknown contributors

### Pattern Dedup

Before fixing, group findings by type. For any pattern appearing in 3+ repos (e.g., "add SECURITY.md", "upgrade Actions v3→v4", "bump pre-commit hooks"), create a single template and pass it to all fix agents for that pattern. Don't let each agent independently research the same boilerplate.

### Synthesis `model: opus, effort: high`

After all agents return, a synthesis agent combines findings into a prioritized report:

```
## Org-Wide Summary
- Repos analyzed: N
- Total gaps found: N (high: N, medium: N, low: N)
- Easy contributions identified: N
- Stale issues worth triaging: N

## Priority Contributions (sorted by confidence * impact)
1. repo-name: "Fix X" (trivial, high confidence)
2. repo-name: "Add Y" (easy, high confidence)
...

## Repos by Contribution Opportunity
| Repo | Stars | Gaps | Easy PRs | Stale Issues | Velocity |
|------|-------|------|----------|--------------|----------|
```

---

## Phase 3: Quick-Win PRs `model: sonnet, effort: medium`

**Goal:** One agent per contribution. Fork, branch, fix, commit, push, open PR.

**Confidence filter:** Only fan out fix agents for findings with `confidence: high`. Include `medium` confidence findings in the report for human review. Drop `low` confidence findings.

### Pre-fork (batch)

Before forking, check which repos the user already has forks of to avoid duplicates:

```bash
# Get list of existing forks
EXISTING_FORKS=$(gh repo list "${USER}" --fork --json nameWithOwner --jq '.[].nameWithOwner')

# Fork only repos not already forked
for repo in $TARGET_REPOS; do
  repo_name=$(echo "$repo" | cut -d/ -f2)
  if echo "$EXISTING_FORKS" | grep -q "${USER}/${repo_name}"; then
    echo "Already forked: $repo"
  else
    gh repo fork "$repo" --clone=false 2>/dev/null &
  fi
done
wait
```

### Per-PR Agent Pattern

```bash
# 1. Fork already done in batch step above

# 2. Clone your fork
gh repo clone "${USER}/${REPO}" "${WORK_DIR}/${REPO}" -- --depth 1
cd "${WORK_DIR}/${REPO}"

# 3. Branch
git checkout -b "fix/${SLUG}"

# 4. Make the fix (edit files)
# IMPORTANT: grep to verify the pattern exists BEFORE editing

# 5. Commit with sign-off
git add -A
git commit -S --signoff -m "${COMMIT_MSG}"

# 6. Push
git push origin "fix/${SLUG}"

# 7. Open PR
gh pr create \
  --repo "${ORG}/${REPO}" \
  --head "${USER}:fix/${SLUG}" \
  --title "${PR_TITLE}" \
  --body "${PR_BODY}"
```

### PR Body Template

```markdown
## Summary

${ONE_LINE_DESCRIPTION}

## Changes

${BULLET_LIST_OF_CHANGES}

## Testing

${HOW_VERIFIED} (e.g., "Ran existing test suite", "Verified link resolves", "N/A for typo fix")
```

### Commit Conventions

- Match the repo's existing commit style (conventional commits, imperative mood, etc.)
- If repo has no clear style, use imperative mood: `Fix typo in README.md`
- Always `git commit -S --signoff` for GPG signing + DCO compliance
- One logical change per commit — no bundling unrelated fixes

### Pre-PR Checklist (from contribution_requirements)

Before opening a PR, check the repo's `contribution_requirements` from Phase 2:

- **changelog_tool = changie**: Add a `.changes/unreleased/*.yaml` entry matching the repo's format
- **changelog_tool = towncrier**: Add a `changelog.d/*.md` fragment
- **has_cla = true**: Note in the PR body that CLA signing may be required
- **ci_approval_required = true**: Note that a maintainer must add an approval label before CI runs
- **branch_naming**: Follow the repo's convention (e.g., `fix/*`, `chore/*`)

### PR Etiquette

- Keep PRs small (under 50 changed lines ideally)
- Reference any related issue: `Fixes #123` or `Related to #456`
- Do not open multiple unrelated PRs to the same repo in quick succession — bundle related fixes into one PR
- If the repo has a CONTRIBUTING.md, follow its instructions (CLA, branch naming, etc.)
- Read CONTRIBUTING.md before PRing

---

## Phase 4: Substantive PRs `model: sonnet, effort: high`

**Goal:** Deeper fixes — bug fixes, security patches, DX improvements.

Same fork-branch-fix-commit-push-PR pattern as Phase 3, but each agent gets more context.

### Bug Fix Agent

1. Read the issue or identify the bug from Phase 2 analysis
2. Clone the repo, find the root cause (grep callers, trace the flow)
3. Write a regression test if the repo has a test suite
4. Fix the root cause (not the symptom)
5. Run the existing test suite to confirm no regressions
6. Open PR linking to the issue

### Security PR Agent

1. Identify the vulnerability (shell injection, path traversal, missing input validation, bare `except:`, etc.)
2. Write the fix with minimal diff
3. If the repo lacks a SECURITY.md, add one using GitHub's template
4. Open PR with clear description of the vulnerability and fix

### DX Improvement Agent

1. Identify the developer experience gap (missing docs, confusing CLI output, broken examples)
2. Fix it
3. Open PR explaining the user-facing improvement

---

## Phase 5: Issues and Triage

### Filing Issues `model: sonnet, effort: medium`

For problems too complex for a quick PR, or where maintainer input is needed first:

```bash
gh issue create -R "${ORG}/${REPO}" \
  --title "${ISSUE_TITLE}" \
  --body "${ISSUE_BODY}"
```

**Issue body template:**

```markdown
## Description

${WHAT_IS_WRONG}

## Steps to Reproduce

${STEPS}

## Expected Behavior

${EXPECTED}

## Actual Behavior

${ACTUAL}

## Suggested Fix

${SUGGESTION_IF_ANY}
```

For bugs without existing issues, file the issue first, then reference it in the PR.

### Stale Issue Triage `model: haiku, effort: low`

Run all triage comments in a **single agent** using a bash loop, not one agent per comment. Each comment is independent and formulaic — doesn't warrant a separate agent.

For issues identified as likely-resolved in Phase 2:

```bash
gh issue comment -R "${ORG}/${REPO}" ${ISSUE_NUMBER} \
  --body "${TRIAGE_COMMENT}"
```

**Triage comment template:**

```markdown
This issue appears to be resolved based on ${EVIDENCE}.

${DETAILS_OF_WHY_RESOLVED}

If this is no longer relevant, it may be worth closing. Happy to help verify if needed.
```

**Stale triage rules:**
- Only comment on issues with concrete evidence they are resolved (merged PR, visible code change, version bump)
- Be respectful — you are a guest in their repo
- Never close issues yourself — suggest closing, let maintainers decide
- Max 10 triage comments per org per session
- Skip issues filed by maintainers

---

## Phase 6: Tracker and Monitoring `model: haiku, effort: low`

### Cost Tracking

Track token usage using the workflow tool's `totalTokens` field from each workflow result. Per-agent breakdowns are not available from the runtime — report **per-phase totals** (one workflow per phase) and **estimated costs** based on the model used.

```markdown
## Cost Summary

| Phase | Agents | Tokens | Model | Est. Cost |
|-------|--------|--------|-------|-----------|
| Discovery | 1 (inline) | ~2k | haiku | $0.01 |
| Analysis | N | from workflow result | sonnet/opus | varies |
| Quick-Win PRs | N | from workflow result | sonnet | varies |
| Triage | 1 | ~10k | haiku | $0.01 |
| **Total** | **sum** | **sum** | | **sum** |
```

**Note:** Token counts come from the `totalTokens` field in each Workflow tool result. Per-agent granularity is not available — only the aggregate per workflow run. Cost estimates use published per-token pricing for the model tier used.

### Tracker File

Create `TRACKER_PATH` with this format:

```markdown
# ${ORG} PR Tracker

Last checked: ${DATE}

## Pull Requests

| # | Repo | PR | Status | Action needed |
|---|------|----|--------|---------------|
| 1 | repo-name | [#123](url) | open | - |
| 2 | repo-name | [#456](url) | merged | none |

## Issues Filed

| Repo | Issue | Description |
|------|-------|-------------|
| repo-name | [#789](url) | Description |

## Stale Issues Triaged

| Repo | Issue | Topic |
|------|-------|-------|
| repo-name | [#101](url) | Topic |
```

### Monitoring Cron

Set up a durable cron to check PR statuses:

```bash
# Check all tracked PRs
for each PR in tracker:
  gh pr view -R "${ORG}/${REPO}" ${PR_NUMBER} --json state,reviewDecision,statusCheckRollup,comments
  # Update tracker status
  # Flag PRs needing action (review comments to address, CI failures, etc.)
```

Schedule: every `CRON_INTERVAL` (default 2 days).

**Status transitions to track:**
- `open` -> `merged` (success, remove from active tracking)
- `open` -> `closed` (rejected — note reason, learn from it)
- `open` with review comments -> `needs_response` (address feedback)
- `open` with CI failure -> `needs_fix` (fix and force-push or new commit)

---

## Running Individual Phases

Each phase is independent:

| Invocation | What it does |
|------------|--------------|
| `/org-triage ORG --scan-only` | Phase 1 + 2 only — report, no PRs |
| `/org-triage ORG --quick-wins` | Phase 3 only (requires prior analysis) |
| `/org-triage ORG --substantive` | Phase 4 only (requires prior analysis) |
| `/org-triage ORG --issues` | Phase 5 only (requires prior analysis) |
| `/org-triage ORG --monitor` | Phase 6 only — check existing tracker |
| `/org-triage ORG` | All phases end-to-end |

---

## Parallelization Strategy

- **Phase 1:** Sequential (single API paginated call + per-repo label fetch) — haiku
- **Phase 2→3→4:** `pipeline(repos, analyze, fix, pr)` — each repo flows through all stages without waiting for others. Analysis on opus, fixing on sonnet.
- **Phase 5 issues:** Fan-out, one agent per issue — sonnet
- **Phase 5 triage:** Single agent, bash loop for all comments — haiku
- **Phase 6:** Sequential (single tracker file update) — haiku

Never exceed 25 concurrent agents. If the contribution list is longer, batch in waves of 15-20.

**Pattern dedup:** Before the fix stage in the pipeline, group findings by type. For patterns in 3+ repos, create one template and distribute to all fix agents for that pattern.

**Bundle community health files:** When a repo is missing multiple community health files (SECURITY.md, .editorconfig, CODEOWNERS, CONTRIBUTING.md, CHANGELOG.md), bundle them into a single "Add community health files" PR per repo. Do NOT open separate PRs for each file — that floods maintainers with noise and makes the contributor look like a bot. One well-structured PR with all missing files is easier to review and more likely to merge.

---

## Safety Rules

- Never force-push to a PR branch after opening
- Never open PRs to archived repos
- Never open duplicate PRs — check existing open PRs first
- Always GPG sign commits with user's configured key
- Always verify patterns with `grep` before editing a file
- Always read CONTRIBUTING.md before PRing — follow repo process
- If CLA required, warn user before proceeding
- DRY_RUN first — always do discovery+analysis before opening anything
- One PR per logical change — do not bundle unrelated fixes
- Rate limit — do not flood an org with 30 PRs simultaneously; space across repos
