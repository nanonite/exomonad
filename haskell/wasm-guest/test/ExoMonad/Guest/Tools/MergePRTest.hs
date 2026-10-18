{-# LANGUAGE OverloadedStrings #-}

module ExoMonad.Guest.Tools.MergePRTest (mergePrTests) where

import Data.Aeson qualified as Aeson
import Data.Aeson.KeyMap qualified as KeyMap
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Agent qualified as Agent
import Effects.Github qualified as GH
import ExoMonad.Guest.Tools.MergePR
  ( MergePRArgs (..),
    MergePROutput (..),
    Readiness (..),
    mergeExpectedHeadSha,
    mergePRRender,
    parentApprovalWatcherMergeGate,
    watcherMergeGate,
  )
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
          NotReady reason ->
            assertBool
              ("unexpected merge rejection: " <> TL.unpack (TL.fromStrict reason))
              False,
      testCase "parent approval accepts aggregate without a reviewer verdict" $ do
        let watcher =
              (watcherResponse "" "")
                { Agent.watcherPrStateResponseReviewState = "pending",
                  Agent.watcherPrStateResponseReviewId = 0,
                  Agent.watcherPrStateResponseReviewVerdict = "",
                  Agent.watcherPrStateResponseReviewHeadSha = "",
                  Agent.watcherPrStateResponseReviewerAgentId = ""
                }
        case parentApprovalWatcherMergeGate 43 hostedResponse watcher of
          Ready -> pure ()
          NotReady reason ->
            assertBool
              ("unexpected parent-approved aggregate rejection: " <> TL.unpack (TL.fromStrict reason))
              False,
      testCase "parent approval still requires current successful CI" $ do
        let watcher =
              (watcherResponse "" "")
                { Agent.watcherPrStateResponseReviewState = "pending",
                  Agent.watcherPrStateResponseReviewId = 0,
                  Agent.watcherPrStateResponseReviewVerdict = "",
                  Agent.watcherPrStateResponseReviewerAgentId = "",
                  Agent.watcherPrStateResponseCiStatus = "failure"
                }
        case parentApprovalWatcherMergeGate 43 hostedResponse watcher of
          NotReady reason -> assertBool "reports CI failure" ("CI status" `TL.isInfixOf` TL.fromStrict reason)
          Ready -> assertBool "must block on failed CI" False,
      testCase "parent approval still requires exact hosted and watcher head" $ do
        let watcher = (watcherResponse "" "") {Agent.watcherPrStateResponseHeadSha = "other-head"}
        case parentApprovalWatcherMergeGate 43 hostedResponse watcher of
          NotReady reason -> assertBool "reports head drift" ("changed" `TL.isInfixOf` TL.fromStrict reason)
          Ready -> assertBool "must block on head drift" False,
      testCase "leaf gate still requires authenticated reviewer for the exact head" $ do
        let watcher =
              (watcherResponse "" "")
                { Agent.watcherPrStateResponseReviewState = "pending",
                  Agent.watcherPrStateResponseReviewId = 0,
                  Agent.watcherPrStateResponseReviewVerdict = "",
                  Agent.watcherPrStateResponseReviewerAgentId = ""
                }
        case watcherMergeGate 43 hostedResponse watcher of
          NotReady reason -> assertBool "reports missing review" ("review approval" `TL.isInfixOf` TL.fromStrict reason)
          Ready -> assertBool "leaf must not inherit parent approval" False,
      testCase "rejects unverified publication ownership" $ do
        let watcher =
              (watcherResponse "review-pr-43-codex" "")
                { Agent.watcherPrStateResponsePublicationOwnershipVerified = False
                }
        case watcherMergeGate 43 hostedResponse watcher of
          NotReady reason ->
            assertBool
              "reports publication ownership failure"
              (TL.isInfixOf "publication ownership" (TL.fromStrict reason))
          Ready -> assertBool "must reject unverified publication ownership" False,
      testCase "rejects publication ownership errors" $ do
        let watcher =
              (watcherResponse "review-pr-43-codex" "")
                { Agent.watcherPrStateResponsePublicationOwnershipError = "wrong invocation"
                }
        case watcherMergeGate 43 hostedResponse watcher of
          NotReady reason ->
            assertBool
              "reports publication ownership error"
              (TL.isInfixOf "wrong invocation" (TL.fromStrict reason))
          Ready -> assertBool "must reject publication ownership errors" False,
      testCase "rejects unreachable head evidence" $ do
        let watcher =
              (watcherResponse "review-pr-43-codex" "")
                { Agent.watcherPrStateResponseHeadReachable = False
                }
        case watcherMergeGate 43 hostedResponse watcher of
          NotReady reason ->
            assertBool
              "reports unreachable head"
              (TL.isInfixOf "unreachable" (TL.fromStrict reason))
          Ready -> assertBool "must reject unreachable head evidence" False,
      testCase "rejects canonical evidence errors" $ do
        let watcher =
              (watcherResponse "review-pr-43-codex" "")
                { Agent.watcherPrStateResponseEvidenceError = "merge tree mismatch"
                }
        case watcherMergeGate 43 hostedResponse watcher of
          NotReady reason ->
            assertBool
              "reports evidence error"
              (TL.isInfixOf "merge tree mismatch" (TL.fromStrict reason))
          Ready -> assertBool "must reject canonical evidence errors" False,
      testCase "always compare-binds the observed head" $ do
        let args =
              MergePRArgs
                { mprPrNumber = 43,
                  mprStrategy = Nothing,
                  mprWorkingDir = Nothing,
                  mprExpectedBaseSha = Nothing,
                  mprExpectedHeadSha = Just "caller-head",
                  mprExpectedPatchDigest = Nothing,
                  mprExpectedMergeTreeSha = Nothing,
                  mprChainlinkIssueId = Nothing,
                  mprAggregateApproval = Nothing
                }
        assertBool
          "observed head must override a caller-supplied head"
          (mergeExpectedHeadSha args "head-a" == "head-a"),
      testCase "remote merge result leaves local synchronization to the durable flow" $ do
        let output =
              Aeson.toJSON $
                mergePRRender
                  MergePROutput
                    { mpoSuccess = True,
                      mpoMessage = "PR #43 merged",
                      mpoGitFetched = False,
                      mpoBranchName = "main.slice-a"
                    }
        case output of
          Aeson.Object envelope ->
            case KeyMap.lookup "result" envelope of
              Just (Aeson.Object result) -> do
                assertBool "remote merge success remains authoritative" (KeyMap.lookup "success" result == Just (Aeson.Bool True))
                assertBool "merge tool does not claim a fetch" (KeyMap.lookup "git_fetched" result == Just (Aeson.Bool False))
                case KeyMap.lookup "next" result of
                  Just (Aeson.String next) ->
                    assertBool "next action names the durable sync owner" ("durable post-merge flow" `TL.isInfixOf` TL.fromStrict next)
                  _ -> assertBool "next action is present" False
              _ -> assertBool "tool result is an object" False
          _ -> assertBool "tool response is an object" False
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
      Agent.watcherPrStateResponsePrHeadTreeSha = "pr-tree-a",
      Agent.watcherPrStateResponseMergeCommitSha = "merge-commit-a",
      Agent.watcherPrStateResponseMergeCommitTreeSha = "merge-tree-a",
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
      Agent.watcherPrStateResponseReviewBody = "",
      Agent.watcherPrStateResponseReviewSubmittedAt = ""
    }
