"""Host-side GitHub REST client (PRD 8). Agents use `gh` inside containers instead."""

from codeit.github_client.client import GitHubClient, GitHubError, GitHubNotFound, repo_slug
from codeit.github_client.prs import PRComment, PullRequest

__all__ = ["GitHubClient", "GitHubError", "GitHubNotFound", "PRComment", "PullRequest", "repo_slug"]
