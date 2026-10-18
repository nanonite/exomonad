{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

-- | Merge PR tool - merges a child's PR with readiness checks.
--
-- 'mergePRCore' contains the shared I/O logic.
-- Role-specific MCP wrappers apply their own state transitions.
module ExoMonad.Guest.Tools.MergePR
  ( MergePR,
    MergePRArgs (..),
    MergePROutput (..),
    mergePRCore,
    mergePRDescription,
    mergePRSchema,
    mergePRRender,
    extractAgentName,
    Readiness (..),
    watcherMergeGate,
    parentApprovalWatcherMergeGate,
    authenticatedReviewEvidenceGate,
    mergeExpectedHeadSha,
  )
where

import Control.Monad (void, when)
import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON, object, withObject, (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.ByteString.Lazy qualified as BSL
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Agent qualified as Agent
import Effects.EffectError (EffectError)
import Effects.FilePr qualified as FPR
import Effects.Git qualified as Git
import Effects.Github qualified as GH
import Effects.Log qualified as Log
import Effects.MergePr qualified as MP
import ExoMonad.Effects.Agent (AgentCleanup, AgentWatcherPrState)
import ExoMonad.Effects.FilePR (FilePRLocalPrGet)
import ExoMonad.Effects.Git (GitGetBranch, GitGetRepoInfo)
import ExoMonad.Effects.GitHub (GitHubGetPullRequest)
import ExoMonad.Effects.Log (LogEmitEvent, LogError, LogInfo)
import ExoMonad.Effects.MergePR (MergePRMergePr)
import ExoMonad.Guest.Tool.Class (Effects, MCPCallOutput, MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (JsonSchema (..), genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect, suspendEffect_)
import GHC.Generics (Generic)
import Proto3.Suite.Types qualified as Protobuf

data MergePR

data MergePRArgs = MergePRArgs
  { mprPrNumber :: Int,
    mprStrategy :: Maybe Text,
    mprWorkingDir :: Maybe Text,
    mprChainlinkIssueId :: Maybe Int,
    mprExpectedBaseSha :: Maybe Text,
    mprExpectedHeadSha :: Maybe Text,
    mprExpectedPatchDigest :: Maybe Text,
    mprExpectedMergeTreeSha :: Maybe Text,
    mprAggregateApproval :: Maybe AggregateApprovalRef
  }
  deriving (Show, Eq, Generic)

data AggregateApprovalRef = AggregateApprovalRef
  { aarReceiptId :: Text,
    aarParentRunId :: Text,
    aarParentScopePath :: V.Vector Text,
    aarCandidateKey :: Text,
    aarControllerEpoch :: Text
  }
  deriving (Show, Eq, Generic)

instance JsonSchema AggregateApprovalRef where
  toSchema =
    object
      [ "type" .= ("object" :: Text),
        "properties"
          .= object
            [ "receipt_id" .= object ["type" .= ("string" :: Text)],
              "parent_run_id" .= object ["type" .= ("string" :: Text)],
              "parent_scope_path" .= object ["type" .= ("array" :: Text), "items" .= object ["type" .= ("string" :: Text)]],
              "candidate_key" .= object ["type" .= ("string" :: Text)],
              "controller_epoch" .= object ["type" .= ("string" :: Text)]
            ],
        "required" .= (["receipt_id", "parent_run_id", "parent_scope_path", "candidate_key", "controller_epoch"] :: [Text])
      ]

instance FromJSON AggregateApprovalRef where
  parseJSON = withObject "AggregateApprovalRef" $ \v -> do
    receiptId <- v .: "receipt_id"
    parentRunId <- v .: "parent_run_id"
    parentScopePath <- v .: "parent_scope_path"
    candidateKey <- v .: "candidate_key"
    controllerEpoch <- v .: "controller_epoch"
    when (any (T.null . T.strip) [receiptId, parentRunId, candidateKey, controllerEpoch]) $
      fail "aggregate approval reference contains an empty coordinate"
    when (V.null parentScopePath || V.any (T.null . T.strip) parentScopePath) $
      fail "aggregate approval reference requires a non-empty parent scope path"
    pure
      AggregateApprovalRef
        { aarReceiptId = receiptId,
          aarParentRunId = parentRunId,
          aarParentScopePath = parentScopePath,
          aarCandidateKey = candidateKey,
          aarControllerEpoch = controllerEpoch
        }

instance FromJSON MergePRArgs where
  parseJSON = withObject "MergePRArgs" $ \v ->
    MergePRArgs
      <$> v .: "pr_number"
      <*> v .:? "strategy"
      <*> v .:? "working_dir"
      <*> v .:? "chainlink_issue_id"
      <*> v .:? "expected_base_sha"
      <*> v .:? "expected_head_sha"
      <*> v .:? "expected_patch_digest"
      <*> v .:? "expected_merge_tree_sha"
      <*> v .:? "aggregate_approval"

data MergePROutput = MergePROutput
  { mpoSuccess :: Bool,
    mpoMessage :: Text,
    mpoGitFetched :: Bool,
    mpoBranchName :: Text
  }
  deriving (Show, Eq, Generic)

instance Aeson.ToJSON MergePROutput where
  toJSON (MergePROutput s m g b) =
    object
      [ "success" .= s,
        "message" .= m,
        "git_fetched" .= g,
        "branch_name" .= b
      ]

-- | Shared tool description for merge_pr.
mergePRDescription :: Text
mergePRDescription = "Merge a pull request after live readiness checks. Local parent synchronization and post-merge bookkeeping belong to the durable TL post-merge flow; this tool does not pull. Leaf PRs require an authenticated Forgejo reviewer approval for the exact current head. A plan-declared aggregate may instead carry its durable parent-approval reference; the host validates that receipt and all current evidence before merging. Both paths require passing CI, verified publication ownership, and exact compare evidence. Stale or missing evidence is rejected. Pass chainlink_issue_id to close an issue after a successful merge."

-- | Shared tool schema for merge_pr.
mergePRSchema :: Aeson.Object
mergePRSchema =
  genericToolSchemaWith @MergePRArgs
    [ ("pr_number", "PR number to merge"),
      ("strategy", "Merge strategy: squash (default), merge, or rebase"),
      ("working_dir", "Working directory for git operations"),
      ("chainlink_issue_id", "Optional Chainlink issue ID to close and commit CHANGELOG.md after a successful merge"),
      ("aggregate_approval", "Durable parent approval reference for a declared aggregate PR; host revalidates it")
    ]

-- | Core merge_pr I/O: self-merge guard + readiness check + remote merge + cleanup.
-- Returns Left on error, Right MergePROutput on success.
mergePRCore :: MergePRArgs -> Eff Effects (Either Text MergePROutput)
mergePRCore args = do
  let prNum = mprPrNumber args
  void $ suspendEffect_ @LogInfo (Log.InfoRequest {Log.infoRequestMessage = TL.fromStrict $ "MergePR: Merging PR #" <> T.pack (show prNum), Log.infoRequestFields = ""})

  -- Get repo info and branch (shared across self-merge guard and readiness check)
  repoInfoResult <- suspendEffect @GitGetRepoInfo (Git.GetRepoInfoRequest {Git.getRepoInfoRequestWorkingDir = "."})
  branchResult <- suspendEffect @GitGetBranch (Git.GetBranchRequest {Git.getBranchRequestWorkingDir = "."})

  case (repoInfoResult, branchResult) of
    (Right repoInfo, Right branchResp) -> do
      let owner = TL.toStrict (Git.getRepoInfoResponseOwner repoInfo)
          repo = TL.toStrict (Git.getRepoInfoResponseName repoInfo)
          currentBranch = TL.toStrict (Git.getBranchResponseBranch branchResp)

      if Git.getBranchResponseDetached branchResp
        then do
          void $ suspendEffect_ @LogError (Log.ErrorRequest {Log.errorRequestMessage = "MergePR: current worktree is detached; cannot verify self-merge guard", Log.errorRequestFields = ""})
          pure $ Left ("Cannot merge PR #" <> T.pack (show prNum) <> " from a detached HEAD worktree.")
        else do
          -- Self-merge guard: agents cannot merge their own PRs
          localPrResult <-
            suspendEffect @FilePRLocalPrGet
              FPR.LocalPrGetRequest
                { FPR.localPrGetRequestPrNumber = fromIntegral prNum
                }
          case localPrResult of
            Right localPr
              | FPR.localPrResponseFound localPr -> mergeFromLocalPr args prNum owner repo currentBranch localPr
            _ -> mergeFromGitHub args prNum owner repo currentBranch localPrResult
    _ -> do
      void $ suspendEffect_ @LogError (Log.ErrorRequest {Log.errorRequestMessage = "MergePR: failed to get repo info or branch for self-merge check", Log.errorRequestFields = ""})
      pure $ Left ("Failed to determine repo/branch info. Cannot verify self-merge guard for PR #" <> T.pack (show prNum) <> ".")

-- | Render a MergePROutput to MCPCallOutput.
mergePRRender :: MergePROutput -> MCPCallOutput
mergePRRender output =
  let nextText =
        if mpoSuccess output
          then "The remote merge is confirmed. The durable post-merge flow owned by the TL controller handles local synchronization and bookkeeping."
          else "The remote merge was not confirmed. Reconcile the PR state before retrying."
   in successResult $
        object
          [ "success" .= mpoSuccess output,
            "message" .= mpoMessage output,
            "git_fetched" .= mpoGitFetched output,
            "next" .= (nextText :: Text)
          ]

-- | Forgejo reviewer readiness.
data Readiness = Ready | NotReady Text

mergeFromLocalPr :: MergePRArgs -> Int -> Text -> Text -> Text -> FPR.LocalPrResponse -> Eff Effects (Either Text MergePROutput)
mergeFromLocalPr args prNum owner repo currentBranch localPr = do
  let headBranch = TL.toStrict (FPR.localPrResponseHeadBranch localPr)
  if headBranch == currentBranch
    then pure $ Left $ "Cannot merge your own PR #" <> T.pack (show prNum) <> ". Your parent agent will merge this PR after reviewing. Call notify_parent instead."
    else mergeFromHostedPr args prNum owner repo currentBranch (Right localPr)

mergeFromGitHub ::
  MergePRArgs ->
  Int ->
  Text ->
  Text ->
  Text ->
  Either EffectError FPR.LocalPrResponse ->
  Eff Effects (Either Text MergePROutput)
mergeFromGitHub args prNum owner repo currentBranch localPrResult = do
  mergeFromHostedPr args prNum owner repo currentBranch localPrResult

mergeFromHostedPr ::
  MergePRArgs ->
  Int ->
  Text ->
  Text ->
  Text ->
  Either EffectError FPR.LocalPrResponse ->
  Eff Effects (Either Text MergePROutput)
mergeFromHostedPr args prNum owner repo currentBranch localPrResult = do
  prResult <-
    suspendEffect @GitHubGetPullRequest
      GH.GetPullRequestRequest
        { GH.getPullRequestRequestOwner = TL.fromStrict owner,
          GH.getPullRequestRequestRepo = TL.fromStrict repo,
          GH.getPullRequestRequestNumber = fromIntegral prNum,
          GH.getPullRequestRequestIncludeReviews = True
        }
  case prResult of
    Left err -> do
      let localDetail = case localPrResult of
            Left localErr -> "local registry lookup failed: " <> T.pack (show localErr)
            Right localPr
              | FPR.localPrResponseFound localPr -> "local registry found PR #" <> T.pack (show prNum) <> " but hosted readiness lookup is authoritative"
              | otherwise -> "local registry has no PR #" <> T.pack (show prNum)
          message =
            "Failed to fetch PR #"
              <> T.pack (show prNum)
              <> " for live hosted readiness check. "
              <> localDetail
              <> "; hosted lookup failed: "
              <> T.pack (show err)
              <> ". If this is a hosted Forgejo flow, set forgejo_url and forgejo_token."
      void $ suspendEffect_ @LogError (Log.ErrorRequest {Log.errorRequestMessage = TL.fromStrict $ "MergePR: " <> message, Log.errorRequestFields = ""})
      pure $ Left message
    Right resp -> do
      let mPr = GH.getPullRequestResponsePullRequest resp
          isSelfMerge = case mPr of
            Just pr -> TL.toStrict (GH.pullRequestHeadRef pr) == currentBranch
            Nothing -> False
      if isSelfMerge
        then pure $ Left $ "Cannot merge your own PR #" <> T.pack (show prNum) <> ". Your parent agent will merge this PR after reviewing. Call notify_parent instead."
        else do
          let readiness = case mprAggregateApproval args of
                Just _ -> Ready
                Nothing -> checkReviewerReadinessFromPR prNum resp
          case readiness of
            NotReady reason -> mergeBlocked reason
            Ready -> do
              watcherResult <-
                suspendEffect @AgentWatcherPrState
                  Agent.WatcherPrStateRequest
                    { Agent.watcherPrStateRequestPrNumber = fromIntegral prNum
                    }
              case watcherResult of
                Left err ->
                  mergeBlocked $
                    "Failed to fetch canonical review/CI evidence for PR #"
                      <> T.pack (show prNum)
                      <> ": "
                      <> T.pack (show err)
                Right watcher ->
                  let watcherReadiness = case mprAggregateApproval args of
                        Just _ -> parentApprovalWatcherMergeGate prNum resp watcher
                        Nothing -> watcherMergeGate prNum resp watcher
                   in case watcherReadiness of
                        Ready -> doMerge args (TL.toStrict (Agent.watcherPrStateResponseHeadSha watcher))
                        NotReady reason -> mergeBlocked reason

mergeBlocked :: Text -> Eff Effects (Either Text MergePROutput)
mergeBlocked reason = do
  void $ suspendEffect_ @LogError (Log.ErrorRequest {Log.errorRequestMessage = TL.fromStrict $ "MergePR: blocked: " <> reason, Log.errorRequestFields = ""})
  pure $ Left reason

watcherMergeGate :: Int -> GH.GetPullRequestResponse -> Agent.WatcherPrStateResponse -> Readiness
watcherMergeGate = watcherMergeGatePolicy False

-- | Parent approval replaces only the aggregate reviewer verdict. All live
-- publication, head, reachability, PR-state, and CI checks remain mandatory.
parentApprovalWatcherMergeGate :: Int -> GH.GetPullRequestResponse -> Agent.WatcherPrStateResponse -> Readiness
parentApprovalWatcherMergeGate = watcherMergeGatePolicy True

watcherMergeGatePolicy :: Bool -> Int -> GH.GetPullRequestResponse -> Agent.WatcherPrStateResponse -> Readiness
watcherMergeGatePolicy allowParentApproval prNum resp watcher =
  let hostedHead = maybe "" (TL.toStrict . GH.pullRequestHeadSha) (GH.getPullRequestResponsePullRequest resp)
      observedHead = TL.toStrict (Agent.watcherPrStateResponseHeadSha watcher)
      reviewState = T.toLower (TL.toStrict (Agent.watcherPrStateResponseReviewState watcher))
      ciStatus = T.toLower (TL.toStrict (Agent.watcherPrStateResponseCiStatus watcher))
      prState = T.toLower (TL.toStrict (Agent.watcherPrStateResponsePrState watcher))
      ownershipVerified = Agent.watcherPrStateResponsePublicationOwnershipVerified watcher
      ownershipError = T.strip (TL.toStrict (Agent.watcherPrStateResponsePublicationOwnershipError watcher))
      headReachable = Agent.watcherPrStateResponseHeadReachable watcher
      evidenceError = T.strip (TL.toStrict (Agent.watcherPrStateResponseEvidenceError watcher))
      prefix = "Canonical merge evidence for PR #" <> T.pack (show prNum) <> ": "
   in case () of
        _
          | not (Agent.watcherPrStateResponseSuccess watcher) ->
              NotReady $ prefix <> TL.toStrict (Agent.watcherPrStateResponseError watcher)
          | not (Agent.watcherPrStateResponseFound watcher) ->
              NotReady $ prefix <> "PR was not found"
          | not ownershipVerified ->
              NotReady $ prefix <> "publication ownership is not verified"
          | not (T.null ownershipError) ->
              NotReady $ prefix <> "publication ownership error: " <> ownershipError
          | not headReachable ->
              NotReady $ prefix <> "published PR head is unreachable"
          | not (T.null evidenceError) ->
              NotReady $ prefix <> "canonical head evidence is invalid: " <> evidenceError
          | T.null observedHead ->
              NotReady $ prefix <> "current PR head SHA is unavailable"
          | Agent.watcherPrStateResponseMerged watcher ->
              NotReady $ prefix <> "PR is already merged"
          | prState /= "open" ->
              NotReady $ prefix <> "PR is not open"
          | observedHead /= hostedHead ->
              NotReady $ prefix <> "hosted PR head SHA changed during the merge check"
          | not allowParentApproval && reviewState /= "approved" ->
              NotReady $ prefix <> "review approval is not recorded for the current PR head"
          | otherwise ->
              if allowParentApproval
                then mergeCiGate prefix ciStatus
                else case authenticatedReviewEvidenceGate prNum observedHead watcher of
                  NotReady reason -> NotReady (prefix <> reason)
                  Ready -> mergeCiGate prefix ciStatus

mergeCiGate :: Text -> Text -> Readiness
mergeCiGate prefix ciStatus
  | ciStatus == "success" || ciStatus == "neutral" = Ready
  | otherwise = NotReady $ prefix <> "CI status for the current PR head is " <> ciStatus

-- | Require the canonical watcher response to carry authenticated, exact-head
-- review evidence before allowing the final merge effect.
authenticatedReviewEvidenceGate :: Int -> Text -> Agent.WatcherPrStateResponse -> Readiness
authenticatedReviewEvidenceGate prNum observedHead watcher =
  let reviewVerdict = T.toLower (T.strip (TL.toStrict (Agent.watcherPrStateResponseReviewVerdict watcher)))
      reviewHeadSha = T.strip (TL.toStrict (Agent.watcherPrStateResponseReviewHeadSha watcher))
      reviewerAgentId = T.strip (TL.toStrict (Agent.watcherPrStateResponseReviewerAgentId watcher))
      identityError = T.strip (TL.toStrict (Agent.watcherPrStateResponseReviewerIdentityError watcher))
      prefix = "review evidence for PR #" <> T.pack (show prNum) <> ": "
   in case () of
        _
          | Agent.watcherPrStateResponseReviewId watcher <= 0 ->
              NotReady $ prefix <> "Forgejo review ID is missing"
          | reviewVerdict /= "approved" ->
              NotReady $ prefix <> "authenticated review verdict is not approved"
          | T.null reviewHeadSha ->
              NotReady $ prefix <> "authenticated review head SHA is missing"
          | reviewHeadSha /= observedHead ->
              NotReady $ prefix <> "authenticated review is not for the current PR head"
          | not (T.null identityError) ->
              NotReady $ prefix <> "reviewer identity could not be authenticated: " <> identityError
          | T.null reviewerAgentId ->
              NotReady $ prefix <> "authenticated reviewer identity is missing"
          | otherwise -> Ready

-- | Check Forgejo reviewer readiness from an already-fetched PR response.
checkReviewerReadinessFromPR :: Int -> GH.GetPullRequestResponse -> Readiness
checkReviewerReadinessFromPR prNum resp =
  let reviews = V.toList (GH.getPullRequestResponseReviews resp)
      pr = GH.getPullRequestResponsePullRequest resp
      headSha = case pr of
        Just p -> TL.toStrict (GH.pullRequestHeadSha p)
        Nothing -> ""
      reviewerReviews = reviews
   in if T.null headSha
        then
          NotReady $
            "Current PR head SHA is unavailable for PR #"
              <> T.pack (show prNum)
        else case reverse reviewerReviews of
          [] ->
            NotReady $
              "No Forgejo reviewer response yet on PR #"
                <> T.pack (show prNum)
                <> ". Wait for [PR READY] or [REVIEW TIMEOUT] from the event system."
          (latest : _) ->
            let reviewSha = TL.toStrict (GH.reviewCommitId latest)
                state = GH.reviewState latest
             in case state of
                  Protobuf.Enumerated (Right GH.ReviewStateREVIEW_STATE_APPROVED) ->
                    if headSha == reviewSha && not (T.null reviewSha)
                      then Ready
                      else
                        NotReady $
                          "Forgejo approval for PR #"
                            <> T.pack (show prNum)
                            <> " is stale; wait for approval of the current head before merging."
                  Protobuf.Enumerated (Right GH.ReviewStateREVIEW_STATE_CHANGES_REQUESTED) ->
                    NotReady $
                      "Forgejo reviewer requested changes on PR #"
                        <> T.pack (show prNum)
                        <> ". Wait for a new review of the current head before merging."
                  Protobuf.Enumerated (Right GH.ReviewStateREVIEW_STATE_COMMENTED) ->
                    NotReady $
                      "Forgejo reviewer commented on PR #"
                        <> T.pack (show prNum)
                        <> ". Wait for a new review of the current head before merging."
                  _ ->
                    NotReady $
                      "Forgejo reviewer has not approved the current head of PR #"
                        <> T.pack (show prNum)
                        <> "."

-- | Extract the agent name (last dot-segment) from a branch name.
-- After the unified namespace change, the last segment IS the agent name (suffixed).
extractAgentName :: Text -> Maybe Text
extractAgentName branch
  | T.null branch = Nothing
  | otherwise = case reverse (T.splitOn "." branch) of
      [] -> Nothing
      (slug : _) -> Just slug

-- | The head observed by the canonical watcher is mandatory compare-and-swap
-- evidence. The caller's optional head is retained for request compatibility,
-- but can never weaken the final merge binding.
mergeExpectedHeadSha :: MergePRArgs -> Text -> Text
mergeExpectedHeadSha _ observedHead = observedHead

-- | Execute the actual merge after readiness check passes.
doMerge :: MergePRArgs -> Text -> Eff Effects (Either Text MergePROutput)
doMerge = runMerge

runMerge :: MergePRArgs -> Text -> Eff Effects (Either Text MergePROutput)
runMerge args observedHead = do
  let req =
        MP.MergePrRequest
          { MP.mergePrRequestPrNumber = fromIntegral (mprPrNumber args),
            MP.mergePrRequestStrategy = maybe "" TL.fromStrict (mprStrategy args),
            MP.mergePrRequestWorkingDir = maybe "" TL.fromStrict (mprWorkingDir args),
            MP.mergePrRequestExpectedBaseSha = maybe "" TL.fromStrict (mprExpectedBaseSha args),
            MP.mergePrRequestExpectedHeadSha = TL.fromStrict (mergeExpectedHeadSha args observedHead),
            MP.mergePrRequestExpectedPatchDigest = maybe "" TL.fromStrict (mprExpectedPatchDigest args),
            MP.mergePrRequestExpectedMergeTreeSha = maybe "" TL.fromStrict (mprExpectedMergeTreeSha args),
            MP.mergePrRequestAggregateApproval = fmap toProtoAggregateApproval (mprAggregateApproval args)
          }
  result <- suspendEffect @MergePRMergePr req
  case result of
    Left err -> do
      let failureMessage = TL.fromStrict $ "MergePR: failed: " <> T.pack (show err)
      void $
        suspendEffect_ @LogError
          ( Log.ErrorRequest
              { Log.errorRequestMessage = failureMessage,
                Log.errorRequestFields = ""
              }
          )
      pure $ Left (T.pack (show err))
    Right resp -> do
      let branchName = TL.toStrict (MP.mergePrResponseBranchName resp)
          mergeSuccess = MP.mergePrResponseSuccess resp
          mergeMsg = TL.toStrict (MP.mergePrResponseMessage resp)
          gitFetched = MP.mergePrResponseGitFetched resp
          headSha = TL.toStrict (MP.mergePrResponseHeadSha resp)
          headShaValue = if T.null headSha then Nothing else Just headSha
          headShaFinding =
            if T.null headSha
              then Just ("not_available_without_verified_pr_context" :: Text)
              else Nothing

      void $ suspendEffect_ @LogInfo (Log.InfoRequest {Log.infoRequestMessage = TL.fromStrict $ "MergePR: " <> mergeMsg, Log.infoRequestFields = ""})

      if mergeSuccess
        then do
          let eventPayload =
                BSL.toStrict $
                  Aeson.encode $
                    object
                      [ "pr_number" .= mprPrNumber args,
                        "success" .= True,
                        "head_sha" .= headShaValue,
                        "head_sha_finding" .= headShaFinding
                      ]
          void $
            suspendEffect_ @LogEmitEvent
              ( Log.EmitEventRequest
                  { Log.emitEventRequestEventType = "pr.merged",
                    Log.emitEventRequestPayload = eventPayload,
                    Log.emitEventRequestTimestamp = 0
                  }
              )
          cleanupMergedAgent branchName
        else pure ()

      pure $
        Right $
          MergePROutput
            { mpoSuccess = mergeSuccess,
              mpoMessage = mergeMsg,
              mpoGitFetched = gitFetched,
              mpoBranchName = branchName
            }

cleanupMergedAgent :: Text -> Eff Effects ()
cleanupMergedAgent branchName =
  case extractAgentName branchName of
    Nothing -> pure ()
    Just slug -> do
      let cleanupReq =
            Agent.CleanupRequest
              { Agent.cleanupRequestIssue = TL.fromStrict slug,
                Agent.cleanupRequestForce = True,
                Agent.cleanupRequestSubrepo = ""
              }
      cleanupResult <- suspendEffect @AgentCleanup cleanupReq
      case cleanupResult of
        Left cleanupErr ->
          logCleanupMessage $ "cleanup failed (non-fatal): " <> T.pack (show cleanupErr)
        Right _ -> logCleanupMessage $ "cleaned up agent " <> slug

logCleanupMessage :: Text -> Eff Effects ()
logCleanupMessage message =
  void $
    suspendEffect_ @LogInfo
      ( Log.InfoRequest
          { Log.infoRequestMessage = TL.fromStrict $ "MergePR: " <> message,
            Log.infoRequestFields = ""
          }
      )

toProtoAggregateApproval :: AggregateApprovalRef -> MP.AggregateApprovalReference
toProtoAggregateApproval reference =
  MP.AggregateApprovalReference
    { MP.aggregateApprovalReferenceReceiptId = TL.fromStrict (aarReceiptId reference),
      MP.aggregateApprovalReferenceParentRunId = TL.fromStrict (aarParentRunId reference),
      MP.aggregateApprovalReferenceParentScopePath = V.map TL.fromStrict (aarParentScopePath reference),
      MP.aggregateApprovalReferenceCandidateKey = TL.fromStrict (aarCandidateKey reference),
      MP.aggregateApprovalReferenceControllerEpoch = TL.fromStrict (aarControllerEpoch reference)
    }
