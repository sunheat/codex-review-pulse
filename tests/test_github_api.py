from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))
FIXTURES = ROOT / "tests" / "fixtures"

import github_api  # noqa: E402
import storage  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if process.returncode != 0:
        raise RuntimeError(f"git {args} failed: {process.stderr.strip()}")
    return process.stdout.strip()


class OwnedRepository:
    def __init__(self, root: Path) -> None:
        self.path = root / "repo"
        self.path.mkdir()
        git(self.path, "init", "-b", "main")
        git(self.path, "config", "user.email", "t@e.test")
        git(self.path, "config", "user.name", "T")
        (self.path / "f").write_text("x", encoding="utf-8")
        git(self.path, "add", "f")
        git(self.path, "commit", "-m", "init")
        acquired = storage.acquire_lock(
            "owner/repo", 7,
            campaign_id="crp-20260914T120000Z-abc123",
            acquired_at="2026-09-14T12:00:00Z",
            repository_path=self.path,
        )
        self.token = acquired["owner_token"]


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


def thread_node(thread_id: str, login: str = CODEX, resolved: bool = False) -> dict:
    return {
        "id": thread_id,
        "isResolved": resolved,
        "comments": {"nodes": [{"id": f"{thread_id}-c1", "author": {"login": login}}]},
    }


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.owned = OwnedRepository(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _call(self, fake, thread_id="T1", batch=("T1",)):
        return github_api.verify_and_resolve_thread(
            repository="owner/repo",
            number=7,
            thread_id=thread_id,
            batch_thread_ids=list(batch),
            reviewer_logins=[CODEX],
            owner_token=self.owned.token,
            repository_path=self.owned.path,
            graphql_call=fake,
        )

    def test_resolution_requires_ownership(self) -> None:
        fake = FakeGraphQL()
        fake.threads = [thread_node("T1")]
        with self.assertRaisesRegex(RuntimeError, "owner|lock|token"):
            github_api.verify_and_resolve_thread(
                repository="owner/repo",
                number=7,
                thread_id="T1",
                batch_thread_ids=["T1"],
                reviewer_logins=[CODEX],
                owner_token="wrong-token",
                repository_path=self.owned.path,
                graphql_call=fake,
            )

    def test_resolves_applicable_batch_thread(self) -> None:
        fake = FakeGraphQL()
        fake.threads = [thread_node("T1"), thread_node("T2", login="human")]
        result = self._call(fake, batch=("T1",))
        self.assertTrue(result["isResolved"])
        self.assertFalse(result["already_resolved"])
        self.assertEqual(fake.mutations[-1], ("resolve", {"threadId": "T1"}))

    def test_already_resolved_is_confirmed_without_second_mutation(self) -> None:
        fake = FakeGraphQL()
        fake.threads = [thread_node("T1", resolved=True)]
        result = self._call(fake)
        self.assertTrue(result["already_resolved"])
        self.assertEqual(fake.mutations, [])

    def test_non_codex_root_author_refuses(self) -> None:
        fake = FakeGraphQL()
        fake.threads = [thread_node("T1", login="human")]
        with self.assertRaisesRegex(RuntimeError, "not an applicable Codex identity"):
            self._call(fake)

    def test_thread_outside_batch_refuses(self) -> None:
        fake = FakeGraphQL()
        fake.threads = [thread_node("T1"), thread_node("T2")]
        with self.assertRaisesRegex(
            RuntimeError, "not part of this in-memory remediation batch"
        ):
            self._call(fake, thread_id="T2", batch=("T1",))

    def test_batch_thread_missing_from_pr_refuses(self) -> None:
        fake = FakeGraphQL()
        fake.threads = [thread_node("T1")]
        with self.assertRaisesRegex(RuntimeError, "not all present"):
            self._call(fake, batch=("T1", "T9"))


class IssueTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.owned = OwnedRepository(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _issue(self, **kwargs):
        return github_api.ensure_deferred_issue(
            repository="owner/repo",
            number=7,
            owner_token=self.owned.token,
            repository_path=self.owned.path,
            **kwargs,
        )

    def test_reuses_issue_with_exact_marker_in_body(self) -> None:
        marker = github_api.deferred_issue_marker("owner/repo", 7, "T1")
        created: list = []
        result = self._issue(
            thread_id="T1",
            title="Fix later",
            body="details",
            searcher=lambda repo, query: [
                {"number": 42, "url": "https://x/42", "title": "Fix later",
                 "body": f"text\n{marker}"},
            ],
            creator=lambda *a: created.append(a) or "https://x/created",
        )
        self.assertFalse(result["created"])
        self.assertEqual(result["number"], 42)
        self.assertEqual(created, [])

    def test_title_match_without_marker_creates_new_issue(self) -> None:
        bodies: list[str] = []
        result = self._issue(
            thread_id="T1",
            title="Fix later",
            body="details",
            searcher=lambda repo, query: [
                {"number": 9, "url": "https://x/9", "title": "Fix later", "body": "no marker"},
            ],
            creator=lambda repo, title, body: (bodies.append(body) or "https://x/10"),
        )
        self.assertTrue(result["created"])
        marker = github_api.deferred_issue_marker("owner/repo", 7, "T1")
        self.assertIn(marker, bodies[0])

    def test_issue_creation_requires_ownership(self) -> None:
        with self.assertRaises(RuntimeError):
            github_api.ensure_deferred_issue(
                repository="owner/repo",
                number=7,
                thread_id="T1",
                title="Fix later",
                body="details",
                owner_token="wrong",
                repository_path=self.owned.path,
                searcher=lambda repo, query: [],
            )


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
