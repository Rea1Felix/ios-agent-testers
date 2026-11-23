"""GitHub issue reference parsing and bug report formatting for reproduce mode."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

_GITHUB_URL_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([0-9]+)/?$"
)
_GITHUB_REF_RE = re.compile(
    r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)$"
)


def parse_github_issue_ref(raw: str) -> tuple[str, int] | None:
    """Parse a full GitHub issue URL or `owner/repo#123` reference.

    Returns `(owner_repo, issue_number)` when `raw` strictly matches one of the
    two GitHub issue forms (ignoring surrounding whitespace on a single line),
    or `None` when `raw` should be treated as plain-text bug report content.
    """
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    if not stripped or "\n" in stripped or "\r" in stripped:
        return None

    match = _GITHUB_URL_RE.fullmatch(stripped) or _GITHUB_REF_RE.fullmatch(stripped)
    if not match:
        return None

    repo = match.group(1)
    owner, name = repo.split("/", 1)
    if owner in (".", "..") or name in (".", ".."):
        return None

    issue_number = int(match.group(2))
    if issue_number <= 0:
        return None
    return repo, issue_number


def extract_bug_report_title(bug_report_md: str | None) -> str | None:
    """Extract the first non-empty heading/line from a bug report markdown string."""
    if not bug_report_md:
        return None
    for line in bug_report_md.splitlines():
        cleaned = line.lstrip("#").strip()
        if cleaned:
            return cleaned
    return None


def fetch_or_format_bug_report(raw: str) -> str:
    """Resolve a GitHub issue reference via `gh api` or format plain bug text.

    Matches upstream `scripts/bug_report.sh`:
    - Full GitHub issue URL or `owner/repo#N` -> fetches title, html_url, body
      via `gh api repos/<owner>/<repo>/issues/<N>` (first post only, no comments).
    - Otherwise -> preserves the plain text verbatim with a trailing newline.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("bug_report is empty")

    ref = parse_github_issue_ref(raw)
    if ref is None:
        return raw if raw.endswith("\n") else raw + "\n"

    repo, issue_number = ref
    gh_exe = shutil.which("gh")
    if not gh_exe:
        raise RuntimeError(
            f"Cannot fetch GitHub issue {repo}#{issue_number}: `gh` CLI not found on PATH."
        )

    proc = subprocess.run(
        [gh_exe, "api", f"repos/{repo}/issues/{issue_number}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(
            f"Failed to fetch GitHub issue {repo}#{issue_number} via gh api (exit {proc.returncode}): {err}"
        )

    try:
        payload = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid JSON returned by gh api for {repo}#{issue_number}: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected payload type from gh api for {repo}#{issue_number}")

    title = str(payload.get("title") or "").strip()
    html_url = str(
        payload.get("html_url") or f"https://github.com/{repo}/issues/{issue_number}"
    ).strip()
    body = payload.get("body")
    body_text = "" if body is None else str(body)

    formatted = f"# {title}\n\nReported in {html_url}\n\n{body_text}"
    return formatted if formatted.endswith("\n") else formatted + "\n"
