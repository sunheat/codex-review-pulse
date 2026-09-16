from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))
FIXTURES = ROOT / "tests" / "fixtures"

import github_api  # noqa: E402


SERVER_TIME = "2026-09-14T12:00:00Z"
CODEX = "chatgpt-codex-connector"


def timed(payload: dict) -> dict:
    payload["_github_server_time"] = SERVER_TIME
    return payload


def meta_payload(*, head: str = "H1", state: str = "OPEN") -> dict:
    return timed(
        {
            "data": {
                "repository": {
                    "nameWithOwner": "owner/repo",
                    "pullRequest": {
                        "id": "PR_1",
                        "number": 7,
                        "state": state,
                        "headRefName": "feature",
                        "headRefOid": head,
                        "headRepository": {"nameWithOwner": "owner/repo"},
                    },
                }
            }
        }
    )


def connection_payload(name: str, nodes: list) -> dict:
    return timed(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        name: {
                            "nodes": nodes,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }
    )


class FakeGraphQL:
    def __init__(self, head: str = "H1", reactions: dict | None = None):
        self.head = head
        self.contents: list[str] = []
        self.mutations: list[tuple[str, dict]] = []
        self.reactions = reactions or {}

    def __call__(self, query: str, variables: dict) -> dict:
        if "number headRefOid" in query:
            return timed(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "number": 7,
                                "headRefOid": "H1",
                                "reviewThreads": {
                                    "nodes": self.threads,
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                },
                            }
                        }
                    }
                }
            )
        if "reviewThreads(first" in query:
            return connection_payload("reviewThreads", self.threads)
        if "reactions(first" in query:
            self.contents.append(variables["content"])
            return connection_payload("reactions", self.reactions.get(variables["content"], []))
        if "reviews(first" in query:
            return connection_payload("reviews", self.reviews)
        if "comments(first" in query:
            return connection_payload("comments", self.comments)
        if "resolveReviewThread" in query:
            self.mutations.append(("resolve", variables))
            return {
                "data": {
                    "resolveReviewThread": {
                        "thread": {"id": variables["threadId"], "isResolved": True}
                    }
                }
            }
        return meta_payload(head=self.head)

    threads: list = []
    reviews: list = []
    comments: list = []


class SnapshotTests(unittest.TestCase):
    def test_snapshot_includes_both_thumbs_up_and_eyes(self) -> None:
        fake = FakeGraphQL(
            reactions={
                "THUMBS_UP": [
                    {"id": "up1", "content": "THUMBS_UP", "createdAt": T_PLUS(10),
                     "user": {"login": CODEX}},
                ],
                "EYES": [
                    {"id": "eye1", "content": "EYES", "createdAt": T_PLUS(5),
                     "user": {"login": CODEX}},
                ],
            }
        )
        result = github_api.fetch_snapshot(
            "owner/repo", 7, graphql_call=fake, viewer_call=lambda: "operator"
        )
        self.assertEqual(set(self_content(fake)), {"THUMBS_UP", "EYES"})
        contents = {item["content"] for item in result["reactions"]}
        self.assertEqual(contents, {"THUMBS_UP", "EYES"})
        self.assertTrue(result["complete"])
        self.assertEqual(result["server_time"], SERVER_TIME)

    def test_head_bracket_mismatch_raises_instead_of_normalizing(self) -> None:
        class MovingHead(FakeGraphQL):
            def __call__(self, query: str, variables: dict) -> dict:
                if "reviews(first" in query:
                    self.head = "H2"
                return super().__call__(query, variables)

        with self.assertRaisesRegex(RuntimeError, "head moved"):
            github_api.fetch_snapshot(
                "owner/repo", 7, graphql_call=MovingHead(), viewer_call=lambda: "op"
            )

    def test_normalize_fixture_identities(self) -> None:
        raw_threads = json.loads(
            (FIXTURES / "review_threads.json").read_text(encoding="utf-8")
        )
        normalized = github_api.normalize_snapshot(
            repository="owner/repo",
            pull_request={"number": 7, "state": "OPEN", "headRefOid": "H",
                          "headRefName": "b", "headRepository": {}},
            threads=raw_threads,
            reactions=[],
            reviews=[],
            comments=[],
            server_time=SERVER_TIME,
            viewer="op",
        )
        by_id = {t["id"]: t for t in normalized["threads"]}
        self.assertEqual(by_id["T_CODEX"]["root_login"], CODEX)
        self.assertEqual(by_id["T_CODEX_BOT"]["root_login"], CODEX)
        self.assertEqual(by_id["T_HUMAN"]["root_login"], "human-reviewer")
        self.assertIsNone(by_id["T_UNKNOWN"]["root_login"])
        self.assertTrue(by_id["T_RESOLVED"]["is_resolved"])
        self.assertFalse(by_id["T_CODEX"]["is_resolved"])
        # Frozen-evidence identity fields the Phase 3 boundaries derive.
        self.assertEqual(by_id["T_CODEX"]["root_comment_id"], "C1")
        self.assertEqual(by_id["T_CODEX"]["body"], "Fix the null check here.")
        self.assertEqual(by_id["T_CODEX"]["root_updated_at"], "2026-09-14T12:00:00Z")
        self.assertEqual(by_id["T_CODEX"]["path"], "src/core.py")

    def test_pagination_with_next_page_collects_all_nodes(self) -> None:
        pages = [
            connection_payload("reviewThreads", [{"id": "T1"}]),
            connection_payload("reviewThreads", [{"id": "T2"}]),
            connection_payload("reviewThreads", []),
        ]
        pages[0]["data"]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"] = {
            "hasNextPage": True,
            "endCursor": "cursor-1",
        }
        pages[1]["data"]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"] = {
            "hasNextPage": True,
            "endCursor": "cursor-2",
        }

        def call(query: str, variables: dict) -> dict:
            return pages.pop(0)

        nodes, _ = github_api.fetch_connection(
            github_api.THREADS_QUERY,
            "reviewThreads",
            "owner",
            "repo",
            7,
            graphql_call=call,
        )
        self.assertEqual([n["id"] for n in nodes], ["T1", "T2"])


def self_content(fake: FakeGraphQL) -> list[str]:
    return fake.contents


def T_PLUS(minutes: int) -> str:
    return f"2026-09-14T12:{minutes:02d}:00Z"


class RejectionClassificationTests(unittest.TestCase):
    def test_graphql_error_payload_is_a_server_authoritative_rejection(self) -> None:
        original = github_api.run_json
        github_api.run_json = lambda *a, **k: {"errors": [{"message": "bad"}]}
        try:
            with self.assertRaises(github_api.GithubRejectionError):
                github_api.graphql("mutation { noop }")
        finally:
            github_api.run_json = original

    def test_rejection_is_distinct_from_transport_failures(self) -> None:
        self.assertTrue(issubclass(github_api.GithubRejectionError, RuntimeError))
        # Transport-level failures stay plain RuntimeErrors so classification
        # can distinguish "GitHub rejected the mutation" from "the call may
        # still complete".
        self.assertNotIsInstance(RuntimeError("timeout"), github_api.GithubRejectionError)

    def test_resolve_thread_transport_confirms_resolution(self) -> None:
        original = github_api.run_json
        seen: list = []
        github_api.run_json = lambda *a, **k: seen.append(a) or {
            "data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}
        }
        try:
            result = github_api.resolve_thread("T1")
        finally:
            github_api.run_json = original
        self.assertEqual(result, {"id": "T1", "isResolved": True})
        self.assertTrue(seen)


class GraphQLArgumentTests(unittest.TestCase):
    def test_string_variables_use_raw_f_so_at_is_not_a_file_reference(self) -> None:
        arguments = github_api._graphql_arguments(
            {"subjectId": "PR_1", "body": "@codex review"}
        )
        self.assertEqual(arguments, ["-f", "subjectId=PR_1", "-f", "body=@codex review"])

    def test_integers_and_booleans_use_typed_f_with_json_booleans(self) -> None:
        arguments = github_api._graphql_arguments(
            {"number": 7, "flag": True, "off": False, "nothing": None}
        )
        self.assertEqual(
            arguments,
            ["-F", "number=7", "-F", "flag=true", "-F", "off=false"],
        )


class CommentNormalizationTests(unittest.TestCase):
    def test_body_normalization(self) -> None:
        self.assertEqual(
            github_api.normalize_comment_body("  @Codex   Review\n"),
            "@codex review",
        )
        self.assertEqual(github_api.normalize_comment_body(None), "")


if __name__ == "__main__":
    unittest.main()
