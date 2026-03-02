#!/usr/bin/env bash
# setup_github_auth.sh — One-time GitHub authentication setup
#
# Stores your GitHub credentials so that sync_github.sh (and all future
# git push / pull commands) work without prompting for a password.
#
# Supported auth methods (choose one):
#   1. Personal Access Token (HTTPS) — recommended, easiest setup
#   2. SSH key                       — no tokens, more secure long-term
#
# Usage:
#   chmod +x setup_github_auth.sh
#   ./setup_github_auth.sh

set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
CYN='\033[0;36m'
WHT='\033[1;37m'
DIM='\033[2m'
RST='\033[0m'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_URL="https://github.com/otreci4sgelt0nas/polymarket"
BRANCH="develop"

hr()  { echo -e "${DIM}────────────────────────────────────────────────────${RST}"; }
hdr() { echo -e "\n${CYN}${WHT}$*${RST}\n"; }
ok()  { echo -e "${GRN}✓  $*${RST}"; }
err() { echo -e "${RED}✗  $*${RST}"; }
inf() { echo -e "${DIM}   $*${RST}"; }
ask() { echo -en "${WHT}▶  $*${RST} "; }

# ── Sanity checks ────────────────────────────────────────────────────────────
check_prerequisites() {
    hdr "Checking prerequisites"

    if ! command -v git &>/dev/null; then
        err "git is not installed. Please install git first."
        exit 1
    fi
    ok "git $(git --version | awk '{print $3}')"

    cd "$REPO_DIR"
    if ! git rev-parse --git-dir &>/dev/null; then
        err "Not a git repository: ${REPO_DIR}"
        exit 1
    fi
    ok "Repository: ${REPO_DIR}"

    local current_remote
    current_remote=$(git remote get-url origin 2>/dev/null || echo "none")
    ok "Remote: ${current_remote}"

    local pending
    pending=$(git log origin/develop..HEAD --oneline 2>/dev/null | wc -l | tr -d ' ')
    if (( pending > 0 )); then
        ok "${pending} commit(s) ready to push"
    else
        inf "No pending commits (working tree is in sync)"
    fi
}

# ── Method 1: Personal Access Token ─────────────────────────────────────────
setup_pat() {
    hdr "Setup: Personal Access Token (HTTPS)"

    echo -e "${YLW}You need a GitHub Personal Access Token (PAT) with 'repo' scope.${RST}"
    echo
    echo -e "  ${WHT}How to create one:${RST}"
    inf "1. Open https://github.com/settings/tokens"
    inf "2. Click 'Generate new token (classic)'"
    inf "3. Give it a name (e.g. 'polymarket-sync')"
    inf "4. Check the 'repo' checkbox"
    inf "5. Click 'Generate token' and copy the token"
    echo
    echo -e "${RED}⚠  The token is shown ONCE — copy it before closing the page.${RST}"
    echo

    ask "GitHub username:"
    read -r gh_user

    ask "Personal Access Token (input hidden):"
    read -rs gh_token
    echo  # newline after hidden input

    if [[ -z "$gh_user" || -z "$gh_token" ]]; then
        err "Username or token is empty — aborting."
        exit 1
    fi

    # Configure git to use credential store
    git config credential.helper store

    # Write credentials to ~/.git-credentials
    # Format: https://user:token@github.com
    local cred_file="${HOME}/.git-credentials"
    local cred_line="https://${gh_user}:${gh_token}@github.com"

    # Remove any existing entry for github.com to avoid duplicates
    if [[ -f "$cred_file" ]]; then
        grep -v "github.com" "$cred_file" > "${cred_file}.tmp" || true
        mv "${cred_file}.tmp" "$cred_file"
    fi

    echo "$cred_line" >> "$cred_file"
    chmod 600 "$cred_file"
    ok "Credentials saved to ${cred_file} (chmod 600)"

    # Update git config identity if not already set
    local cfg_email cfg_name
    cfg_email=$(git config user.email 2>/dev/null || echo "")
    cfg_name=$(git config user.name 2>/dev/null || echo "")

    if [[ -z "$cfg_email" ]]; then
        ask "Git commit email (press Enter to use ${gh_user}@users.noreply.github.com):"
        read -r input_email
        cfg_email="${input_email:-${gh_user}@users.noreply.github.com}"
        git config user.email "$cfg_email"
        ok "git user.email = ${cfg_email}"
    else
        ok "git user.email already set: ${cfg_email}"
    fi

    if [[ -z "$cfg_name" ]]; then
        ask "Git display name (press Enter to use ${gh_user}):"
        read -r input_name
        cfg_name="${input_name:-${gh_user}}"
        git config user.name "$cfg_name"
        ok "git user.name  = ${cfg_name}"
    else
        ok "git user.name already set: ${cfg_name}"
    fi

    echo
    ok "PAT authentication configured."
    push_pending_commits
}

# ── Method 2: SSH key ────────────────────────────────────────────────────────
setup_ssh() {
    hdr "Setup: SSH Key"

    local key_file="${HOME}/.ssh/id_ed25519"
    local pub_file="${key_file}.pub"

    # Check for an existing SSH key
    if [[ -f "$pub_file" ]]; then
        echo -e "${YLW}Existing SSH public key found:${RST}"
        echo
        cat "$pub_file"
        echo
    else
        echo -e "${YLW}No SSH key found — generating one now.${RST}"
        echo

        ask "Email for SSH key label (e.g. you@example.com):"
        read -r ssh_email
        if [[ -z "$ssh_email" ]]; then
            err "Email is required for SSH key generation."
            exit 1
        fi

        ssh-keygen -t ed25519 -C "$ssh_email" -f "$key_file" -N ""
        echo
        ok "SSH key generated: ${key_file}"
        echo
        echo -e "${YLW}Your new public key:${RST}"
        echo
        cat "$pub_file"
        echo
    fi

    echo -e "${WHT}Add the public key above to GitHub:${RST}"
    inf "1. Open https://github.com/settings/keys"
    inf "2. Click 'New SSH key'"
    inf "3. Paste the key and save"
    echo
    ask "Press Enter once the key has been added to GitHub..."
    read -r

    # Switch remote from HTTPS to SSH
    local ssh_remote="git@github.com:otreci4sgelt0nas/polymarket.git"
    git remote set-url origin "$ssh_remote"
    ok "Remote updated to SSH: ${ssh_remote}"

    # Start ssh-agent and add key if needed
    if ! ssh-add -l &>/dev/null; then
        eval "$(ssh-agent -s)"
        ssh-add "$key_file"
        ok "SSH key added to agent"
    fi

    # Test connection
    echo
    inf "Testing SSH connection to GitHub..."
    if ssh -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
        ok "SSH connection to GitHub is working."
    else
        echo -e "${YLW}⚠  Could not confirm SSH connection. Trying push anyway...${RST}"
    fi

    push_pending_commits
}

# ── Push pending commits ─────────────────────────────────────────────────────
push_pending_commits() {
    hr
    hdr "Pushing commits to GitHub"

    cd "$REPO_DIR"

    local pending
    pending=$(git log origin/develop..HEAD --oneline 2>/dev/null | wc -l | tr -d ' ')

    if (( pending == 0 )); then
        inf "Nothing to push — remote is already up to date."
        return
    fi

    echo -e "${DIM}Commits to be pushed:${RST}"
    git log origin/develop..HEAD --oneline | while IFS= read -r line; do
        inf "$line"
    done
    echo

    if git push origin "$BRANCH"; then
        echo
        ok "Successfully pushed ${pending} commit(s) → origin/${BRANCH}"
        ok "GitHub: https://github.com/otreci4sgelt0nas/polymarket/tree/${BRANCH}"
    else
        err "Push failed. Check the error above and try again."
        exit 1
    fi
}

# ── Activate sync automation ─────────────────────────────────────────────────
offer_launchd_install() {
    hr
    hdr "Optional: Schedule automatic sync (macOS only)"

    if [[ "$(uname)" != "Darwin" ]]; then
        inf "Not macOS — skipping launchd setup."
        inf "On Linux, add to cron instead:"
        inf "  crontab -e"
        inf "  */30 * * * * /bin/bash ${REPO_DIR}/sync_github.sh >> ${REPO_DIR}/logs/sync.log 2>&1"
        return
    fi

    local plist_src="${REPO_DIR}/com.polymarket.sync.plist"
    local plist_dst="${HOME}/Library/LaunchAgents/com.polymarket.sync.plist"

    if [[ ! -f "$plist_src" ]]; then
        inf "com.polymarket.sync.plist not found — skipping launchd setup."
        return
    fi

    ask "Install launchd agent to auto-sync every 30 minutes? [y/N]"
    read -r yn
    if [[ "${yn,,}" != "y" ]]; then
        inf "Skipped. You can install later with:"
        inf "  cp ${plist_src} ~/Library/LaunchAgents/"
        inf "  launchctl load ~/Library/LaunchAgents/com.polymarket.sync.plist"
        return
    fi

    # Patch the plist path in case the repo is not at /home/camper/polymarket
    sed "s|/home/camper/polymarket|${REPO_DIR}|g" "$plist_src" > "$plist_dst"

    # Unload first in case it was previously installed
    launchctl unload "$plist_dst" 2>/dev/null || true
    launchctl load "$plist_dst"

    ok "launchd agent installed and started."
    ok "Sync will run every 30 minutes automatically."
    inf "Logs: ${REPO_DIR}/logs/sync_github.log"
    inf "To stop: launchctl unload ${plist_dst}"
}

# ── Menu ─────────────────────────────────────────────────────────────────────
show_menu() {
    echo
    echo -e "${CYN}${WHT}  POLYMARKET — GitHub Authentication Setup${RST}"
    hr
    echo
    echo -e "  ${WHT}1)${RST} Personal Access Token  ${DIM}(HTTPS — recommended)${RST}"
    echo -e "  ${WHT}2)${RST} SSH Key                ${DIM}(no token, more secure)${RST}"
    echo -e "  ${WHT}3)${RST} Push only              ${DIM}(credentials already configured)${RST}"
    echo -e "  ${WHT}q)${RST} Quit"
    echo
    ask "Choose [1/2/3/q]:"
    read -r choice

    case "$choice" in
        1) setup_pat ;;
        2) setup_ssh ;;
        3) push_pending_commits ;;
        q|Q) echo -e "${DIM}Bye.${RST}"; exit 0 ;;
        *) err "Invalid choice."; show_menu ;;
    esac
}

# ── Entry point ──────────────────────────────────────────────────────────────
check_prerequisites
show_menu
offer_launchd_install

hr
echo
ok "All done! Future syncs:"
echo
inf "Manual:    ./sync_github.sh"
inf "Automatic: already scheduled (if you chose to install the agent)"
inf "Log:       tail -f ${REPO_DIR}/logs/sync_github.log"
echo
