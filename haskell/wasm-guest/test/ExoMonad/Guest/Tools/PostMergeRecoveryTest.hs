{-# LANGUAGE OverloadedStrings #-}

module ExoMonad.Guest.Tools.PostMergeRecoveryTest (postMergeRecoveryTests) where

import Control.Monad.Freer (run)
import Control.Monad.Freer.Coroutine (Status (Continue, Done), runC)
import Data.Aeson (Value, decode, encode, object, toJSON, (.=))
import Data.ByteString qualified as BS
import Data.ByteString.Lazy.Char8 qualified as L8
import Data.ByteString.Lazy qualified as BL
import Data.Int (Int32)
import Data.Text (Text)
import Data.Word (Word8)
import Effects.Envelope qualified as Envelope
import Effects.Process qualified as Proc
import ExoMonad.Guest.Tool.Suspend.Types (EffectRequest (..))
import ExoMonad.Guest.Tools.PostMergeRecovery
  ( PostMergeChangelogArgs (..),
    PostMergeParentSyncArgs (..),
    PostMergePushArgs (..),
    interpretGitResult,
    postMergeChangelogCore,
    postMergeChangelogCommitArgs,
    postMergeChangelogStageArgs,
    postMergeChangelogSchema,
    postMergeParentSyncFetchArgs,
    postMergeParentSyncMergeArgs,
    postMergeParentSyncSchema,
    postMergePushGitArgs,
    postMergePushSchema
  )
import Proto3.Suite.Class (toLazyByteString)
import Test.Tasty (TestTree, testGroup)
import Test.Tasty.HUnit (assertBool, assertEqual, testCase)

postMergeRecoveryTests :: TestTree
postMergeRecoveryTests =
  testGroup
    "post-merge recovery boundaries"
    [ testCase "parent synchronization arguments and argv are stable" $ do
        let value =
              object
                [ "child_id" .= ("slice-a" :: Text),
                  "pr_number" .= (43 :: Int),
                  "repository" .= ("org/repo" :: Text),
                  "parent_branch" .= ("main" :: Text),
                  "merged_head_sha" .= ("merge-head" :: Text),
                  "expected_base_sha" .= ("base-head" :: Text),
                  "lane_epoch" .= (7 :: Int)
                ]
            expected =
              PostMergeParentSyncArgs
                "slice-a"
                43
                "org/repo"
                "main"
                "merge-head"
                "base-head"
                7
                Nothing
        assertEqual "parent sync JSON arguments" (Just expected) (decode (encode value))
        assertEqual
          "fetch argv"
          ["fetch", "--prune", "origin", "main"]
          (postMergeParentSyncFetchArgs "main")
        assertEqual
          "merge argv"
          ["merge", "--ff-only", "origin/main"]
          (postMergeParentSyncMergeArgs "main"),
      testCase "changelog arguments and argv are stable" $ do
        let value =
              object
                [ "child_id" .= ("slice-a" :: Text),
                  "issue_id" .= (104 :: Int),
                  "repository" .= ("org/repo" :: Text),
                  "parent_branch" .= ("main" :: Text),
                  "expected_base_sha" .= ("base-head" :: Text),
                  "generation" .= (2 :: Int),
                  "intent_id" .= ("changelog-intent" :: Text)
                ]
            expected =
              PostMergeChangelogArgs
                "slice-a"
                104
                "org/repo"
                "main"
                "base-head"
                2
                "changelog-intent"
                Nothing
        assertEqual "changelog JSON arguments" (Just expected) (decode (encode value))
        assertEqual
          "staging argv"
          ["add", "--", "CHANGELOG.md"]
          postMergeChangelogStageArgs
        assertEqual
          "commit argv"
          [ "commit",
            "--only",
            "CHANGELOG.md",
            "-m",
            "Update changelog for Chainlink issue #104"
          ]
          (postMergeChangelogCommitArgs 104),
      testCase "parent push arguments and compare-and-swap argv are stable" $ do
        let value =
              object
                [ "child_id" .= ("slice-a" :: Text),
                  "repository" .= ("org/repo" :: Text),
                  "parent_branch" .= ("main" :: Text),
                  "lane_epoch" .= (7 :: Int),
                  "push_intent_id" .= ("push-intent" :: Text),
                  "push_journal_id" .= ("push-journal" :: Text),
                  "expected_base_sha" .= ("base-head" :: Text),
                  "pushed_commit" .= ("changelog-head" :: Text)
                ]
            expected =
              PostMergePushArgs
                "slice-a"
                "org/repo"
                "main"
                7
                "push-intent"
                "push-journal"
                "base-head"
                "changelog-head"
                Nothing
        assertEqual "push JSON arguments" (Just expected) (decode (encode value))
        assertEqual
          "force-with-lease push argv"
          [ "push",
            "--porcelain",
            "--force-with-lease=refs/heads/main:base-head",
            "origin",
            "HEAD:refs/heads/main"
          ]
          (postMergePushGitArgs "main" "base-head"),
      testCase "missing required boundary arguments fail closed" $ do
        assertEqual
          "parent sync requires all identity fields"
          Nothing
          (decode "{\"child_id\":\"slice-a\"}" :: Maybe PostMergeParentSyncArgs)
        assertEqual
          "changelog requires all identity fields"
          Nothing
          (decode "{\"child_id\":\"slice-a\"}" :: Maybe PostMergeChangelogArgs)
        assertEqual
          "push requires all identity fields"
          Nothing
          (decode "{\"child_id\":\"slice-a\"}" :: Maybe PostMergePushArgs),
      testCase "non-zero Git exit is returned as a durable boundary error" $ do
        assertEqual
          "stderr is preserved in the failure"
          (Left "git command failed (128): rejected by remote")
          (interpretGitResult 128 "ignored stdout" "rejected by remote\n"),
      testCase "changelog core propagates a failed commit" $ do
        let args =
              PostMergeChangelogArgs
                "slice-a"
                104
                "org/repo"
                "main"
                "base-head"
                0
                "changelog-intent"
                Nothing
        assertEqual
          "a failed commit must not become a successful boundary"
          (Left "git command failed (1): changelog commit failed")
          (runChangelogWithFailedCommit args),
      testCase "schemas expose every boundary identity and CAS field" $ do
        let schemaText schema = L8.unpack (encode schema)
            parentSchema = schemaText postMergeParentSyncSchema
            changelogSchema = schemaText postMergeChangelogSchema
            pushSchema = schemaText postMergePushSchema
        mapM_
          (\field -> assertBool ("parent schema field: " <> field) (field `L8.isInfixOf` L8.pack parentSchema))
          ["child_id", "repository", "parent_branch", "merged_head_sha", "expected_base_sha", "lane_epoch"]
        mapM_
          (\field -> assertBool ("changelog schema field: " <> field) (field `L8.isInfixOf` L8.pack changelogSchema))
          ["child_id", "issue_id", "repository", "parent_branch", "expected_base_sha", "generation", "intent_id"]
        mapM_
          (\field -> assertBool ("push schema field: " <> field) (field `L8.isInfixOf` L8.pack pushSchema))
          ["child_id", "repository", "parent_branch", "lane_epoch", "push_intent_id", "push_journal_id", "expected_base_sha", "pushed_commit"]
    ]

runChangelogWithFailedCommit :: PostMergeChangelogArgs -> Either Text Value
runChangelogWithFailedCommit args =
  run $ runC (postMergeChangelogCore args) >>= handleResponses 0
  where
    handleResponses _ (Done result) = pure result
    handleResponses processCount (Continue request resume) =
      resume (responseFor processCount request)
        >>= handleResponses (if erType request == "process.run" then processCount + 1 else processCount)

responseFor :: Int -> EffectRequest -> Value
responseFor processCount request
  | erType request /= "process.run" = encodedEffectResponse BS.empty
  | otherwise =
      case processCount of
        0 -> processResponse 0 "main" ""
        1 -> processResponse 0 "base-head" ""
        2 -> processResponse 0 " M CHANGELOG.md" ""
        3 -> processResponse 0 "" ""
        4 -> processResponse 1 "" "changelog commit failed\n"
        _ -> processResponse 1 "" "unexpected process request"

processResponse :: Int32 -> Text -> Text -> Value
processResponse exitCode stdout stderr =
  encodedEffectResponse
    ( BL.toStrict
        (toLazyByteString (Proc.RunResponse exitCode stdout stderr))
    )

encodedEffectResponse :: BS.ByteString -> Value
encodedEffectResponse payload =
  toJSON
    ( BS.unpack
        ( BL.toStrict
            ( toLazyByteString
                (Envelope.EffectResponse (Just (Envelope.EffectResponseResultPayload payload)))
            )
        ) :: [Word8]
    )
