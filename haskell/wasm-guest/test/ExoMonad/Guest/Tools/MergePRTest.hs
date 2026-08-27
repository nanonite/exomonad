{-# LANGUAGE OverloadedStrings #-}

module ExoMonad.Guest.Tools.MergePRTest (mergePrTests) where

import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Agent qualified as Agent
import Effects.Github qualified as GH
import ExoMonad.Guest.Tools.MergePR (Readiness (..), watcherMergeGate)
import Proto3.Suite.Types qualified as Protobuf
import Test.Tasty (TestTree, testGroup)
import Test.Tasty.HUnit (assertBool, testCase)

mergePrTests :: TestTree
mergePrTests =
  testGroup
    "merge_pr"
    [ testCase "rejects approved review with unauthenticated reviewer identity" $ do
        let watcher = watcherResponse "" "owner-authored"
        case watcherMergeGate 43 hostedResponse watcher of
          NotReady reason -> assertBool "reports reviewer identity failure" ("owner-authored" `TL.isInfixOf` TL.fromStrict reason)
          Ready -> assertBool "must reject missing reviewer identity evidence" False,
      testCase "accepts complete authenticated exact-head review evidence" $ do
        let watcher = watcherResponse "review-pr-43-codex" ""
        case watcherMergeGate 43 hostedResponse watcher of
          Ready -> pure ()
          NotReady reason -> assertBool ("unexpected merge rejection: " <> TL.unpack (TL.fromStrict reason)) False
    ]

hostedResponse :: GH.GetPullRequestResponse
hostedResponse =
  GH.GetPullRequestResponse
    { GH.getPullRequestResponsePullRequest =
        Just
          GH.PullRequest
            { GH.pullRequestNumber = 43,
              GH.pullRequestTitle = "",
              GH.pullRequestBody = "",
              GH.pullRequestState = Protobuf.Enumerated (Right GH.IssueStateISSUE_STATE_OPEN),
              GH.pullRequestAuthor = Nothing,
              GH.pullRequestHeadRef = "main.slice-a",
              GH.pullRequestBaseRef = "main",
              GH.pullRequestMerged = False,
              GH.pullRequestDraft = False,
              GH.pullRequestLabels = V.empty,
              GH.pullRequestCreatedAt = 0,
              GH.pullRequestUpdatedAt = 0,
              GH.pullRequestHeadSha = "head-a"
            },
      GH.getPullRequestResponseReviews = V.empty
    }

watcherResponse :: TL.Text -> TL.Text -> Agent.WatcherPrStateResponse
watcherResponse reviewerAgentId identityError =
  Agent.WatcherPrStateResponse
    { Agent.watcherPrStateResponseSuccess = True,
      Agent.watcherPrStateResponseError = "",
      Agent.watcherPrStateResponsePrNumber = 43,
      Agent.watcherPrStateResponseFound = True,
      Agent.watcherPrStateResponseReviewState = "approved",
      Agent.watcherPrStateResponseCiStatus = "success",
      Agent.watcherPrStateResponseHeadSha = "head-a",
      Agent.watcherPrStateResponseHeadBranch = "main.slice-a",
      Agent.watcherPrStateResponseBaseBranch = "main",
      Agent.watcherPrStateResponsePrState = "open",
      Agent.watcherPrStateResponseMerged = False,
      Agent.watcherPrStateResponseReviewCount = 1,
      Agent.watcherPrStateResponseBaseSha = "base-a",
      Agent.watcherPrStateResponsePatchDigest = "patch-a",
      Agent.watcherPrStateResponseMergeTreeSha = "tree-a",
      Agent.watcherPrStateResponseHeadReachable = True,
      Agent.watcherPrStateResponseEvidenceError = "",
      Agent.watcherPrStateResponsePublicationOwnershipVerified = True,
      Agent.watcherPrStateResponsePublicationOwnershipError = "",
      Agent.watcherPrStateResponsePublication = Nothing,
      Agent.watcherPrStateResponseReviewId = 403,
      Agent.watcherPrStateResponseReviewVerdict = "approved",
      Agent.watcherPrStateResponseReviewHeadSha = "head-a",
      Agent.watcherPrStateResponseReviewerAgentId = reviewerAgentId,
      Agent.watcherPrStateResponseReviewerIdentityError = identityError,
      Agent.watcherPrStateResponseReviewBody = ""
    }
