#!/usr/bin/env python3

"""
Generate README.md from TEMPLATE.md by filling in GitHub stats.
This replaces the third-party profile-readme-stats action with an internal script
that talks to the GitHub GraphQL API using the provided token.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional
from urllib import error, request

API_URL = "https://api.github.com/graphql"
PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}")


class GitHubGraphQLClient:
    """Minimal GraphQL client for GitHub's API."""

    def __init__(self, token: str) -> None:
        self.token = token

    def execute(self, query: str, variables: Dict[str, object]) -> Dict[str, object]:
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        req = request.Request(
            API_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "profile-readme-generator",
            },
        )
        try:
            with request.urlopen(req) as resp:  # type: ignore[arg-type]
                data = json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:  # pragma: no cover - network failure handling
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"GraphQL request failed ({exc.code}): {detail}") from exc

        if "errors" in data:
            raise RuntimeError(f"GraphQL API returned errors: {data['errors']}")

        return data["data"]


def isoformat(dt: datetime) -> str:
    """Return an ISO 8601 string in UTC without microseconds."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fetch_user_created_at(client: GitHubGraphQLClient, login: str) -> datetime:
    query = """
    query($login: String!) {
      user(login: $login) {
        createdAt
      }
    }
    """
    data = client.execute(query, {"login": login})
    user = data.get("user")
    if not user:
        raise RuntimeError(f"User '{login}' not found when fetching creation date.")
    created_at = user["createdAt"]
    return datetime.fromisoformat(created_at.replace("Z", "+00:00"))


def fetch_commit_total(client: GitHubGraphQLClient, login: str) -> int:
    """Sum commit contributions across the account lifetime."""
    created_at = fetch_user_created_at(client, login)
    now = datetime.now(timezone.utc)
    total_commits = 0

    contributions_query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          totalCommitContributions
        }
      }
    }
    """

    period_start = created_at
    while period_start < now:
        period_end = datetime(period_start.year + 1, 1, 1, tzinfo=timezone.utc)
        if period_end > now:
            period_end = now

        variables = {
            "login": login,
            "from": isoformat(period_start),
            "to": isoformat(period_end),
        }
        data = client.execute(contributions_query, variables)
        collection = data["user"]["contributionsCollection"]
        total_commits += collection["totalCommitContributions"]

        period_start = period_end

    return total_commits


def fetch_total_stars(client: GitHubGraphQLClient, login: str) -> int:
    """Sum the stars across all non-fork repositories owned by the user."""
    stars_query = """
    query($login: String!, $cursor: String) {
      user(login: $login) {
        repositories(
          first: 100,
          after: $cursor,
          ownerAffiliations: OWNER,
          isFork: false,
          orderBy: {field: UPDATED_AT, direction: DESC}
        ) {
          nodes {
            stargazerCount
          }
          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
    }
    """
    total_stars = 0
    cursor: Optional[str] = None

    while True:
        data = client.execute(stars_query, {"login": login, "cursor": cursor})
        repos = data["user"]["repositories"]
        for repo in repos["nodes"]:
            total_stars += repo["stargazerCount"]
        if not repos["pageInfo"]["hasNextPage"]:
            break
        cursor = repos["pageInfo"]["endCursor"]

    return total_stars


def render_template(template_text: str, replacements: Dict[str, object]) -> str:
    """Replace {{ PLACEHOLDER }} tokens with computed stats."""

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        if key in replacements:
            return str(replacements[key])
        return match.group(0)

    return PLACEHOLDER_PATTERN.sub(substitute, template_text)


def main() -> None:
    parser = argparse.ArgumentParser(description="Update README using TEMPLATE and GitHub stats.")
    parser.add_argument("--template", default="TEMPLATE.md", help="Path to TEMPLATE.md")
    parser.add_argument("--readme", default="README.md", help="Path to README.md")
    parser.add_argument(
        "--login",
        default=os.environ.get("GITHUB_REPOSITORY_OWNER"),
        help="GitHub username to fetch stats for (defaults to repository owner).",
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("TOKEN")
    if not token:
        raise SystemExit("Missing GITHUB_TOKEN environment variable for GitHub API access.")
    if not args.login:
        raise SystemExit("GitHub login is not specified. Pass --login or set GITHUB_REPOSITORY_OWNER.")

    client = GitHubGraphQLClient(token)
    print(f"Fetching stats for {args.login} …")
    stars = fetch_total_stars(client, args.login)
    commits = fetch_commit_total(client, args.login)
    print(f"Total stars: {stars}")
    print(f"Total commits: {commits}")

    template_path = Path(args.template)
    readme_path = Path(args.readme)
    template_text = template_path.read_text(encoding="utf-8")
    rendered = render_template(template_text, {"STARS": stars, "COMMITS": commits})
    readme_path.write_text(rendered, encoding="utf-8")
    print(f"Updated {readme_path} from {template_path} with latest stats.")


if __name__ == "__main__":
    main()
