#!/usr/bin/env bash
# sync_github.sh — Auto-commit and push polymarket changes to GitHub
#
# Usage:
#   ./sync_github.sh                  # auto-detect message from changed files
#   ./sync_github.sh "my message"     # use a custom commit message
#
# Can be run manually or scheduled via cron / launchd.
# Requires git credentials to be pre-configured (SSH key or stored token).

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRANCH="${GIT_BRANCH:-develop}"
REMOTE="${GIT_REMOTE:-origin}"
LOG_FILE="${REPO_DIR}/logs/sync_github.log"
MAX_LOG_LINES=1000   # rotate log after this many lines

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
CYN='\033[0;36m'
DIM='\033[2m'
RST='\033[0m'

# ── Helpers ──────────────────────────────────────────────────────────────────
ts()  { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo -e "$(ts)  $*" | tee -a "$LOG_FILE"; }

rotate_log() {
    if [[ -f "$LOG_FILE" ]]; then
        local lines
        lines=$(wc -l < "$LOG_FILE")
        if (( lines > MAX_LOG_LINES )); then
            tail -n $((MAX_LOG_LINES / 2)) "$LOG_FILE" > "${LOG_FILE}.tmp"
            mv "${LOG_FILE}.tmp" "$LOG_FILE"
        fi
    fi
}

# Build an automatic commit message from the list of changed files
auto_message() {
    local staged modified untracked files_summary

    staged=$(git diff --cached --name-only 2>/dev/null || true)
    modified=$(git diff --name-only 2>/dev/null || true)
    untracked=$(git ls-files --others --exclude-standard 2>/dev/null || true)

    # Combine all changed file names (unique, sorted)
    local all_files
    all_files=$(printf '%s\n%s\n%s\n' "$staged" "$modified" "$untracked" \
                | grep -v '^$' | sort -u)

    if [[ -z "$all_files" ]]; then
        echo "chore: sync $(ts)"
        return
    fi

    local count
    count=$(echo "$all_files" | wc -l | tr -d ' ')

    # Categorise by prefix
    local has_src=0 has_docs=0 has_radar=0 has_other=0
    while IFS= read -r f; do
        [[ "$f" == src/* ]]           && has_src=1
        [[ "$f" == docs/* ]]          && has_docs=1
        [[ "$f" == radar_poly.py ]]   && has_radar=1
        [[ "$f" != src/* && "$f" != docs/* && "$f" != radar_poly.py ]] && has_other=1
    done <<< "$all_files"

    local parts=()
    (( has_radar )) && parts+=("main loop")
    (( has_src ))   && parts+=("src modules")
    (( has_docs ))  && parts+=("docs")
    (( has_other )) && parts+=("misc")

    local scope
    scope=$(IFS=', '; echo "${parts[*]}")

    if (( count == 1 )); then
        files_summary=$(echo "$all_files" | head -1)
        echo "chore: update ${files_summary}"
    else
        echo "chore: sync ${count} files (${scope})"
    fi
}

# ── Main ─────────────────────────────────────────────────────────────────────
main() {
    mkdir -p "$(dirname "$LOG_FILE")"
    rotate_log

    log "${CYN}── sync_github ──────────────────────────────${RST}"

    cd "$REPO_DIR"

    # Verify this is a git repository
    if ! git rev-parse --git-dir > /dev/null 2>&1; then
        log "${RED}✗ Not a git repository: ${REPO_DIR}${RST}"
        exit 1
    fi

    # Make sure we're on the right branch
    current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    if [[ "$current_branch" != "$BRANCH" ]]; then
        log "${YLW}⚠  On branch '${current_branch}', expected '${BRANCH}' — switching...${RST}"
        git checkout "$BRANCH" 2>&1 | tee -a "$LOG_FILE" || {
            log "${RED}✗ Could not switch to branch '${BRANCH}'.${RST}"
            exit 1
        }
    fi

    # Check for any changes (staged, unstaged, or untracked)
    local has_staged has_unstaged has_untracked
    has_staged=$(git diff --cached --name-only)
    has_unstaged=$(git diff --name-only)
    has_untracked=$(git ls-files --others --exclude-standard \
                    | grep -v '__pycache__' \
                    | grep -v '\.pyc$' \
                    | grep -v '^venv/' \
                    | grep -v '\.env$' \
                    | grep -v '^logs/' \
                    | head -1)

    if [[ -z "$has_staged" && -z "$has_unstaged" && -z "$has_untracked" ]]; then
        log "${DIM}✓ Nothing to commit — working tree clean.${RST}"
        exit 0
    fi

    # Stage everything (respects .gitignore)
    log "${DIM}Staging changes...${RST}"
    git add --all 2>&1 | tee -a "$LOG_FILE"

    # Re-check after staging (gitignore might have filtered everything)
    if git diff --cached --quiet; then
        log "${DIM}✓ Nothing to commit after staging (all changes are gitignored).${RST}"
        exit 0
    fi

    # Show what we're about to commit
    log "${DIM}Changed files:${RST}"
    git diff --cached --name-status | while IFS= read -r line; do
        log "   ${DIM}${line}${RST}"
    done

    # Determine commit message
    local commit_msg
    if [[ $# -gt 0 && -n "${1:-}" ]]; then
        commit_msg="$1"
    else
        commit_msg=$(auto_message)
    fi
    log "${DIM}Commit: ${commit_msg}${RST}"

    # Commit
    git commit -m "$commit_msg" 2>&1 | tee -a "$LOG_FILE"

    # Push with retry (up to 3 attempts with backoff)
    local attempt=0
    local max_attempts=3
    local push_ok=0

    while (( attempt < max_attempts )); do
        (( attempt++ ))
        log "${DIM}Pushing to ${REMOTE}/${BRANCH} (attempt ${attempt}/${max_attempts})...${RST}"

        if git push "$REMOTE" "$BRANCH" 2>&1 | tee -a "$LOG_FILE"; then
            push_ok=1
            break
        else
            if (( attempt < max_attempts )); then
                local wait=$(( attempt * 15 ))
                log "${YLW}⚠  Push failed — retrying in ${wait}s...${RST}"
                sleep "$wait"
            fi
        fi
    done

    if (( push_ok )); then
        local sha
        sha=$(git rev-parse --short HEAD)
        log "${GRN}✓ Pushed ${sha} → ${REMOTE}/${BRANCH}${RST}"
    else
        log "${RED}✗ Push failed after ${max_attempts} attempts. Changes are committed locally.${RST}"
        exit 1
    fi
}

main "$@"
