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
    MergeIntegrationProof (..),
    postMergeParentSyncCore,
    postMergeParentSyncFetchArgs,
    postMergeParentSyncMergeArgs,
    postMergeParentSyncDescription,
    postMergeParentSyncSchema,
    PostMergeRemoteReconcile,
    postMergeRemoteReconcileCore,
    postMergeRemoteReconcileDescription,
    postMergeRemoteReconcileSchema,
    postMergeRemoteReconcileRebaseArgs,
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
    interpretGitResult,
    runGitAt,
    parseRemoteHead,
    resolveSelectedRemoteAt,
    selectRemote,
    refName,
    validToken,
    logInfo,
  )
where

import Control.Monad (void)
import Control.Monad.Freer (Eff, Member)
import Data.Aeson (FromJSON (..), ToJSON (..), Value, object, withObject, (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Aeson.Types (Pair)
import Data.Char (isControl)
import Data.Int (Int32)
import Data.Map qualified as Map
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Data.Word (Word64)
import Effects.EffectError qualified as EE
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

data PostMergeRemoteReconcile

-- | Git-verified evidence that the reviewed PR was integrated into the
-- fetched parent head. Squash merges deliberately have no source-head
-- ancestry, so their proof carries exact Forgejo binding and Git tree
-- identities instead of manufacturing an ancestry string.
data MergeIntegrationProof
  = AncestryMergeIntegrationProof Int Text Text Text
  | SquashMergeIntegrationProof Int Text Text Text Text Text Text Bool Text Int
  deriving (Show, Eq)

instance ToJSON MergeIntegrationProof where
  toJSON proof = case proof of
    AncestryMergeIntegrationProof prNumber prHead mergeCommit ancestry ->
      object
        [ "kind" .= ("ancestry" :: Text),
          "pr_number" .= prNumber,
          "pr_head_sha" .= prHead,
          "merge_commit_sha" .= mergeCommit,
          "ancestry" .= ancestry
        ]
    SquashMergeIntegrationProof
      prNumber
      prHead
      mergeCommit
      prTree
      mergeTree
      reviewedTree
      prospectiveTree
      forgejoMerged
      forgejoHead
      forgejoPr ->
        object
          [ "kind" .= ("squash" :: Text),
            "pr_number" .= prNumber,
            "pr_head_sha" .= prHead,
            "merge_commit_sha" .= mergeCommit,
            "pr_head_tree_sha" .= prTree,
            "merge_commit_tree_sha" .= mergeTree,
            "reviewed_pr_head_tree_sha" .= reviewedTree,
            "prospective_merge_tree_sha" .= prospectiveTree,
            "forgejo_merged" .= forgejoMerged,
            "forgejo_head_sha" .= forgejoHead,
            "forgejo_merge_commit_sha" .= mergeCommit,
            "forgejo_merge_commit_tree_sha" .= mergeTree,
            "forgejo_pr_number" .= forgejoPr
          ]

data PostMergeParentSyncArgs = PostMergeParentSyncArgs
  { pmpChildId :: Text,
    pmpPrNumber :: Int,
    pmpRepository :: Text,
    pmpParentBranch :: Text,
    pmpMergedHeadSha :: Text,
    pmpExpectedBaseSha :: Text,
    pmpLaneEpoch :: Int,
    pmpWorkingDir :: Maybe Text,
    pmpForgejoPrNumber :: Maybe Int,
    pmpForgejoMerged :: Maybe Bool,
    pmpForgejoHeadSha :: Maybe Text,
    pmpForgejoMergeCommitSha :: Maybe Text,
    pmpForgejoMergeCommitTreeSha :: Maybe Text,
    pmpProspectiveMergeTreeSha :: Maybe Text,
    pmpReviewedPrHeadTreeSha :: Maybe Text,
    pmpSyncOperationId :: Maybe Text,
    pmpSyncAttempt :: Maybe Int,
    pmpReconcileOnly :: Maybe Bool
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
      <*> v .:? "forgejo_pr_number"
      <*> v .:? "forgejo_merged"
      <*> v .:? "forgejo_head_sha"
      <*> v .:? "forgejo_merge_commit_sha"
      <*> v .:? "forgejo_merge_commit_tree_sha"
      <*> v .:? "prospective_merge_tree_sha"
      <*> v .:? "reviewed_pr_head_tree_sha"
      <*> v .:? "sync_operation_id"
      <*> v .:? "sync_attempt"
      <*> v .:? "reconcile_only"

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
  "Synchronize a merged child into its parent branch and return a Git-verified receipt or a structured refusal, partial-progress, retry-safety, or unknown-outcome decision. Supply the stable sync_operation_id and a monotonically increasing sync_attempt for each effect attempt; set reconcile_only to observe and classify current Git state without advancing the branch."

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
      ("forgejo_pr_number", "Authoritative Forgejo PR number bound to the merge evidence."),
      ("forgejo_merged", "Authoritative Forgejo merged flag for the exact PR."),
      ("forgejo_head_sha", "Authoritative Forgejo source head bound to the PR."),
      ("forgejo_merge_commit_sha", "Authoritative Forgejo merge commit SHA."),
      ("forgejo_merge_commit_tree_sha", "Git tree resolved from the authoritative merge commit."),
      ("prospective_merge_tree_sha", "Git merge-tree result for the reviewed base and PR head."),
      ("reviewed_pr_head_tree_sha", "Tree SHA resolved from the exact reviewed PR head."),
      ("sync_operation_id", "Stable identity for the logical parent synchronization."),
      ("sync_attempt", "Durable attempt number for this synchronization operation."),
      ("reconcile_only", "Observe remote and local Git evidence without fast-forwarding."),
      ("working_dir", "Optional relative repository working directory.")
    ]

postMergeRemoteReconcileDescription :: Text
postMergeRemoteReconcileDescription =
  "Fetch an advanced parent branch and rebase local post-merge bookkeeping onto it, returning Git-verified rebuild evidence."

postMergeRemoteReconcileSchema :: Aeson.Object
postMergeRemoteReconcileSchema =
  genericToolSchemaWith @PostMergeParentSyncArgs
    [ ("child_id", "Stable child slice identifier."),
      ("pr_number", "Merged Forgejo pull request number."),
      ("repository", "Authoritative repository identity."),
      ("parent_branch", "Direct parent integration branch."),
      ("merged_head_sha", "Exact merged child head SHA."),
      ("expected_base_sha", "The stale parent base captured by the failed push."),
      ("lane_epoch", "Durable integration-lane epoch."),
      ("forgejo_pr_number", "Authoritative Forgejo PR number bound to the merge evidence."),
      ("forgejo_merged", "Authoritative Forgejo merged flag for the exact PR."),
      ("forgejo_head_sha", "Authoritative Forgejo source head bound to the PR."),
      ("forgejo_merge_commit_sha", "Authoritative Forgejo merge commit SHA."),
      ("forgejo_merge_commit_tree_sha", "Git tree resolved from the authoritative merge commit."),
      ("prospective_merge_tree_sha", "Git merge-tree result for the reviewed base and PR head."),
      ("reviewed_pr_head_tree_sha", "Tree SHA resolved from the exact reviewed PR head."),
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

instance MCPTool PostMergeRemoteReconcile where
  type ToolArgs PostMergeRemoteReconcile = PostMergeParentSyncArgs
  toolName = "post_merge_remote_reconcile"
  toolDescription = postMergeRemoteReconcileDescription
  toolSchema = postMergeRemoteReconcileSchema
  toolHandlerEff args = either errorResult successResult <$> postMergeRemoteReconcileCore args

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

postMergeParentSyncCore :: (Member SuspendYield effs) => PostMergeParentSyncArgs -> Eff effs (Either Text Value)
postMergeParentSyncCore args = do
  case validateParentSync args of
    Left err -> pure $ Right (syncDecision args "pre_mutation_refusal" "validation" [] True err)
    Right () -> do
      selectedRemote <- resolveSelectedRemoteAt (pmpWorkingDir args)
      case selectedRemote of
        Left err -> pure $ Right (syncDecision args "pre_mutation_refusal" "remote_selection" [] True err)
        Right remote -> do
          branch <- runGitAt (pmpWorkingDir args) ["branch", "--show-current"]
          case branch of
            Left err -> pure $ Right (syncDecision args "pre_mutation_refusal" "branch_check" [] True err)
            Right current
              | T.strip current /= pmpParentBranch args ->
                  pure $ Right (syncDecision args "pre_mutation_refusal" "branch_check" [] True "parent synchronization requires the requested branch to be checked out")
              | pmpReconcileOnly args == Just True -> reconcileParentSync args remote
              | otherwise -> synchronizeParentBranch args remote

data SyncGitResult
  = SyncGitResponse Int32 Text Text
  | SyncGitInterrupted Bool Text

synchronizeParentBranch :: (Member SuspendYield effs) => PostMergeParentSyncArgs -> Text -> Eff effs (Either Text Value)
synchronizeParentBranch args remote = do
  fetched <- runGitSyncAt (pmpWorkingDir args) (postMergeParentSyncFetchArgs remote (pmpParentBranch args))
  case fetched of
    SyncGitInterrupted True diagnostic ->
      pure $ Right (syncDecision args "pre_mutation_refusal" "fetch" [] True diagnostic)
    SyncGitInterrupted False diagnostic ->
      pure $ Right (syncDecision args "unknown_interruption" "fetch" [] False diagnostic)
    SyncGitResponse exitCode stdout stderr
      | exitCode /= 0 ->
          pure $ Right (syncDecision args "unknown_interruption" "fetch" [] False (exitDiagnostic exitCode stdout stderr))
      | otherwise -> fastForwardParentBranch args remote

fastForwardParentBranch :: (Member SuspendYield effs) => PostMergeParentSyncArgs -> Text -> Eff effs (Either Text Value)
fastForwardParentBranch args remote = do
  before <- runGitAt (pmpWorkingDir args) ["rev-parse", "HEAD"]
  case before of
    Left diagnostic ->
      pure $ Right (syncDecision args "known_partial_progress" "pre_fast_forward_observation" ["fetch"] True diagnostic)
    Right beforeHead -> do
      merged <- runGitSyncAt (pmpWorkingDir args) (postMergeParentSyncMergeArgs remote (pmpParentBranch args))
      case merged of
        SyncGitInterrupted True diagnostic ->
          pure $ Right (syncDecision args "known_partial_progress" "fast_forward" ["fetch"] True diagnostic)
        SyncGitInterrupted False diagnostic ->
          pure $ Right (syncDecision args "unknown_interruption" "fast_forward" ["fetch"] False diagnostic)
        SyncGitResponse 0 _ _ -> verifySynchronizedParent args remote ["fetch", "fast_forward"]
        SyncGitResponse exitCode stdout stderr ->
          classifyRejectedFastForward args remote beforeHead exitCode stdout stderr

classifyRejectedFastForward :: (Member SuspendYield effs) => PostMergeParentSyncArgs -> Text -> Text -> Int32 -> Text -> Text -> Eff effs (Either Text Value)
classifyRejectedFastForward args remote beforeHead exitCode stdout stderr = do
  after <- runGitAt (pmpWorkingDir args) ["rev-parse", "HEAD"]
  mergeHead <- runGitSyncAt (pmpWorkingDir args) ["rev-parse", "--quiet", "--verify", "MERGE_HEAD"]
  case (after, mergeHead) of
    (Right afterHead, SyncGitResponse 1 _ _)
      | afterHead == beforeHead ->
          pure $ Right (syncDecision args "known_partial_progress" "fast_forward" ["fetch"] True (exitDiagnostic exitCode stdout stderr))
    (Right afterHead, _)
      | afterHead /= beforeHead ->
          verifySynchronizedParent args remote ["fetch", "fast_forward"]
    _ ->
      pure $ Right (syncDecision args "unknown_interruption" "fast_forward_verification" ["fetch"] False "Git refused fast-forward and its local result could not be proven")

verifySynchronizedParent :: (Member SuspendYield effs) => PostMergeParentSyncArgs -> Text -> [Text] -> Eff effs (Either Text Value)
verifySynchronizedParent args remote completed = do
  parentCommit <- runGitAt (pmpWorkingDir args) ["rev-parse", "HEAD"]
  remoteHead <- runGitAt (pmpWorkingDir args) ["rev-parse", remoteRef remote (pmpParentBranch args)]
  case (parentCommit, remoteHead) of
    (Right parent, Right observedRemote)
      | parent /= observedRemote ->
          pure $ Right (syncDecision args "unknown_interruption" "receipt_verification" completed False "local parent and selected remote heads differ after fast-forward")
      | otherwise -> do
          integration <- verifyMergeIntegrationAt (pmpWorkingDir args) args parent
          case integration of
            Left diagnostic ->
              pure $ Right (syncDecision args "unknown_interruption" "receipt_verification" completed False diagnostic)
            Right integrationProof ->
              pure $ Right (syncReceipt args remote parent observedRemote integrationProof completed)
    _ ->
      pure $ Right (syncDecision args "unknown_interruption" "receipt_verification" completed False "parent synchronization could not read authoritative local and remote heads")

reconcileParentSync :: (Member SuspendYield effs) => PostMergeParentSyncArgs -> Text -> Eff effs (Either Text Value)
reconcileParentSync args remote = do
  fetched <- runGitSyncAt (pmpWorkingDir args) (postMergeParentSyncFetchArgs remote (pmpParentBranch args))
  case fetched of
    SyncGitResponse 0 _ _ -> inspectSynchronizedParent args remote
    SyncGitResponse exitCode stdout stderr ->
      pure $ Right (syncDecision args "unknown_interruption" "reconcile_fetch" [] False (exitDiagnostic exitCode stdout stderr))
    SyncGitInterrupted _ diagnostic ->
      pure $ Right (syncDecision args "unknown_interruption" "reconcile_fetch" [] False diagnostic)

inspectSynchronizedParent :: (Member SuspendYield effs) => PostMergeParentSyncArgs -> Text -> Eff effs (Either Text Value)
inspectSynchronizedParent args remote = do
  localHead <- runGitAt (pmpWorkingDir args) ["rev-parse", "HEAD"]
  remoteHead <- runGitAt (pmpWorkingDir args) ["rev-parse", remoteRef remote (pmpParentBranch args)]
  case (localHead, remoteHead) of
    (Right local, Right observedRemote) ->
      if local == observedRemote
        then
          confirmedOrUnresolved
            args
            remote
            local
            observedRemote
            ["fetch", "current_git_matches"]
            "current_git"
        else safeRetryWhenFastForwardable args remote local observedRemote
    _ -> pure $ Right (syncDecision args "unknown_interruption" "reconcile_observation" ["fetch"] False "current local or selected remote head is unavailable")

safeRetryWhenFastForwardable :: (Member SuspendYield effs) => PostMergeParentSyncArgs -> Text -> Text -> Text -> Eff effs (Either Text Value)
safeRetryWhenFastForwardable args remote local observedRemote = do
  ancestry <- verifyAncestryAt (pmpWorkingDir args) local observedRemote
  case ancestry of
    Left _ -> pure $ Right (syncDecision args "unresolved" "reconcile_observation" ["fetch"] False "local and remote history does not prove a safe fast-forward retry")
    Right _ -> confirmedOrUnresolved args remote local observedRemote ["fetch", "fast_forward_available"] "safe_to_retry"

confirmedOrUnresolved :: (Member SuspendYield effs) => PostMergeParentSyncArgs -> Text -> Text -> Text -> [Text] -> Text -> Eff effs (Either Text Value)
confirmedOrUnresolved args remote local observedRemote completed basis = do
  integration <- verifyMergeIntegrationAt (pmpWorkingDir args) args observedRemote
  case integration of
    Left _ -> pure $ Right (syncDecision args "unresolved" "reconcile_integration_proof" completed False "current Git state does not prove the merged child is integrated")
    Right proof
      | basis == "safe_to_retry" ->
          pure $ Right (syncDecision args "safe_to_retry" "reconcile_complete" completed True "remote contains the merged child and local HEAD is an ancestor")
      | local /= observedRemote ->
          pure $ Right (syncDecision args "unresolved" "reconcile_complete" completed False "local and selected remote heads differ")
      | otherwise ->
          pure $ Right (syncReceipt args remote local observedRemote proof completed)

syncDecision :: PostMergeParentSyncArgs -> Text -> Text -> [Text] -> Bool -> Text -> Value
syncDecision args outcome phase completed retrySafe diagnostic =
  object $
    [ "sync_outcome" .= outcome,
      "sync_phase" .= phase,
      "completed_steps" .= completed,
      "retry_safe" .= retrySafe,
      "diagnostic" .= redactDiagnostic diagnostic
    ]
      <> syncCorrelation args

syncReceipt :: PostMergeParentSyncArgs -> Text -> Text -> Text -> MergeIntegrationProof -> [Text] -> Value
syncReceipt args remote parent observedRemote integrationProof completed =
  object $
    [ "sync_outcome" .= ("confirmed" :: Text),
      "sync_phase" .= ("complete" :: Text),
      "completed_steps" .= completed,
      "retry_safe" .= False,
      "diagnostic" .= ("" :: Text),
      "child_id" .= pmpChildId args,
      "pr_number" .= pmpPrNumber args,
      "repository" .= pmpRepository args,
      "parent_branch" .= pmpParentBranch args,
      "remote_name" .= remote,
      "merged_head_sha" .= pmpMergedHeadSha args,
      "expected_base_sha" .= pmpExpectedBaseSha args,
      "lane_epoch" .= pmpLaneEpoch args,
      "parent_commit_sha" .= parent,
      "remote_head_sha" .= observedRemote,
      "ancestry_proof" .= legacyProof integrationProof,
      "merge_integration_proof" .= integrationProof
    ]
      <> syncCorrelation args

syncCorrelation :: PostMergeParentSyncArgs -> [Pair]
syncCorrelation args =
  maybe [] (\value -> ["sync_operation_id" .= value]) (pmpSyncOperationId args)
    <> maybe [] (\value -> ["sync_attempt" .= value]) (pmpSyncAttempt args)

runGitSyncAt :: (Member SuspendYield effs) => Maybe Text -> [Text] -> Eff effs SyncGitResult
runGitSyncAt workingDir args = do
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
    Left err -> do
      logInfo "post-merge git outcome unavailable"
      pure (SyncGitInterrupted (isPreMutationGitError err) (redactDiagnostic (T.pack (show err))))
    Right response -> do
      let stdout = T.strip (TL.toStrict (Proc.runResponseStdout response))
          stderr = TL.toStrict (Proc.runResponseStderr response)
          exitCode = Proc.runResponseExitCode response
      logInfo ("post-merge git after: exit=" <> T.pack (show exitCode))
      pure (SyncGitResponse exitCode stdout stderr)

isPreMutationGitError :: EE.EffectError -> Bool
isPreMutationGitError (EE.EffectError (Just (EE.EffectErrorKindCustom custom))) =
  EE.customCode custom == "git_mutation_unavailable"
    || EE.customCode custom == "git_audit_unavailable"
isPreMutationGitError _ = False

exitDiagnostic :: Int32 -> Text -> Text -> Text
exitDiagnostic exitCode stdout stderr =
  redactDiagnostic
    ( "Git exited with code "
        <> T.pack (show exitCode)
        <> "; stdout: "
        <> stdout
        <> "; stderr: "
        <> stderr
    )

redactDiagnostic :: Text -> Text
redactDiagnostic =
  T.take 500 . T.unwords . map redactCredentialToken . T.words . T.filter (not . isControl)

redactCredentialToken :: Text -> Text
redactCredentialToken = redactSensitiveAssignments . redactUrlAuthority

redactSensitiveAssignments :: Text -> Text
redactSensitiveAssignments value =
  foldl redactMarker value ["token=", "password=", "authorization="]
  where
    redactMarker value marker =
      let (prefix, remainder) = T.breakOn marker (T.toLower value)
       in if T.null remainder
            then value
            else T.take (T.length prefix) value <> marker <> "[REDACTED]"

redactUrlAuthority :: Text -> Text
redactUrlAuthority token =
  let (scheme, remainder) = T.breakOn "://" token
      authorityStart = T.drop 3 remainder
      (authority, suffix) = T.breakOn "/" authorityStart
      (_, host) = T.breakOnEnd "@" authority
   in if T.null remainder || T.null host
        then token
        else scheme <> "://[REDACTED]@" <> host <> suffix

postMergeRemoteReconcileCore :: (Member SuspendYield effs) => PostMergeParentSyncArgs -> Eff effs (Either Text Value)
postMergeRemoteReconcileCore args = do
  case validateParentSync args of
    Left err -> pure $ Left err
    Right () -> do
      selectedRemote <- resolveSelectedRemoteAt (pmpWorkingDir args)
      case selectedRemote of
        Left err -> pure $ Left err
        Right remote -> do
          branch <- runGitAt (pmpWorkingDir args) ["branch", "--show-current"]
          case branch of
            Left err -> pure $ Left err
            Right current
              | T.strip current /= pmpParentBranch args ->
                  pure $ Left "remote reconciliation requires the requested branch to be checked out"
              | otherwise -> do
                  fetched <- runGitAt (pmpWorkingDir args) (postMergeParentSyncFetchArgs remote (pmpParentBranch args))
                  case fetched of
                    Left err -> pure $ Left err
                    Right _ -> do
                      remoteBefore <-
                        runGitAt
                          (pmpWorkingDir args)
                          ["rev-parse", remoteRef remote (pmpParentBranch args)]
                      localHead <- runGitAt (pmpWorkingDir args) ["rev-parse", "HEAD"]
                      case (remoteBefore, localHead) of
                        (Right remoteHeadBefore, Right local)
                          | remoteHeadBefore == pmpExpectedBaseSha args ->
                              pure $ Left "remote reconciliation found no parent-branch advancement"
                          | local == pmpExpectedBaseSha args ->
                              pure $ Left "remote reconciliation found no local bookkeeping to rebuild"
                          | otherwise -> do
                              baseReachable <-
                                runGitAt
                                  (pmpWorkingDir args)
                                  ["merge-base", "--is-ancestor", pmpExpectedBaseSha args, local]
                              case baseReachable of
                                Left err -> pure $ Left ("failed parent-base ancestry check: " <> err)
                                Right _ -> do
                                  rebased <-
                                    runGitAt
                                      (pmpWorkingDir args)
                                      (postMergeRemoteReconcileRebaseArgs remote (pmpParentBranch args) (pmpExpectedBaseSha args))
                                  case rebased of
                                    Left err -> do
                                      _ <- runGitAt (pmpWorkingDir args) ["rebase", "--abort"]
                                      pure $ Left ("failed to rebuild bookkeeping on the advanced parent: " <> err)
                                    Right _ -> do
                                      rebuilt <- runGitAt (pmpWorkingDir args) ["rev-parse", "HEAD"]
                                      remoteAfter <-
                                        runGitAt
                                          (pmpWorkingDir args)
                                          ["rev-parse", remoteRef remote (pmpParentBranch args)]
                                      case (rebuilt, remoteAfter) of
                                        (Right rebuiltHead, Right remoteHead)
                                          | remoteHead /= remoteHeadBefore ->
                                              pure $ Left "parent branch advanced during bookkeeping rebuild; recovery must restart"
                                          | otherwise -> do
                                              remoteProof <-
                                                verifyAncestryAt
                                                  (pmpWorkingDir args)
                                                  remoteHead
                                                  rebuiltHead
                                              integration <-
                                                verifyReconcileIntegrationAt
                                                  (pmpWorkingDir args)
                                                  args
                                                  rebuiltHead
                                              case (remoteProof, integration) of
                                                (Right remoteAncestry, Right integrationProof) ->
                                                  pure $
                                                    Right
                                                      ( object
                                                          [ "child_id" .= pmpChildId args,
                                                            "pr_number" .= pmpPrNumber args,
                                                            "repository" .= pmpRepository args,
                                                            "parent_branch" .= pmpParentBranch args,
                                                            "remote_name" .= remote,
                                                            "merged_head_sha" .= pmpMergedHeadSha args,
                                                            "expected_base_sha" .= pmpExpectedBaseSha args,
                                                            "lane_epoch" .= pmpLaneEpoch args,
                                                            "parent_commit_sha" .= rebuiltHead,
                                                            "rebuilt_commit_sha" .= rebuiltHead,
                                                            "remote_head_sha" .= remoteHead,
                                                            "new_base_sha" .= remoteHead,
                                                            "remote_ancestry_proof" .= remoteAncestry,
                                                            "ancestry_proof" .= legacyProof integrationProof,
                                                            "merge_integration_proof" .= integrationProof
                                                          ]
                                                      )
                                                _ -> pure $ Left "bookkeeping rebuild could not prove the rebuilt integration"
                                        _ -> pure $ Left "bookkeeping rebuild could not read authoritative heads"
                        _ -> pure $ Left "remote reconciliation could not read authoritative heads"

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
                                  ( \commitSha ->
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
      selectedRemote <- resolveSelectedRemoteAt (pmpuWorkingDir args)
      case selectedRemote of
        Left err -> pure $ Left err
        Right remote -> do
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
                      (postMergePushGitArgs remote (pmpuParentBranch args) (pmpuExpectedBaseSha args))
                  case pushed of
                    Left err -> pure $ Left err
                    Right _ -> do
                      observed <-
                        runGitAt
                          (pmpuWorkingDir args)
                          ["ls-remote", "--exit-code", remote, refName (pmpuParentBranch args)]
                      case observed >>= parseRemoteHead of
                        Left err -> pure $ Left err
                        Right remoteHead
                          | remoteHead /= pmpuPushedCommit args ->
                              pure $ Left "parent bookkeeping push returned a different remote head"
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
                                            "remote_name" .= remote,
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

resolveSelectedRemoteAt :: (Member SuspendYield effs) => Maybe Text -> Eff effs (Either Text Text)
resolveSelectedRemoteAt workingDir = do
  configured <- runGitAt workingDir ["config", "--get", "exomonad.remote"]
  remotes <- runGitAt workingDir ["remote"]
  pure $ do
    names <- remotes >>= parseRemoteNames
    selectRemote configured names

parseRemoteNames :: Text -> Either Text [Text]
parseRemoteNames output =
  let names = filter (not . T.null) (map T.strip (T.lines output))
   in if null names || any (not . validToken) names
        then Left "Git remote inspection returned no valid remote names"
        else Right names

selectRemote :: Either Text Text -> [Text] -> Either Text Text
selectRemote configured names =
  case configured of
    Right remote
      | remote `elem` names -> Right remote
      | otherwise -> Left "configured exomonad.remote is not a configured Git remote"
    Left err
      | isMissingConfigValue err ->
          if "origin" `elem` names
            then Right "origin"
            else Left "post-merge synchronization needs exomonad.remote or an origin remote"
    Left err -> Left ("could not resolve configured Git remote: " <> err)

isMissingConfigValue :: Text -> Bool
isMissingConfigValue value = value == "git command failed (1):" || value == "git command failed (1): "

-- | Git argv for fetching the direct parent branch before synchronization.
postMergeParentSyncFetchArgs :: Text -> Text -> [Text]
postMergeParentSyncFetchArgs remote branch = ["fetch", "--prune", remote, branch]

postMergeRemoteReconcileRebaseArgs :: Text -> Text -> Text -> [Text]
postMergeRemoteReconcileRebaseArgs remote branch base =
  ["rebase", "--onto", remoteRef remote branch, base, "HEAD"]

-- | Git argv for fast-forwarding the checked-out parent branch.
postMergeParentSyncMergeArgs :: Text -> Text -> [Text]
postMergeParentSyncMergeArgs remote branch = ["merge", "--ff-only", remoteRef remote branch]

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
postMergePushGitArgs :: Text -> Text -> Text -> [Text]
postMergePushGitArgs remote branch base =
  [ "push",
    "--porcelain",
    forceLease branch base,
    remote,
    "HEAD:" <> refName branch
  ]

-- | Convert one process response into the boundary's authoritative result.
interpretGitResult :: Int32 -> Text -> Text -> Either Text Text
interpretGitResult exitCode stdout stderr =
  if exitCode == 0
    then Right (T.strip stdout)
    else Left ("git command failed (" <> T.pack (show exitCode) <> "): " <> T.strip stderr)

verifyAncestryAt :: (Member SuspendYield effs) => Maybe Text -> Text -> Text -> Eff effs (Either Text Text)
verifyAncestryAt workingDir mergedHead parent = do
  result <- runGitAt workingDir ["merge-base", "--is-ancestor", mergedHead, parent]
  pure $ fmap (const ("ancestor:" <> mergedHead <> "->" <> parent)) result

verifyMergeIntegrationAt :: (Member SuspendYield effs) => Maybe Text -> PostMergeParentSyncArgs -> Text -> Eff effs (Either Text MergeIntegrationProof)
verifyMergeIntegrationAt workingDir args parent = do
  ancestry <- verifyAncestryAt workingDir (pmpMergedHeadSha args) parent
  case ancestry of
    Right proof ->
      pure $
        Right
          ( AncestryMergeIntegrationProof
              (pmpPrNumber args)
              (pmpMergedHeadSha args)
              parent
              proof
          )
    Left err
      | err /= "git command failed (1): " -> pure (Left err)
      | otherwise -> verifySquashIntegrationAt workingDir args parent

-- | Reconciliation rebases bookkeeping onto a later parent head. For a squash
-- merge the original Forgejo merge commit is the durable expected base, so the
-- rebuilt bookkeeping commit must not be required to inherit the PR head.
verifyReconcileIntegrationAt :: (Member SuspendYield effs) => Maybe Text -> PostMergeParentSyncArgs -> Text -> Eff effs (Either Text MergeIntegrationProof)
verifyReconcileIntegrationAt workingDir args rebuiltHead = do
  ancestry <- verifyAncestryAt workingDir (pmpMergedHeadSha args) rebuiltHead
  case ancestry of
    Right proof ->
      pure $
        Right
          ( AncestryMergeIntegrationProof
              (pmpPrNumber args)
              (pmpMergedHeadSha args)
              rebuiltHead
              proof
          )
    Left err
      | err /= "git command failed (1): " -> pure (Left err)
      | otherwise -> verifySquashIntegrationAt workingDir args (pmpExpectedBaseSha args)

verifySquashIntegrationAt :: (Member SuspendYield effs) => Maybe Text -> PostMergeParentSyncArgs -> Text -> Eff effs (Either Text MergeIntegrationProof)
verifySquashIntegrationAt workingDir args parent =
  case squashEvidence args of
    Left err -> pure (Left err)
    Right (forgejoPr, forgejoHead, forgejoMergeCommit, reviewedTree, authoritativeMergeTree, prospectiveTree) ->
      if forgejoMergeCommit /= parent
        then pure $ Left "squash merge proof disagrees with the authoritative merge commit"
        else do
          prTree <- runGitAt workingDir ["rev-parse", pmpMergedHeadSha args <> "^{tree}"]
          mergeTree <- runGitAt workingDir ["rev-parse", parent <> "^{tree}"]
          case (prTree, mergeTree) of
            (Right prTreeSha, Right mergeTreeSha)
              | prTreeSha /= reviewedTree ->
                  pure $ Left "squash merge proof disagrees with the reviewed PR head tree"
              | mergeTreeSha /= authoritativeMergeTree ->
                  pure $ Left "squash merge proof disagrees with the authoritative merge tree"
              | mergeTreeSha /= prospectiveTree ->
                  pure $ Left "squash merge proof disagrees with the prospective merge tree"
              | otherwise ->
                  pure $
                    Right
                      ( SquashMergeIntegrationProof
                          forgejoPr
                          (pmpMergedHeadSha args)
                          forgejoMergeCommit
                          prTreeSha
                          mergeTreeSha
                          reviewedTree
                          prospectiveTree
                          True
                          forgejoHead
                          forgejoPr
                      )
            _ -> pure $ Left "squash merge proof could not read Git tree identities"

squashEvidence :: PostMergeParentSyncArgs -> Either Text (Int, Text, Text, Text, Text, Text)
squashEvidence args = do
  forgejoPr <- maybe (Left "squash merge proof requires the authoritative Forgejo PR number") Right (pmpForgejoPrNumber args)
  if forgejoPr /= pmpPrNumber args
    then Left "squash merge proof PR number does not match the requested PR"
    else pure ()
  case pmpForgejoMerged args of
    Just True -> pure ()
    _ -> Left "squash merge proof requires authoritative Forgejo merged evidence"
  forgejoHead <- maybe (Left "squash merge proof requires the authoritative Forgejo PR head") Right (pmpForgejoHeadSha args)
  if forgejoHead /= pmpMergedHeadSha args
    then Left "squash merge proof PR head does not match the requested head"
    else pure ()
  forgejoMergeCommit <- maybe (Left "squash merge proof requires the authoritative Forgejo merge commit") Right (pmpForgejoMergeCommitSha args)
  authoritativeMergeTree <- maybe (Left "squash merge proof requires the authoritative Forgejo merge tree") Right (pmpForgejoMergeCommitTreeSha args)
  prospectiveTree <- maybe (Left "squash merge proof requires the prospective merge tree") Right (pmpProspectiveMergeTreeSha args)
  reviewedTree <- maybe (Left "squash merge proof requires the reviewed PR head tree") Right (pmpReviewedPrHeadTreeSha args)
  if all validToken [forgejoHead, forgejoMergeCommit, authoritativeMergeTree, prospectiveTree, reviewedTree]
    then Right (forgejoPr, forgejoHead, forgejoMergeCommit, reviewedTree, authoritativeMergeTree, prospectiveTree)
    else Left "squash merge proof contains invalid tree or commit identity"

legacyProof :: MergeIntegrationProof -> Text
legacyProof proof = case proof of
  AncestryMergeIntegrationProof _ _ _ ancestry -> ancestry
  SquashMergeIntegrationProof _ _ _ _ _ _ _ _ _ _ -> "squash-tree-match"

remoteRef :: Text -> Text -> Text
remoteRef remote branch = remote <> "/" <> branch

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
  | not (validSyncCorrelation args) = Left "sync operation identity and positive attempt must be supplied together"
  | pmpReconcileOnly args == Just True && pmpSyncOperationId args == Nothing =
      Left "reconciliation requires a sync operation identity and attempt"
  | otherwise = Right ()

validSyncCorrelation :: PostMergeParentSyncArgs -> Bool
validSyncCorrelation args =
  case (pmpSyncOperationId args, pmpSyncAttempt args) of
    (Nothing, Nothing) -> True
    (Just identity, Just attempt) -> validDigest identity && validPositive attempt
    _ -> False

validDigest :: Text -> Bool
validDigest value =
  T.length value == 64 && T.all (\character -> character `elem` ("0123456789abcdef" :: String)) value

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
