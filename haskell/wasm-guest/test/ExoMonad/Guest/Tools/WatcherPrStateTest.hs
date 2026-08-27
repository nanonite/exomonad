{-# LANGUAGE OverloadedStrings #-}

module ExoMonad.Guest.Tools.WatcherPrStateTest (watcherPrStateTests) where

import Data.Aeson (Value (..))
import Data.Aeson qualified as Aeson
import Data.Aeson.Key qualified as Key
import Data.Aeson.KeyMap qualified as KeyMap
import Data.Text.Lazy qualified as TL
import Effects.Agent qualified as PA
import ExoMonad.Guest.Tools.WatcherPrState (watcherPrStateResponseValue)
import Test.Tasty (TestTree, testGroup)
import Test.Tasty.HUnit (assertEqual, assertFailure, testCase)

watcherPrStateTests :: TestTree
watcherPrStateTests =
  testGroup
    "watcher_pr_state"
    [testCase "serializes durable review evidence at the WASM boundary" $ do
      let response =
            PA.WatcherPrStateResponse
              { PA.watcherPrStateResponseSuccess = True,
                PA.watcherPrStateResponseError = "",
                PA.watcherPrStateResponsePrNumber = 43,
                PA.watcherPrStateResponseFound = True,
                PA.watcherPrStateResponseReviewState = TL.pack "approved",
                PA.watcherPrStateResponseCiStatus = TL.pack "success",
                PA.watcherPrStateResponseHeadSha = TL.pack "head-a",
                PA.watcherPrStateResponseHeadBranch = TL.pack "main.slice-a",
                PA.watcherPrStateResponseBaseBranch = TL.pack "main",
                PA.watcherPrStateResponsePrState = TL.pack "open",
                PA.watcherPrStateResponseMerged = False,
                PA.watcherPrStateResponseReviewCount = 1,
                PA.watcherPrStateResponseBaseSha = TL.pack "base-a",
                PA.watcherPrStateResponsePatchDigest = TL.pack "patch-a",
                PA.watcherPrStateResponseMergeTreeSha = TL.pack "tree-a",
                PA.watcherPrStateResponseHeadReachable = True,
                PA.watcherPrStateResponseEvidenceError = "",
                PA.watcherPrStateResponsePublicationOwnershipVerified = True,
                PA.watcherPrStateResponsePublicationOwnershipError = "",
                PA.watcherPrStateResponsePublication = Nothing,
                PA.watcherPrStateResponseReviewId = 403,
                PA.watcherPrStateResponseReviewVerdict = TL.pack "approved",
                PA.watcherPrStateResponseReviewHeadSha = TL.pack "head-a",
                PA.watcherPrStateResponseReviewerAgentId = TL.pack "review-pr-43-codex",
                PA.watcherPrStateResponseReviewerIdentityError = "",
                PA.watcherPrStateResponseReviewBody = TL.pack "Looks good"
              }
      case watcherPrStateResponseValue response of
        Object fields -> do
          assertEqual "review_id" (Just (Aeson.toJSON (403 :: Integer))) (KeyMap.lookup (Key.fromString "review_id") fields)
          assertEqual "review_verdict" (Just (Aeson.toJSON ("approved" :: String))) (KeyMap.lookup (Key.fromString "review_verdict") fields)
          assertEqual "review_head_sha" (Just (Aeson.toJSON ("head-a" :: String))) (KeyMap.lookup (Key.fromString "review_head_sha") fields)
          assertEqual "reviewer_agent_id" (Just (Aeson.toJSON ("review-pr-43-codex" :: String))) (KeyMap.lookup (Key.fromString "reviewer_agent_id") fields)
          assertEqual "reviewer_identity_error" (Just (Aeson.toJSON ("" :: String))) (KeyMap.lookup (Key.fromString "reviewer_identity_error") fields)
          assertEqual "review_body" (Just (Aeson.toJSON ("Looks good" :: String))) (KeyMap.lookup (Key.fromString "review_body") fields)
        _ -> assertFailure "watcher_pr_state projection must serialize as a JSON object"
    ]
