from __future__ import annotations

from pathlib import Path
import contextlib
import io
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from resolve_thread import main, resolve_exact_thread, select_resolution_context  # noqa: E402


class ResolveThreadScopeTests(unittest.TestCase):
    def test_explicit_ids_without_a_frozen_checkpoint_are_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "active frozen batch"):
            select_resolution_context(
                checkpoint=None,
                explicit_expected_ids=["T1"],
                configured_reviewer_logins=["chatgpt-codex-connector"],
                thread_id="T1",
            )

    def test_explicit_expected_set_cannot_override_active_batch(self) -> None:
        checkpoint = {
            "approval_epoch": {"head_oid": "HEAD1"},
            "active_batch": {
                "frozen_head_oid": "HEAD1",
                "targeted_thread_ids": ["T1"],
                "reviewer_logins": ["custom-codex"],
                "thread_outcomes": {},
            },
        }
        with self.assertRaisesRegex(RuntimeError, "cannot override"):
            select_resolution_context(
                checkpoint=checkpoint,
                explicit_expected_ids=["T1"],
                configured_reviewer_logins=[],
                thread_id="T1",
            )

    def test_active_batch_restores_custom_reviewer_identity(self) -> None:
        checkpoint = {
            "approval_epoch": {"head_oid": "HEAD1"},
            "active_batch": {
                "frozen_head_oid": "HEAD1",
                "targeted_thread_ids": ["T1"],
                "reviewer_logins": ["custom-codex"],
                "thread_outcomes": {"T1": {"classification": "no-fix"}},
            },
        }
        expected, reviewers, head = select_resolution_context(
            checkpoint=checkpoint,
            explicit_expected_ids=[],
            configured_reviewer_logins=[],
            thread_id="T1",
        )
        self.assertEqual(expected, ["T1"])
        self.assertEqual(reviewers, ["custom-codex"])
        self.assertEqual(head, "HEAD1")

    def test_thread_from_another_pr_is_rejected_before_mutation(self) -> None:
        calls: list[str] = []

        def fake_graphql(query: str, variables: dict[str, object]) -> dict:
            calls.append(query)
            if "mutation" in query:
                self.fail("Mutation must not run after PR ownership mismatch")
            return {
                "data": {
                    "repository": {
                        "nameWithOwner": "Owner/Repo",
                        "pullRequest": {
                            "number": 17,
                            "headRefOid": "HEAD1",
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [{"id": "THREAD_IN_PR_17", "isResolved": False}],
                            },
                        },
                    }
                }
            }

        with self.assertRaisesRegex(RuntimeError, "outside the requested PR"):
            resolve_exact_thread(
                repository="Owner/Repo",
                pr_number=17,
                thread_id="THREAD_FROM_PR_18",
                expected_thread_ids=["THREAD_FROM_PR_18"],
                graphql_call=fake_graphql,
            )
        self.assertEqual(len(calls), 1)

    def test_exact_returned_state_is_verified(self) -> None:
        def fake_graphql(query: str, variables: dict[str, object]) -> dict:
            if "mutation" not in query:
                return {
                    "data": {
                        "repository": {
                            "nameWithOwner": "Owner/Repo",
                            "pullRequest": {
                                "number": 17,
                                "headRefOid": "HEAD1",
                                "reviewThreads": {
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": [
                                        {
                                            "id": "T1",
                                            "isResolved": False,
                                            "comments": {
                                                "nodes": [
                                                    {
                                                        "author": {
                                                            "login": "chatgpt-codex-connector"
                                                        }
                                                    }
                                                ]
                                            },
                                        }
                                    ],
                                },
                            },
                        }
                    }
                }
            return {
                "data": {
                    "resolveReviewThread": {
                        "thread": {"id": "WRONG", "isResolved": True}
                    }
                }
            }

        with self.assertRaisesRegex(RuntimeError, "did not confirm"):
            resolve_exact_thread(
                repository="Owner/Repo",
                pr_number=17,
                thread_id="T1",
                expected_thread_ids=["T1"],
                graphql_call=fake_graphql,
            )

    def test_authority_is_rechecked_immediately_before_graphql_mutation(self) -> None:
        mutation_called = False
        boundary_checks = 0

        def fake_graphql(query: str, variables: dict[str, object]) -> dict:
            nonlocal mutation_called
            if "mutation" in query:
                mutation_called = True
                self.fail("Mutation must not run after authority is lost")
            return {
                "data": {
                    "repository": {
                        "nameWithOwner": "Owner/Repo",
                        "pullRequest": {
                            "number": 17,
                            "headRefOid": "HEAD1",
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "T1",
                                        "isResolved": False,
                                        "comments": {
                                            "nodes": [
                                                {
                                                    "author": {
                                                        "login": "chatgpt-codex-connector"
                                                    }
                                                }
                                            ]
                                        },
                                    }
                                ],
                            },
                        },
                    }
                }
            }

        def reject_expired_lease() -> None:
            nonlocal boundary_checks
            boundary_checks += 1
            raise RuntimeError("Lease has expired")

        with self.assertRaisesRegex(RuntimeError, "Lease has expired"):
            resolve_exact_thread(
                repository="Owner/Repo",
                pr_number=17,
                thread_id="T1",
                expected_thread_ids=["T1"],
                before_mutation=reject_expired_lease,
                graphql_call=fake_graphql,
            )
        self.assertEqual(boundary_checks, 1)
        self.assertFalse(mutation_called)

    def test_same_pr_human_thread_is_rejected_before_mutation(self) -> None:
        calls: list[str] = []

        def fake_graphql(query: str, variables: dict[str, object]) -> dict:
            calls.append(query)
            if "mutation" in query:
                self.fail("Mutation must not run for a human root author")
            return {
                "data": {
                    "repository": {
                        "nameWithOwner": "Owner/Repo",
                        "pullRequest": {
                            "number": 17,
                            "headRefOid": "HEAD1",
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "T_HUMAN",
                                        "isResolved": False,
                                        "comments": {
                                            "nodes": [
                                                {"author": {"login": "human-reviewer"}}
                                            ]
                                        },
                                    }
                                ],
                            },
                        },
                    }
                }
            }

        with self.assertRaisesRegex(RuntimeError, "non-target root authors"):
            resolve_exact_thread(
                repository="Owner/Repo",
                pr_number=17,
                thread_id="T_HUMAN",
                expected_thread_ids=["T_HUMAN"],
                graphql_call=fake_graphql,
            )
        self.assertEqual(len(calls), 1)

    def test_frozen_head_mismatch_is_rejected_before_mutation(self) -> None:
        calls: list[str] = []

        def fake_graphql(query: str, variables: dict[str, object]) -> dict:
            calls.append(query)
            if "mutation" in query:
                self.fail("Mutation must not run after a frozen-head mismatch")
            return {
                "data": {
                    "repository": {
                        "nameWithOwner": "Owner/Repo",
                        "pullRequest": {
                            "number": 17,
                            "headRefOid": "NEW_HEAD",
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "T1",
                                        "isResolved": False,
                                        "comments": {
                                            "nodes": [
                                                {
                                                    "author": {
                                                        "login": "chatgpt-codex-connector"
                                                    }
                                                }
                                            ]
                                        },
                                    }
                                ],
                            },
                        },
                    }
                }
            }

        with self.assertRaisesRegex(RuntimeError, "frozen batch head"):
            resolve_exact_thread(
                repository="Owner/Repo",
                pr_number=17,
                thread_id="T1",
                expected_thread_ids=["T1"],
                expected_head_oid="OLD_HEAD",
                graphql_call=fake_graphql,
            )
        self.assertEqual(len(calls), 1)

    def test_already_resolved_path_rechecks_authority_before_checkpoint_write(self) -> None:
        checkpoint = {
            "approval_epoch": {"head_oid": "HEAD1"},
            "active_batch": {
                "targeted_thread_ids": ["T1"],
                "frozen_head_oid": "HEAD1",
                "reviewer_logins": ["chatgpt-codex-connector"],
                "thread_outcomes": {"T1": {"classification": "no-fix"}},
                "resolved_thread_ids": [],
            }
        }
        contract = {
            "repository": "owner/repo",
            "pull_request_number": 17,
            "paths": {"checkpoint": "C:/checkpoint.json"},
        }
        authority_checks: list[str] = []

        def authority(*args, **kwargs) -> None:
            authority_checks.append(kwargs["required_scope"])
            if len(authority_checks) == 2:
                raise RuntimeError("lease lost before checkpoint write")

        with patch.object(sys, "argv", [
            "resolve_thread.py",
            "T1",
            "--repo",
            "owner/repo",
            "--pr",
            "17",
            "--state-file",
            "C:/checkpoint.json",
            "--run-contract",
            "C:/contract.json",
            "--lease-owner-token",
            "owner-a",
        ]), patch("resolve_thread.load_checkpoint", return_value=checkpoint), patch(
            "resolve_thread.validate_checkpoint"
        ), patch(
            "resolve_thread.load_mutation_run_contract", return_value=contract
        ), patch(
            "resolve_thread.assert_mutation_authority", side_effect=authority
        ), patch(
            "resolve_thread.resolve_exact_thread",
            return_value={"id": "T1", "isResolved": True, "alreadyResolved": True},
        ), patch("resolve_thread.save_checkpoint") as save:
            with self.assertRaisesRegex(RuntimeError, "lease lost"):
                with contextlib.redirect_stdout(io.StringIO()):
                    main()

        self.assertEqual(authority_checks, ["resolve_threads", "resolve_threads"])
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
