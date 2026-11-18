"""Auditable workflow decisions; GitHub data is read as JSON, never shell code."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

MARKER = "owner-approved-self-merge"


def api(path, paginate=False):
    args = ["gh", "api", path]
    if paginate:
        pages = json.loads(subprocess.check_output(args + ["--paginate", "--slurp"], text=True))
        return [item for page in pages for item in page]
    return json.loads(subprocess.check_output(args, text=True))


def merged_pr_number(sha, pulls, repository, branch="main"):
    for pr in pulls:
        base = pr.get("base", {})
        if (pr.get("merged_at") and pr.get("merge_commit_sha") == sha
                and base.get("ref") == branch
                and base.get("repo", {}).get("full_name", "").lower() == repository.lower()):
            return pr["number"]
    return None


def review_gate(pr, reviews, comments, owner):
    if pr.get("draft"):
        return [], []
    author = pr["user"]["login"]
    head = pr["head"]["sha"]
    latest = {}
    for review in sorted(reviews, key=lambda value: value["id"]):
        login = review["user"]["login"]
        if login != author and review["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[login] = review
    if any(r["state"] == "CHANGES_REQUESTED" for r in latest.values()):
        return ["Outstanding changes-requested review."], []
    if any(r["state"] == "APPROVED" and r.get("commit_id") == head for r in latest.values()):
        return [], []
    marked = MARKER in (pr.get("body") or "").splitlines() or any(
        label["name"] == MARKER for label in pr.get("labels", []))
    decision = None
    for comment in sorted(comments, key=lambda value: value["id"]):
        if comment["user"]["login"].lower() != owner.lower():
            continue
        lines = (comment.get("body") or "").splitlines()
        if f"owner-revoked-self-merge: {head}" in lines:
            decision = None
        elif f"{MARKER}: {head}" in lines:
            decision = comment["id"]
    self_review = [r for r in reviews if r["user"]["login"] == author
                   and r["state"] == "COMMENTED" and r.get("commit_id") == head
                   and (r.get("body") or "").startswith("Self-review passed")]
    if marked and decision and self_review:
        return [], [f"{MARKER}: owner comment {decision}; head {head}; review {self_review[-1]['id']}. "
                    "Explicit exception, not independent approval."]
    return ["Non-draft PR requires current-head non-author approval, or a commit-specific owner "
            "exception plus current-head self-review."], []


def committed_files(pr):
    """Scan the PR's actual commit, not an unrelated checkout or edited worktree."""
    head = pr["head"]["sha"]
    local_head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if local_head != head:
        raise SystemExit(f"Local HEAD {local_head} does not match PR head {head}; check out the current PR commit.")
    base = pr["base"]["sha"]
    subprocess.run(["git", "cat-file", "-e", f"{base}^{{commit}}"], check=True)
    names = subprocess.check_output(
        ["git", "diff", "--name-only", "-z", "--diff-filter=ACMRT", f"{base}...{head}"], text=True)
    for rel in filter(None, names.split("\0")):
        blob = f"{head}:{rel}"
        size = int(subprocess.check_output(["git", "cat-file", "-s", blob], text=True))
        content = None if size > 50 * 1024 * 1024 else subprocess.check_output(
            ["git", "show", blob]).decode("utf-8", errors="replace")
        yield rel, size, content


def pr_policy():
    repo, number = os.environ["GITHUB_REPOSITORY"], os.environ["PR_NUMBER"]
    pr = api(f"repos/{repo}/pulls/{number}")
    reviews = api(f"repos/{repo}/pulls/{number}/reviews?per_page=100", paginate=True)
    branch, base, body = pr["head"]["ref"], pr["base"]["ref"], pr.get("body") or ""
    comments = api(f"repos/{repo}/issues/{number}/comments?per_page=100", paginate=True)
    failures, warnings = review_gate(pr, reviews, comments, repo.split("/")[0])
    if not re.fullmatch(r"(agent|feat|fix|docs|exp|infra|chore|wip|backup)/[A-Za-z0-9._/-]+", branch):
        failures.append("Invalid branch name.")
    if not re.search(r"(?:Closes|Fixes|Resolves|Refs)\s+#\d+", body, re.I):
        failures.append("PR body must reference an issue.")
    for number in sorted(set(re.findall(r"#(\d+)", body))):
        api(f"repos/{repo}/issues/{number}")
    patterns = [r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"gh[pousr]_[A-Za-z0-9]{36,}",
                r"github_pat_[A-Za-z0-9_]{22,}", r"AKIA[0-9A-Z]{16}", r"xox[baprs]-[0-9A-Za-z-]{10,}"]
    for rel, size, content in committed_files(pr):
        if size > 50 * 1024 * 1024:
            failures.append(f"Large file not allowed: {rel}")
            continue
        if any(re.search(pattern, content) for pattern in patterns):
            failures.append(f"Possible secret pattern: {rel}")
    report = "\n".join(["PR policy report", f"Head: {pr['head']['sha']}"] + warnings + failures)
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(report + "\n")
    for warning in warnings:
        print("::warning::" + warning)
    if failures:
        raise SystemExit(1)


def main_guard():
    repo, sha, before = (os.environ[k] for k in ("GITHUB_REPOSITORY", "COMMIT", "BEFORE"))
    subprocess.run(["git", "merge-base", "--is-ancestor", before, sha], check=True)
    commits = subprocess.check_output(
        ["git", "rev-list", "--first-parent", f"{before}..{sha}"], text=True).splitlines()
    unexplained = []
    for commit in commits:
        pulls = api(f"repos/{repo}/commits/{commit}/pulls?per_page=100", paginate=True)
        number = merged_pr_number(commit, pulls, repo)
        if number:
            print(f"{commit}: merged PR #{number}")
        else:
            unexplained.append(commit)
    if not unexplained:
        print("Main guard passed: all new first-parent commits match merged PRs.")
        return
    title = "Direct push to main detected"
    body = ("No matching merged PR for these first-parent commits:\n\n"
            + "\n".join("- " + commit for commit in unexplained)
            + f"\n\nActor: {os.environ['ACTOR']}\nRun: {os.environ['RUN_URL']}\n"
              "Open a revert PR or an explanatory follow-up PR. Commit-message markers do not exempt pushes.")
    existing = json.loads(subprocess.check_output(
        ["gh", "issue", "list", "--repo", repo, "--state", "open", "--search", title + " in:title",
         "--json", "number,title"], text=True))
    matching = next((issue for issue in existing if issue["title"] == title), None)
    args = (["gh", "issue", "comment", str(matching["number"])] if matching
            else ["gh", "issue", "create", "--title", title, "--label", "P0"])
    subprocess.run(args + ["--repo", repo, "--body", body], check=True)
    raise SystemExit("Main guard failed: direct push evidence recorded.")


if __name__ == "__main__":
    {"pr": pr_policy, "main": main_guard}[sys.argv[1]]()
