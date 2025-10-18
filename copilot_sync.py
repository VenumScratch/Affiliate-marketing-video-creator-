#!/usr/bin/env python3
"""
copilot_sync.py

Local-clone variant — run this inside an existing local clone of your repository.

Usage:
  python3 copilot_sync.py [branch]

Defaults:
  branch: add-docker-and-workflow

What it does:
- Detects the repo owner/name from the local git origin remote (falls back to asking the user)
- Creates or checks out the target branch
- Writes a Dockerfile at the repo root
- Writes .github/workflows/docker-publish.yml
- Commits and pushes the branch
- Attempts to create a pull request using the gh CLI, falling back to the GitHub API when GITHUB_TOKEN (or GH_TOKEN) is set

Requirements:
- python3
- git on PATH
- network access and credentials to push
- Optional: GitHub CLI (gh) configured for creating PRs; otherwise set GITHUB_TOKEN with repo permissions.

Example:
  cd path/to/your/local/clone
  python3 copilot_sync.py
  # or specify branch:
  python3 copilot_sync.py my-branch
"""
from __future__ import annotations
import os
import sys
import json
import subprocess
from pathlib import Path
from urllib import request, error

BRANCH_DEFAULT = "add-docker-and-workflow"
COMMIT_MSG = "Add Dockerfile and GitHub Actions workflow for Docker build/publish"
PR_TITLE = "Add Dockerfile and Docker publish workflow"
PR_BODY = (
    "Adds a Dockerfile at the repository root and a GitHub Actions workflow "
    "(.github/workflows/docker-publish.yml) to build and publish Docker images to GHCR. "
    "The workflow uses Buildx and docker/metadata-action, skips pushing on PRs, and "
    "installs cosign for signing when run outside PRs."
)

DOCKERFILE_CONTENT = """FROM node:20-alpine

WORKDIR /app

COPY package*.json ./

RUN npm install --production

COPY . .

EXPOSE 3000

CMD ["npm", "start"]
"""

WORKFLOW_CONTENT = """name: Docker

on:
  schedule:
    - cron: '43 20 * * *'
  push:
    branches: [ "main" ]
    tags: [ 'v*.*.*' ]
  pull_request:
    branches: [ "main" ]

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
      id-token: write

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Install cosign
        if: github.event_name != 'pull_request'
        uses: sigstore/cosign-installer@59acb6260d9c0ba8f4a2f9d9b48431a222b68e20 #v3.5.0
        with:
          cosign-release: 'v2.2.4'

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@f95db51fddba0c2d1ec667646a06c2ce06100226 # v3.0.0

      - name: Log into registry ${{ env.REGISTRY }}
        if: github.event_name != 'pull_request'
        uses: docker/login-action@343f7c4344506bcbf9b4de18042ae17996df046d # v3.0.0
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Extract Docker metadata
        id: meta
        uses: docker/metadata-action@96383f45573cb7f253c731d3b3ab81c87ef81934 # v5.0.0
        with:
          images: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}

      - name: Build and push Docker image
        id: build-and-push
        uses: docker/build-push-action@0565240e2d4ab88bba5387d719585280857ece09 # v5.0.0
        with:
          context: .
          push: ${{ github.event_name != 'pull_request' }}
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha
"""

def run(cmd, check=True):
    print("+", " ".join(cmd))
    return subprocess.run(cmd, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def get_origin_repo():
    try:
        cp = run(["git", "config", "--get", "remote.origin.url"])
        url = cp.stdout.strip()
        if not url:
            return None
        # Supports: git@github.com:owner/repo.git or https://github.com/owner/repo.git
        if url.startswith("git@"):  
            # git@github.com:owner/repo.git
            try:
                _, path = url.split(":", 1)
            except ValueError:
                return None
        elif url.startswith("https://") or url.startswith("http://"):
            # https://github.com/owner/repo.git
            path = url.split("github.com/", 1)[-1]
        else:
            path = url
        if path.endswith(".git"):
            path = path[:-4]
        if "/" in path:
            owner, repo = path.split("/", 1)
            return owner, repo
        return None
    except subprocess.CalledProcessError:
        return None

def safe_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

def create_pr_via_api(owner: str, repo: str, head: str, base: str = "main"):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        return False, "GITHUB_TOKEN (or GH_TOKEN) not set."
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    payload = {"title": PR_TITLE, "head": head, "base": base, "body": PR_BODY}
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, method="POST", headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "copilot-sync-local",
    })
    try:
        with request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            j = json.loads(body)
            return True, j.get("html_url", "PR created (no URL returned).")
    except error.HTTPError as he:
        try:
            err_body = he.read().decode()
        except Exception:
            err_body = "<no body>"
        return False, f"HTTPError {he.code}: {err_body}"
    except Exception as e:
        return False, str(e)

def main(argv):
    branch = argv[1] if len(argv) > 1 else BRANCH_DEFAULT

    repo_info = get_origin_repo()
    if repo_info is None:
        print("Could not detect origin remote. Please run this inside a git clone with 'origin' set, or supply owner/repo in environment variables.", file=sys.stderr)
        owner = os.environ.get("GIT_OWNER")
        repo = os.environ.get("GIT_REPO")
        if not owner or not repo:
            print("Export GIT_OWNER and GIT_REPO, or set remote origin correctly.", file=sys.stderr)
            return 2
    else:
        owner, repo = repo_info

    cwd = Path.cwd()
    print(f"Working in repository: {owner}/{repo} (path: {cwd})")
    # Create or checkout branch
    try:
        # if remote branch exists, checkout it; else create new
        rc = subprocess.run(["git", "ls-remote", "--exit-code", "--heads", "origin", branch], text=True)
        if rc.returncode == 0:
            run(["git", "checkout", branch])
        else:
            run(["git", "checkout", "-b", branch])
    except subprocess.CalledProcessError as e:
        print("Branch creation/checkout failed:", e.stderr or e, file=sys.stderr)
        return 3

    # Write files
    print("Writing Dockerfile to repo root and workflow to .github/workflows/")
    safe_write(cwd / "Dockerfile", DOCKERFILE_CONTENT)
    safe_write(cwd / ".github" / "workflows" / "docker-publish.yml", WORKFLOW_CONTENT)

    # Stage & commit
    try:
        run(["git", "add", "Dockerfile", ".github/workflows/docker-publish.yml"])
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if diff.returncode == 0:
            print("No changes to commit.")
        else:
            run(["git", "commit", "-m", COMMIT_MSG])
    except subprocess.CalledProcessError as e:
        print("git add/commit failed:", e.stderr or e, file=sys.stderr)
        return 4

    # Push branch
    try:
        run(["git", "push", "-u", "origin", branch])
    except subprocess.CalledProcessError as e:
        print("git push failed:", e.stderr or e, file=sys.stderr)
        return 5

    # Create PR using gh if available, else via API
    gh_path = None
    try:
        import shutil
        gh_path = shutil.which("gh")
    except Exception:
        gh_path = None

    if gh_path:
        print("Creating PR using gh CLI")
        try:
            cp = run(["gh", "pr", "create", "--repo", f"{owner}/{repo}", "--title", PR_TITLE, "--body", PR_BODY, "--base", "main", "--head", branch])
            print("gh output:\n", cp.stdout)
            return 0
        except subprocess.CalledProcessError as e:
            print("gh failed to create PR:", e.stderr or e, file=sys.stderr)
            # fall back to API
    else:
        print("gh CLI not found; attempting GitHub API if GITHUB_TOKEN is set.")

    ok, message = create_pr_via_api(owner, repo, branch, base="main")
    if ok:
        print("Pull request created:", message)
        return 0
    else:
        print("Failed to create PR automatically:", message, file=sys.stderr)
        print(f"Open a PR manually: https://github.com/{owner}/{repo}/compare/main...{branch}?expand=1")
        return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))