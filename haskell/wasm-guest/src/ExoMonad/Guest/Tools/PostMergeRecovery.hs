{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

-- | Effect-backed post-merge recovery boundaries.
--
-- Each boundary performs its own Git operation and returns evidence produced
-- by Git. The controller persists the returned receipt before it advances to
-- the next boundary, so an interrupted operation cannot be replaced by
-- synthetic completion evidence.
module ExoMonad.Guest.Tools.PostMergeRecovery
  ( PostMergeParentSync,
    PostMergeParentSyncArgs (..),
    postMergeParentSyncCore,
    postMergeParentSyncFetchArgs,
    postMergeParentSyncMergeArgs,
    postMergeParentSyncDescription,
    postMergeParentSyncSchema,
    PostMergeChangelog,
    PostMergeChangelogArgs (..),
    postMergeChangelogCore,
    postMergeChangelogStageArgs,
    postMergeChangelogCommitArgs,
    postMergeChangelogDescription,
    postMergeChangelogSchema,
    PostMergePush,
    PostMergePushArgs (..),
    postMergePushCore,
    postMergePushGitArgs,
    postMergePushDescription,
    postMergePushSchema,
    interpretGitResult
  )
where

import Control.Monad (void)
import Control.Monad.Freer (Eff, Member)
import Data.Aeson (FromJSON (..), Value, object, withObject, (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Map qualified as Map
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Data.Word (Word64)
import Effects.Log qualified as Log
import Effects.Process qualified as Proc
import ExoMonad.Effects.Log (LogInfo)
import ExoMonad.Effects.Process (ProcessRun)
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.Suspend.Types (SuspendYield)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect, suspendEffect_)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

gitTimeoutMs :: Word64
gitTimeoutMs = 120000

data PostMergeParentSync

data PostMergeParentSyncArgs = PostMergeParentSyncArgs
  { pmpChildId :: Text,
    pmpPrNumber :: Int,
    pmpRepository :: Text,
    pmpParentBranch :: Text,
    pmpMergedHeadSha :: Text,
    pmpExpectedBaseSha :: Text,
    pmpLaneEpoch :: Int,
    pmpWorkingDir :: Maybe Text
  }
  deriving (Show, Eq, Generic)

instance FromJSON PostMergeParentSyncArgs where
  parseJSON = withObject "PostMergeParentSyncArgs" $ \v ->
    PostMergeParentSyncArgs
      <$> v .: "child_id"
      <*> v .: "pr_number"
      <*> v .: "repository"
      <*> v .: "parent_branch"
      <*> v .: "merged_head_sha"
      <*> v .: "expected_base_sha"
      <*> v .: "lane_epoch"
      <*> v .:? "working_dir"

data PostMergeChangelog

data PostMergeChangelogArgs = PostMergeChangelogArgs
  { pmcChildId :: Text,
    pmcIssueId :: Int,
    pmcRepository :: Text,
    pmcParentBranch :: Text,
    pmcExpectedBaseSha :: Text,
    pmcGeneration :: Int,
    pmcIntentId :: Text,
    pmcWorkingDir :: Maybe Text
  }
  deriving (Show, Eq, Generic)

instance FromJSON PostMergeChangelogArgs where
  parseJSON = withObject "PostMergeChangelogArgs" $ \v ->
    PostMergeChangelogArgs
      <$> v .: "child_id"
      <*> v .: "issue_id"
      <*> v .: "repository"
      <*> v .: "parent_branch"
      <*> v .: "expected_base_sha"
      <*> v .: "generation"
      <*> v .: "intent_id"
      <*> v .:? "working_dir"

data PostMergePush

data PostMergePushArgs = PostMergePushArgs
  { pmpuChildId :: Text,
    pmpuRepository :: Text,
    pmpuParentBranch :: Text,
    pmpuLaneEpoch :: Int,
    pmpuPushIntentId :: Text,
    pmpuPushJournalId :: Text,
    pmpuExpectedBaseSha :: Text,
    pmpuPushedCommit :: Text,
    pmpuWorkingDir :: Maybe Text
  }
  deriving (Show, Eq, Generic)

instance FromJSON PostMergePushArgs where
  parseJSON = withObject "PostMergePushArgs" $ \v ->
    PostMergePushArgs
      <$> v .: "child_id"
      <*> v .: "repository"
      <*> v .: "parent_branch"
      <*> v .: "lane_epoch"
      <*> v .: "push_intent_id"
      <*> v .: "push_journal_id"
      <*> v .: "expected_base_sha"
      <*> v .: "pushed_commit"
      <*> v .:? "working_dir"

postMergeParentSyncDescription :: Text
postMergeParentSyncDescription =
  "Synchronize a merged child into its parent branch and return Git-verified remote-head and ancestry evidence."

postMergeParentSyncSchema :: Aeson.Object
postMergeParentSyncSchema =
  genericToolSchemaWith @PostMergeParentSyncArgs
    [ ("child_id", "Stable child slice identifier."),
      ("pr_number", "Merged Forgejo pull request number."),
      ("repository", "Authoritative repository identity."),
      ("parent_branch", "Direct parent integration branch."),
      ("merged_head_sha", "Exact merged child head SHA."),
      ("expected_base_sha", "Base SHA captured by the merge boundary."),
      ("lane_epoch", "Durable integration-lane epoch."),
      ("working_dir", "Optional relative repository working directory.")
    ]

postMergeChangelogDescription :: Text
postMergeChangelogDescription =
  "Commit the already prepared Chainlink changelog on the parent branch and return the resulting Git commit SHA."

postMergeChangelogSchema :: Aeson.Object
postMergeChangelogSchema =
  genericToolSchemaWith @PostMergeChangelogArgs
    [ ("child_id", "Stable child slice identifier."),
      ("issue_id", "Chainlink issue whose changelog entry was prepared."),
      ("repository", "Authoritative repository identity."),
      ("parent_branch", "Direct parent integration branch."),
      ("expected_base_sha", "Synchronized parent base SHA."),
      ("generation", "Changelog recovery generation."),
      ("intent_id", "Durable changelog effect intent."),
      ("working_dir", "Optional relative repository working directory.")
    ]

postMergePushDescription :: Text
postMergePushDescription =
  "Push parent-branch bookkeeping with force-with-lease compare-and-swap and return a verified push receipt."

postMergePushSchema :: Aeson.Object
postMergePushSchema =
  genericToolSchemaWith @PostMergePushArgs
    [ ("child_id", "Stable child slice identifier."),
      ("repository", "Authoritative repository identity."),
      ("parent_branch", "Direct parent integration branch."),
      ("lane_epoch", "Durable integration-lane epoch."),
      ("push_intent_id", "Durable parent-push effect intent."),
      ("push_journal_id", "Durable parent-push journal identity."),
      ("expected_base_sha", "Compare-and-swap expected remote base SHA."),
      ("pushed_commit", "Changelog commit to publish."),
      ("working_dir", "Optional relative repository working directory.")
    ]

instance MCPTool PostMergeParentSync where
  type ToolArgs PostMergeParentSync = PostMergeParentSyncArgs
  toolName = "post_merge_parent_sync"
  toolDescription = postMergeParentSyncDescription
  toolSchema = postMergeParentSyncSchema
  toolHandlerEff args = either errorResult successResult <$> postMergeParentSyncCore args

instance MCPTool PostMergeChangelog where
  type ToolArgs PostMergeChangelog = PostMergeChangelogArgs
  toolName = "post_merge_changelog"
  toolDescription = postMergeChangelogDescription
  toolSchema = postMergeChangelogSchema
  toolHandlerEff args = either errorResult successResult <$> postMergeChangelogCore args

instance MCPTool PostMergePush where
  type ToolArgs PostMergePush = PostMergePushArgs
  toolName = "post_merge_push"
  toolDescription = postMergePushDescription
  toolSchema = postMergePushSchema
  toolHandlerEff args = either errorResult successResult <$> postMergePushCore args

postMergeParentSyncCore :: PostMergeParentSyncArgs -> Eff Effects (Either Text Value)
postMergeParentSyncCore args = do
  case validateParentSync args of
    Left err -> pure $ Left err
    Right () -> do
      branch <- runGitAt (pmpWorkingDir args) ["branch", "--show-current"]
      case branch of
        Left err -> pure $ Left err
        Right current
          | T.strip current /= pmpParentBranch args ->
              pure $ Left "parent synchronization requires the requested branch to be checked out"
          | otherwise -> do
              fetched <- runGitAt
                (pmpWorkingDir args)
                (postMergeParentSyncFetchArgs (pmpParentBranch args))
              case fetched of
                Left err -> pure $ Left err
                Right _ -> do
                  merged <- runGitAt
                    (pmpWorkingDir args)
                    (postMergeParentSyncMergeArgs (pmpParentBranch args))
                  case merged of
                    Left err -> pure $ Left err
                    Right _ -> do
                      parentCommit <- runGitAt (pmpWorkingDir args) ["rev-parse", "HEAD"]
                      remoteHead <- runGitAt (pmpWorkingDir args) ["rev-parse", remoteRef (pmpParentBranch args)]
                      case (parentCommit, remoteHead) of
                        (Right parent, Right remote)
                          | parent /= remote -> pure $ Left "parent synchronization produced divergent local and remote heads"
                          | otherwise -> do
                              ancestry <- verifyAncestryAt (pmpWorkingDir args) (pmpMergedHeadSha args) parent
                              case ancestry of
                                Left err -> pure $ Left err
                                Right proof ->
                                  pure $
                                    Right
                                      ( object
                                          [ "child_id" .= pmpChildId args,
                                            "pr_number" .= pmpPrNumber args,
                                            "repository" .= pmpRepository args,
                                            "parent_branch" .= pmpParentBranch args,
                                            "merged_head_sha" .= pmpMergedHeadSha args,
                                            "expected_base_sha" .= pmpExpectedBaseSha args,
                                            "lane_epoch" .= pmpLaneEpoch args,
                                            "parent_commit_sha" .= parent,
                                            "remote_head_sha" .= remote,
                                            "ancestry_proof" .= proof
                                          ]
                                      )
                        _ -> pure $ Left "parent synchronization could not read authoritative heads"

postMergeChangelogCore :: (Member SuspendYield effs) => PostMergeChangelogArgs -> Eff effs (Either Text Value)
postMergeChangelogCore args = do
  case validateChangelog args of
    Left err -> pure $ Left err
    Right () -> do
      branch <- runGitAt (pmcWorkingDir args) ["branch", "--show-current"]
      case branch of
        Left err -> pure $ Left err
        Right current
          | T.strip current /= pmcParentBranch args ->
              pure $ Left "changelog recovery requires the requested parent branch to be checked out"
          | otherwise -> do
              currentHead <- runGitAt (pmcWorkingDir args) ["rev-parse", "HEAD"]
              case currentHead of
                Left err -> pure $ Left err
                Right headSha
                  | headSha /= pmcExpectedBaseSha args ->
                      pure $ Left "parent branch advanced after synchronization; changelog recovery must restart"
                  | otherwise -> do
                      status <- runGitAt (pmcWorkingDir args) ["status", "--porcelain", "--", "CHANGELOG.md"]
                      case status of
                        Left err -> pure $ Left err
                        Right changes -> do
                          committed <-
                            if T.null (T.strip changes)
                              then pure (Right ())
                              else do
                                staged <- runGitAt (pmcWorkingDir args) postMergeChangelogStageArgs
                                case staged of
                                  Left err -> pure $ Left err
                                  Right _ -> do
                                    committedResult <-
                                      runGitAt
                                        (pmcWorkingDir args)
                                        (postMergeChangelogCommitArgs (pmcIssueId args))
                                    pure (fmap (const ()) committedResult)
                          case committed of
                            Left err -> pure $ Left err
                            Right () -> do
                              headSha' <- runGitAt (pmcWorkingDir args) ["rev-parse", "HEAD"]
                              pure $
                                fmap
                                  (\commitSha ->
                                    object
                                      [ "child_id" .= pmcChildId args,
                                        "issue_id" .= pmcIssueId args,
                                        "repository" .= pmcRepository args,
                                        "parent_branch" .= pmcParentBranch args,
                                        "expected_base_sha" .= pmcExpectedBaseSha args,
                                        "generation" .= pmcGeneration args,
                                        "intent_id" .= pmcIntentId args,
                                        "commit_sha" .= commitSha
                                      ]
                                  )
                                  headSha'

postMergePushCore :: PostMergePushArgs -> Eff Effects (Either Text Value)
postMergePushCore args = do
  case validatePush args of
    Left err -> pure $ Left err
    Right () -> do
      branch <- runGitAt (pmpuWorkingDir args) ["branch", "--show-current"]
      case branch of
        Left err -> pure $ Left err
        Right current
          | T.strip current /= pmpuParentBranch args ->
              pure $ Left "parent bookkeeping push requires the requested parent branch to be checked out"
          | otherwise -> do
              pushed <-
                runGitAt
                  (pmpuWorkingDir args)
                  (postMergePushGitArgs (pmpuParentBranch args) (pmpuExpectedBaseSha args))
              case pushed of
                Left err -> pure $ Left err
                Right _ -> do
                  remote <- runGitAt (pmpuWorkingDir args) ["ls-remote", "--exit-code", "origin", refName (pmpuParentBranch args)]
                  case remote >>= parseRemoteHead of
                    Left err -> pure $ Left err
                    Right remoteHead
                      | remoteHead /= pmpuPushedCommit args -> pure $ Left "parent bookkeeping push returned a different remote head"
                      | otherwise -> do
                          ancestry <- verifyAncestryAt (pmpuWorkingDir args) (pmpuPushedCommit args) remoteHead
                          case ancestry of
                            Left err -> pure $ Left err
                            Right proof ->
                              pure $
                                Right
                                  ( object
                                      [ "repository" .= pmpuRepository args,
                                        "parent_branch" .= pmpuParentBranch args,
                                        "child_id" .= pmpuChildId args,
                                        "lane_epoch" .= pmpuLaneEpoch args,
                                        "push_intent_id" .= pmpuPushIntentId args,
                                        "push_journal_id" .= pmpuPushJournalId args,
                                        "push_receipt_id" .= receiptId args,
                                        "expected_base_sha" .= pmpuExpectedBaseSha args,
                                        "pushed_commit" .= pmpuPushedCommit args,
                                        "observed_remote_head" .= remoteHead,
                                        "ancestry_proof" .= proof
                                      ]
                                  )

runGitAt :: (Member SuspendYield effs) => Maybe Text -> [Text] -> Eff effs (Either Text Text)
runGitAt workingDir args = do
  logInfo ("post-merge git before: " <> T.unwords args)
  result <-
    suspendEffect @ProcessRun
      ( Proc.RunRequest
          { Proc.runRequestCommand = "git",
            Proc.runRequestArgs = V.fromList (TL.fromStrict <$> args),
            Proc.runRequestWorkingDir = maybe "." TL.fromStrict workingDir,
            Proc.runRequestEnv = Map.empty,
            Proc.runRequestTimeoutMs = gitTimeoutMs
          }
      )
  case result of
    Left err -> pure $ Left ("git effect failed: " <> T.pack (show err))
    Right response -> do
      let stdout = T.strip (TL.toStrict (Proc.runResponseStdout response))
          stderr = T.strip (TL.toStrict (Proc.runResponseStderr response))
          exitCode = Proc.runResponseExitCode response
      logInfo ("post-merge git after: exit=" <> T.pack (show exitCode))
      pure (interpretGitResult exitCode stdout stderr)

-- | Git argv for fetching the direct parent branch before synchronization.
postMergeParentSyncFetchArgs :: Text -> [Text]
postMergeParentSyncFetchArgs branch = ["fetch", "--prune", "origin", branch]

-- | Git argv for fast-forwarding the checked-out parent branch.
postMergeParentSyncMergeArgs :: Text -> [Text]
postMergeParentSyncMergeArgs branch = ["merge", "--ff-only", remoteRef branch]

-- | Git argv for staging the generated changelog entry.
postMergeChangelogStageArgs :: [Text]
postMergeChangelogStageArgs = ["add", "--", "CHANGELOG.md"]

-- | Git argv for committing the staged changelog entry.
postMergeChangelogCommitArgs :: Int -> [Text]
postMergeChangelogCommitArgs issueId =
  [ "commit",
    "--only",
    "CHANGELOG.md",
    "-m",
    changelogMessage issueId
  ]

-- | Git argv for the compare-and-swap parent push boundary.
postMergePushGitArgs :: Text -> Text -> [Text]
postMergePushGitArgs branch base =
  [ "push",
    "--porcelain",
    forceLease branch base,
    "origin",
    "HEAD:" <> refName branch
  ]

-- | Convert one process response into the boundary's authoritative result.
interpretGitResult :: Int -> Text -> Text -> Either Text Text
interpretGitResult exitCode stdout stderr =
  if exitCode == 0
    then Right (T.strip stdout)
    else Left ("git command failed (" <> T.pack (show exitCode) <> "): " <> T.strip stderr)

verifyAncestryAt :: Maybe Text -> Text -> Text -> Eff Effects (Either Text Text)
verifyAncestryAt workingDir mergedHead parent = do
  result <- runGitAt workingDir ["merge-base", "--is-ancestor", mergedHead, parent]
  pure $ fmap (const ("ancestor:" <> mergedHead <> "->" <> parent)) result

remoteRef :: Text -> Text
remoteRef branch = "origin/" <> branch

refName :: Text -> Text
refName branch = "refs/heads/" <> branch

forceLease :: Text -> Text -> Text
forceLease branch base = "--force-with-lease=refs/heads/" <> branch <> ":" <> base

changelogMessage :: Int -> Text
changelogMessage issueId = "Update changelog for Chainlink issue #" <> T.pack (show issueId)

receiptId :: PostMergePushArgs -> Text
receiptId args = "push:" <> pmpuPushedCommit args <> ":" <> pmpuExpectedBaseSha args

parseRemoteHead :: Text -> Either Text Text
parseRemoteHead output =
  case T.words output of
    headSha : _ | not (T.null headSha) -> Right headSha
    _ -> Left "git push did not return an authoritative remote head"

validToken :: Text -> Bool
validToken value =
  let stripped = T.strip value
   in not (T.null stripped)
        && stripped == value
        && not (T.isPrefixOf "-" value)
        && not (T.any (\char -> char == '\0' || char == '\n' || char == '\r' || char == ' ') value)

validPositive :: Int -> Bool
validPositive value = value > 0

validateParentSync :: PostMergeParentSyncArgs -> Either Text ()
validateParentSync args
  | not (validToken (pmpChildId args)) = Left "child_id is required"
  | not (validPositive (pmpPrNumber args)) = Left "pr_number must be positive"
  | not (validToken (pmpRepository args)) = Left "repository is required"
  | not (validToken (pmpParentBranch args)) = Left "parent_branch is required"
  | not (validToken (pmpMergedHeadSha args)) = Left "merged_head_sha is required"
  | not (validToken (pmpExpectedBaseSha args)) = Left "expected_base_sha is required"
  | not (validPositive (pmpLaneEpoch args)) = Left "lane_epoch must be positive"
  | otherwise = Right ()

validateChangelog :: PostMergeChangelogArgs -> Either Text ()
validateChangelog args
  | not (validToken (pmcChildId args)) = Left "child_id is required"
  | not (validPositive (pmcIssueId args)) = Left "issue_id must be positive"
  | not (validToken (pmcRepository args)) = Left "repository is required"
  | not (validToken (pmcParentBranch args)) = Left "parent_branch is required"
  | not (validToken (pmcExpectedBaseSha args)) = Left "expected_base_sha is required"
  | pmcGeneration args < 0 = Left "generation must not be negative"
  | not (validToken (pmcIntentId args)) = Left "intent_id is required"
  | otherwise = Right ()

validatePush :: PostMergePushArgs -> Either Text ()
validatePush args
  | not (validToken (pmpuChildId args)) = Left "child_id is required"
  | not (validToken (pmpuRepository args)) = Left "repository is required"
  | not (validToken (pmpuParentBranch args)) = Left "parent_branch is required"
  | not (validPositive (pmpuLaneEpoch args)) = Left "lane_epoch must be positive"
  | not (validToken (pmpuPushIntentId args)) = Left "push_intent_id is required"
  | not (validToken (pmpuPushJournalId args)) = Left "push_journal_id is required"
  | not (validToken (pmpuExpectedBaseSha args)) = Left "expected_base_sha is required"
  | not (validToken (pmpuPushedCommit args)) = Left "pushed_commit is required"
  | otherwise = Right ()

logInfo :: (Member SuspendYield effs) => Text -> Eff effs ()
logInfo message =
  void $
    suspendEffect_ @LogInfo
      ( Log.InfoRequest
          { Log.infoRequestMessage = TL.fromStrict message,
            Log.infoRequestFields = ""
          }
      )
