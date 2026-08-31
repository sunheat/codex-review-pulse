from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetch_pr_state import fetch_stable_snapshot, graphql  # noqa: E402


def _connection_payload(name: str, server_time: str | None) -> dict:
    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    name: {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [],
                    }
                }
            }
        }
    }
    if server_time is not None:
        payload["_github_server_time"] = server_time
    return payload


def _meta_payload(server_time: str | None) -> dict:
    payload = {
        "data": {
            "repository": {
                "nameWithOwner": "Owner/Repo",
                "pullRequest": {
                    "number": 17,
                    "headRefOid": "HEAD1",
                    "headRepository": {"nameWithOwner": "Owner/Repo"},
                },
            }
        }
    }
    if server_time is not None:
        payload["_github_server_time"] = server_time
    return payload


class GithubServerTimeTests(unittest.TestCase):
    def test_graphql_extracts_authoritative_http_date(self) -> None:
        response = (
            "HTTP/2.0 200 OK\r\n"
            "Content-Type: application/json\r\n"
            "Date: Sun, 30 Aug 2026 16:04:54 GMT\r\n"
            "\r\n"
            '{"data":{"viewer":{"login":"sunheat"}}}\n'
        )
        with patch("fetch_pr_state.run", return_value=response) as command:
            payload = graphql("query { viewer { login } }", "Owner", "Repo", 17)

        self.assertEqual(
            payload["_github_server_time"], "2026-08-30T16:04:54+00:00"
        )
        self.assertIn("--include", command.call_args.args[0])

    def test_stable_snapshot_requires_server_time_when_requested(self) -> None:
        def without_server_time(
            query: str, owner: str, repo: str, number: int, cursor: str | None
        ) -> dict:
            if "nameWithOwner" in query:
                return _meta_payload(None)
            if "reviewThreads(" in query:
                return _connection_payload("reviewThreads", None)
            if "reactions(" in query:
                return _connection_payload("reactions", None)
            return _connection_payload("reviews", None)

        with self.assertRaisesRegex(RuntimeError, "authoritative server time"):
            fetch_stable_snapshot(
                "Owner", "Repo", 17,
                include_conversation=False,
                graphql_call=without_server_time,
            )

    def test_stable_snapshot_uses_the_latest_github_server_time(self) -> None:
        times = iter(
            [
                "2026-08-30T16:04:50+00:00",
                "2026-08-30T16:04:51+00:00",
                "2026-08-30T16:04:52+00:00",
                "2026-08-30T16:04:53+00:00",
                "2026-08-30T16:04:54+00:00",
                "2026-08-30T16:04:55+00:00",
            ]
        )

        def with_server_time(
            query: str, owner: str, repo: str, number: int, cursor: str | None
        ) -> dict:
            server_time = next(times)
            if "nameWithOwner" in query:
                return _meta_payload(server_time)
            if "reviewThreads(" in query:
                return _connection_payload("reviewThreads", server_time)
            if "reactions(" in query:
                return _connection_payload("reactions", server_time)
            return _connection_payload("reviews", server_time)

        snapshot = fetch_stable_snapshot(
            "Owner", "Repo", 17,
            include_conversation=False,
            graphql_call=with_server_time,
        )

        self.assertEqual(snapshot["server_time"], "2026-08-30T16:04:55+00:00")


if __name__ == "__main__":
    unittest.main()
