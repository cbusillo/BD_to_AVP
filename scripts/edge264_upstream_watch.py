#!/usr/bin/env python3
"""Open or refresh the issue that reports a newer upstream edge264 tag than the pin.

The weekly edge264 Upstream Watch workflow runs this with the commit that
``build_edge264_macos.py --pending-update`` printed. With no open tracking issue it
opens one; with an open issue that names an older commit it rewrites the tag
sentence in the body and comments so watchers hear about the newer tag; with an
open issue that already names the commit it does nothing.
"""

import argparse
import json
import re
import subprocess
from collections.abc import Callable, Sequence


DEFAULT_TITLE = "Bundled edge264 is behind upstream"
TAG_SENTENCE = re.compile(r"Upstream's newest tag is at `([0-9a-f]{40})`\.")

Gh = Callable[[Sequence[str], str | None], str]


def tag_sentence(pending: str) -> str:
    return f"Upstream's newest tag is at `{pending}`."


def issue_body(pending: str) -> str:
    return (
        f"{tag_sentence(pending)} Run the **Update edge264 Pin** workflow (revision `latest`) "
        "to open the update pull request."
    )


def run_gh(arguments: Sequence[str], stdin: str | None = None) -> str:
    return subprocess.run(["gh", *arguments], input=stdin or "", text=True, check=True, stdout=subprocess.PIPE).stdout


def find_open_issue(title: str, gh: Gh) -> dict[str, object] | None:
    listing = gh(
        ["issue", "list", "--state", "open", "--search", f'in:title "{title}"', "--json", "number,title,body"],
        None,
    )
    for issue in json.loads(listing or "[]"):
        if issue.get("title") == title:
            return dict(issue)
    return None


def watch(pending: str, title: str, gh: Gh) -> str:
    """Bring the tracking issue in line with ``pending`` and describe what was done."""
    if not pending:
        return "The edge264 pin is current."
    issue = find_open_issue(title, gh)
    if issue is None:
        gh(["issue", "create", "--title", title, "--body-file", "-"], issue_body(pending))
        return f"Opened an issue for upstream commit {pending}."
    number = str(issue["number"])
    body = str(issue.get("body") or "")
    named = TAG_SENTENCE.search(body)
    if named and named.group(1) == pending:
        return f"Issue #{number} already names upstream commit {pending}."
    if named:
        updated = body[: named.start()] + tag_sentence(pending) + body[named.end() :]
    else:
        updated = f"{issue_body(pending)}\n\n{body}" if body else issue_body(pending)
    gh(["issue", "edit", number, "--body-file", "-"], updated)
    previous = f" (was `{named.group(1)}`)" if named else ""
    gh(
        ["issue", "comment", number, "--body-file", "-"],
        f"Upstream's newest tag moved to `{pending}`{previous}. The issue body now names the new commit.",
    )
    return f"Updated issue #{number} to upstream commit {pending}."


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--pending", required=True, help="Commit --pending-update printed; empty when current.")
    parser.add_argument("--title", default=DEFAULT_TITLE, help="Title of the tracking issue.")
    args = parser.parse_args(argv)
    print(watch(args.pending.strip(), args.title, run_gh))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
