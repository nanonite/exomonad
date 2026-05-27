#!/usr/bin/env python3
"""Mock GitHub API server for E2E testing.

Stateful HTTP server that mimics GitHub's REST API for PR workflows.
Supports per-PR review queues controlled via a test-driver control API.

Usage:
    MOCK_LOG=/tmp/mock.log REMOTE_DIR=/path/to/bare.git python3 mock_github.py --port 8888

Endpoints:
    GET  /repos/{owner}/{repo}/pulls          - List open PRs
    POST /repos/{owner}/{repo}/pulls          - Create a PR
    PATCH /repos/{owner}/{repo}/pulls/{n}     - Update PR body/title
    PUT  /repos/{owner}/{repo}/pulls/{n}/merge - Merge a PR
    GET  /repos/{owner}/{repo}/pulls/{n}/reviews - Get reviews (from review queue)
    GET  /repos/{owner}/{repo}/pulls/{n}      - Get PR details
    GET  /repos/{owner}/{repo}/pulls/{n}/comments - Get PR comments (empty array)
    GET  /repos/{owner}/{repo}/commits/{sha}/check-runs - Get commit check runs

Control API (test driver only):
    POST /_control/reviews  - Add a review to a PR's review queue

All requests logged to $MOCK_LOG (one JSON object per line).
"""

import argparse
import datetime
import json
import os
import re
import signal
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def mock_author(login, uid=1):
    """Build a complete GitHub Author object matching octocrab's strict deserialization."""
    base = f"https://api.github.com/users/{login}"
    return {
        "login": login,
        "id": uid,
        "node_id": f"U_{login}",
        "avatar_url": f"https://avatars.githubusercontent.com/u/{uid}",
        "gravatar_id": "",
        "url": base,
        "html_url": f"https://github.com/{login}",
        "followers_url": f"{base}/followers",
        "following_url": f"{base}/following{{/other_user}}",
        "gists_url": f"{base}/gists{{/gist_id}}",
        "starred_url": f"{base}/starred{{/owner}}{{/repo}}",
        "subscriptions_url": f"{base}/subscriptions",
        "organizations_url": f"{base}/orgs",
        "repos_url": f"{base}/repos",
        "events_url": f"{base}/events{{/privacy}}",
        "received_events_url": f"{base}/received_events",
        "type": "User",
        "site_admin": False,
    }


def mock_repo(owner, repo):
    """Build a Repository object with required fields for octocrab."""
    return {
        "id": 1,
        "node_id": "R_mock1",
        "name": repo,
        "full_name": f"{owner}/{repo}",
        "private": False,
        "fork": False,
        "url": f"https://api.github.com/repos/{owner}/{repo}",
        "html_url": f"https://github.com/{owner}/{repo}",
        "owner": mock_author(owner),
    }


def resolve_sha(branch):
    """Resolve current SHA for a branch from the bare remote repo."""
    remote_dir = os.environ.get("REMOTE_DIR")
    if remote_dir:
        try:
            result = subprocess.run(
                ["git", "-C", remote_dir, "rev-parse", branch],
                capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
    return None


class GitHubState:
    def __init__(self):
        self.prs = {}
        self.reviews = {}       # {pr_number: [review_obj, ...]}
        self.next_pr_number = 1
        self.next_review_id = 1

    def create_pr(self, owner, repo, title, head_ref, base_ref, body=""):
        number = self.next_pr_number
        self.next_pr_number += 1
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        head_sha = resolve_sha(head_ref) or f"deadbeef{number:04x}"
        base_sha = resolve_sha(base_ref) or f"cafebabe{number:04x}"
        pr = {
            "number": number,
            "id": 100 + number,
            "node_id": f"PR_mock{number}",
            "title": title,
            "body": body,
            "head": {
                "ref": head_ref,
                "sha": head_sha,
                "label": f"{owner}:{head_ref}",
                "repo": mock_repo(owner, repo),
            },
            "base": {
                "ref": base_ref,
                "sha": base_sha,
                "label": f"{owner}:{base_ref}",
                "repo": mock_repo(owner, repo),
            },
            "state": "open",
            "html_url": f"https://github.com/{owner}/{repo}/pull/{number}",
            "url": f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}",
            "user": mock_author("test-bot"),
            "created_at": now,
            "updated_at": now,
            "merged": False,
            "mergeable": True,
            "merge_commit_sha": None,
            "draft": False,
        }
        self.prs[number] = pr
        return pr

    def merge_pr(self, number):
        if number in self.prs:
            self.prs[number]["state"] = "closed"
            self.prs[number]["updated_at"] = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
            return True
        return False

    def add_review(self, pr_number, review_state, body=""):
        """Add a review to a PR's review queue."""
        review_id = self.next_review_id
        self.next_review_id += 1
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        review = {
            "id": review_id,
            "node_id": f"PRR_{review_id}",
            "html_url": f"https://github.com/test/repo/pull/{pr_number}#pullrequestreview-{review_id}",
            "user": mock_author("copilot[bot]", uid=2),
            "state": review_state,
            "body": body,
            "submitted_at": now,
        }
        if pr_number not in self.reviews:
            self.reviews[pr_number] = []
        self.reviews[pr_number].append(review)
        return review

    def get_reviews(self, pr_number):
        """Get all reviews for a PR. Returns empty list if none posted."""
        return self.reviews.get(pr_number, [])

    def update_pr_sha(self, pr_number):
        """Update a PR's head SHA from the git remote (live tracking)."""
        if pr_number not in self.prs:
            return
        pr = self.prs[pr_number]
        branch = pr["head"]["ref"]
        sha = resolve_sha(branch)
        if sha:
            pr["head"]["sha"] = sha


state = GitHubState()


class GitHubMockHandler(BaseHTTPRequestHandler):
    def _log_request(self, status_code):
        log_path = os.environ.get("MOCK_LOG", "/tmp/mock_github.log")
        log_entry = {
            "method": self.command,
            "path": self.path,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status_code": status_code,
        }
        try:
            with open(log_path, "a") as f:
                f.write(json.dumps(log_entry) + "\n")
        except Exception as e:
            sys.stderr.write(f"Failed to write log: {e}\n")

    def _send_json(self, data, status_code=200):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))
        self._log_request(status_code)

    def _send_error(self, status_code, message):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"message": message}).encode("utf-8"))
        self._log_request(status_code)

    def _path_only(self):
        """Strip query string from self.path for route matching."""
        return self.path.split("?")[0]

    def _query_params(self):
        """Parse query string into dict."""
        from urllib.parse import parse_qs, urlparse
        return parse_qs(urlparse(self.path).query)

    def _read_body(self):
        """Read and parse JSON request body."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        return json.loads(body) if body else {}

    def do_GET(self):
        path = self._path_only()

        # GET /repos/{owner}/{repo}/pulls
        m = re.match(r"^/repos/([^/]+)/([^/]+)/pulls$", path)
        if m:
            params = self._query_params()
            # Update SHAs from git remote before returning
            for pr_number in list(state.prs.keys()):
                state.update_pr_sha(pr_number)
            open_prs = [pr for pr in state.prs.values() if pr["state"] == "open"]
            # Filter by head branch if specified
            head_filter = params.get("head", [None])[0]
            if head_filter:
                # octocrab sends "owner:branch" or just "branch"
                open_prs = [
                    pr for pr in open_prs
                    if pr["head"]["ref"] == head_filter
                    or pr["head"]["ref"] == head_filter.split(":")[-1]
                ]
            return self._send_json(open_prs)

        # GET /repos/{owner}/{repo}/pulls/{n}/reviews
        m = re.match(r"^/repos/([^/]+)/([^/]+)/pulls/(\d+)/reviews$", path)
        if m:
            pr_number = int(m.group(3))
            if pr_number in state.prs:
                return self._send_json(state.get_reviews(pr_number))
            return self._send_error(404, "PR not found")

        # GET /repos/{owner}/{repo}/pulls/{n}/comments
        m = re.match(r"^/repos/([^/]+)/([^/]+)/pulls/(\d+)/comments$", path)
        if m:
            pr_number = int(m.group(3))
            if pr_number in state.prs:
                return self._send_json([])
            return self._send_error(404, "PR not found")

        # GET /repos/{owner}/{repo}/pulls/{n}
        m = re.match(r"^/repos/([^/]+)/([^/]+)/pulls/(\d+)$", path)
        if m:
            pr_number = int(m.group(3))
            if pr_number in state.prs:
                state.update_pr_sha(pr_number)
                return self._send_json(state.prs[pr_number])
            return self._send_error(404, "PR not found")

        # GET /repos/{owner}/{repo}/commits/{sha}/check-runs
        m = re.match(r"^/repos/([^/]+)/([^/]+)/commits/([^/]+)/check-runs$", path)
        if m:
            owner, repo, sha = m.groups()
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            data = {
                "total_count": 1,
                "check_runs": [
                    {
                        "id": 1,
                        "node_id": "CR_kw_mock1",
                        "head_sha": sha,
                        "url": f"https://api.github.com/repos/{owner}/{repo}/check-runs/1",
                        "html_url": f"https://github.com/{owner}/{repo}/runs/1",
                        "details_url": None,
                        "name": "build",
                        "conclusion": "success",
                        "output": {
                            "title": None,
                            "summary": None,
                            "text": None,
                            "annotations_count": 0,
                            "annotations_url": f"https://api.github.com/repos/{owner}/{repo}/check-runs/1/annotations",
                        },
                        "started_at": now,
                        "completed_at": now,
                    }
                ],
            }
            return self._send_json(data)

        return self._send_error(404, "Not Found")

    def do_POST(self):
        path = self._path_only()

        # POST /_control/reviews — test driver adds a review to a PR
        if path == "/_control/reviews":
            try:
                data = self._read_body()
                pr_number = data.get("pr_number")
                review_state = data.get("state", "APPROVED")
                body = data.get("body", "")
                if pr_number is None or pr_number not in state.prs:
                    return self._send_error(400, f"PR {pr_number} not found")
                review = state.add_review(pr_number, review_state, body)
                return self._send_json(review, 201)
            except (json.JSONDecodeError, KeyError) as e:
                return self._send_error(400, f"Invalid request: {e}")

        # POST /repos/{owner}/{repo}/pulls
        m = re.match(r"^/repos/([^/]+)/([^/]+)/pulls$", path)
        if m:
            owner, repo = m.groups()
            try:
                data = self._read_body()
                title = data.get("title", "No Title")
                head = data.get("head", "main")
                base = data.get("base", "main")
                body = data.get("body", "")
                pr = state.create_pr(owner, repo, title, head, base, body)
                return self._send_json(pr, 201)
            except json.JSONDecodeError:
                return self._send_error(400, "Invalid JSON")

        return self._send_error(404, "Not Found")

    def do_PUT(self):
        path = self._path_only()

        # PUT /repos/{owner}/{repo}/pulls/{n}/merge
        m = re.match(r"^/repos/([^/]+)/([^/]+)/pulls/(\d+)/merge$", path)
        if m:
            pr_number = int(m.group(3))
            if state.merge_pr(pr_number):
                return self._send_json({"merged": True, "sha": "abc123"})
            return self._send_error(404, "PR not found")

        return self._send_error(404, "Not Found")

    def do_PATCH(self):
        path = self._path_only()

        # PATCH /repos/{owner}/{repo}/pulls/{n} — update PR body/title
        m = re.match(r"^/repos/([^/]+)/([^/]+)/pulls/(\d+)$", path)
        if m:
            pr_number = int(m.group(3))
            if pr_number not in state.prs:
                return self._send_error(404, "PR not found")
            try:
                data = self._read_body()
                pr = state.prs[pr_number]
                if "body" in data:
                    pr["body"] = data["body"]
                if "title" in data:
                    pr["title"] = data["title"]
                pr["updated_at"] = datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat()
                return self._send_json(pr)
            except json.JSONDecodeError:
                return self._send_error(400, "Invalid JSON")

        return self._send_error(404, "Not Found")

def run_server():
    parser = argparse.ArgumentParser(description="Mock GitHub API server")
    parser.add_argument("--host", default="127.0.0.1", help="Host to listen on")
    parser.add_argument("--port", type=int, default=8888, help="Port to listen on")
    args = parser.parse_args()

    httpd = HTTPServer((args.host, args.port), GitHubMockHandler)

    def signal_handler(sig, frame):
        sys.stderr.write("\nShutting down Mock GitHub API...\n")
        httpd.server_close()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    sys.stderr.write(f"Mock GitHub API listening on {args.host}:{args.port}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run_server()
