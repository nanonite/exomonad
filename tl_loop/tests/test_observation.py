from tl_loop.loop.observation import WatcherObservation


def test_projection_preserves_explicit_false_and_empty_error() -> None:
    observed = WatcherObservation.from_response(
        {
            "publication_ownership_verified": False,
            "publication_ownership_error": "",
        }
    )

    assert observed.publication_ownership_verified is False
    assert observed.publication_ownership_error == ""
    assert observed.ownership_status() == (False, "publication ownership is unverified")


def test_projection_distinguishes_omitted_ownership_fields() -> None:
    observed = WatcherObservation.from_response({"merged": False})

    assert observed.merged is False
    assert observed.publication_ownership_verified is None
    assert observed.publication_ownership_error is None
    assert observed.ownership_status() == (
        False,
        "watcher_pr_state omitted publication_ownership_verified",
    )


def test_projection_keeps_verified_publication_identity() -> None:
    observed = WatcherObservation.from_response(
        {
            "found": True,
            "publication_ownership_verified": True,
            "publication_ownership_error": "",
            "publication": {
                "invocation_id": "inv-1",
                "slice_id": "slice-a",
                "author_agent": "agent-a",
            },
        }
    )

    assert observed.ownership_status() == (True, None)
    assert observed.publication is not None
    assert observed.publication.invocation_id == "inv-1"
    assert observed.publication.slice_id == "slice-a"


def test_projection_preserves_exact_head_review_evidence() -> None:
    observed = WatcherObservation.from_response(
        {
            "review_id": 17,
            "review_verdict": " APPROVED ",
            "review_head_sha": "head-a",
            "review_body": "Looks good",
            "reviewer_agent_id": "review-pr-7-codex",
            "reviewer_identity_error": "",
        }
    )

    assert observed.review_id == 17
    assert observed.review_verdict == "approved"
    assert observed.review_head_sha == "head-a"
    assert observed.review_body == "Looks good"
    assert observed.reviewer_agent_id == "review-pr-7-codex"
    assert observed.to_payload()["review_id"] == 17


def test_projection_drops_unknown_or_non_positive_review_evidence() -> None:
    observed = WatcherObservation.from_response(
        {"review_id": 0, "review_verdict": "dismissed", "review_head_sha": ""}
    )

    assert observed.review_id is None
    assert observed.review_verdict is None
    assert observed.review_head_sha == ""
