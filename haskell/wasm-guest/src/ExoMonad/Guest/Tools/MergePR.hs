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
    authenticatedReviewEvidenceGate,
  )
where

import Control.Monad (void, when)
import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON, object, withObject, (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.ByteString qualified as BS
import Data.ByteString.Lazy qualified as BSL
import Data.Map qualified as Map
import Data.Maybe (fromMaybe)
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Encoding qualified as TE
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Agent qualified as Agent
import Effects.EffectError (EffectError)
import Effects.FilePr qualified as FPR
import Effects.Git qualified as Git
import Effects.Github qualified as GH
import Effects.Log qualified as Log
import Effects.MergePr qualified as MP
import Effects.Process qualified as Proc
import ExoMonad.Effects.Agent (AgentCleanup, AgentWatcherPrState)
import ExoMonad.Effects.FilePR (FilePRLocalPrGet)
import ExoMonad.Effects.Git (GitGetBranch, GitGetRepoInfo)
import ExoMonad.Effects.GitHub (GitHubGetPullRequest)
import ExoMonad.Effects.Log (LogEmitEvent, LogError, LogInfo)
import ExoMonad.Effects.MergePR (MergePRMergePr)
import ExoMonad.Effects.Process (ProcessRun)
import ExoMonad.Guest.Tool.Class (Effects, MCPCallOutput, MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
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
    mprExpectedMergeTreeSha :: Maybe Text
  }
  deriving (Show, Eq, Generic)

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

data MergePROutput = MergePROutput
  { mpoSuccess :: Bool,
    mpoMessage :: Text,
    mpoGitFetched :: Bool,
    mpoBranchName :: Text,
    mpoPullOk :: Bool,
    mpoPullFailure :: Maybe Text
  }
  deriving (Show, Eq, Generic)

instance Aeson.ToJSON MergePROutput where
  toJSON (MergePROutput s m g b p pf) =
    object
      [ "success" .= s,
        "message" .= m,
        "git_fetched" .= g,
        "branch_name" .= b,
        "pull_ok" .= p,
        "pull_failure" .= pf
      ]

-- | Shared tool description for merge_pr.
mergePRDescription :: Text
mergePRDescription = "Merge a GitHub pull request and fetch changes. Before merging, requires a Forgejo reviewer approval for the exact current PR head and passing CI (success or neutral) for that same head; stale approvals, review changes/comments, pending/failed/unknown CI, and missing evidence are rejected. Pass chainlink_issue_id to close the issue and commit CHANGELOG.md before the merge. After merging, verify the build — especially when merging multiple PRs in parallel, as changes may interact."

-- | Shared tool schema for merge_pr.
mergePRSchema :: Aeson.Object
mergePRSchema =
  genericToolSchemaWith @MergePRArgs
    [ ("pr_number", "PR number to merge"),
      ("strategy", "Merge strategy: squash (default), merge, or rebase"),
      ("working_dir", "Working directory for git operations"),
      ("chainlink_issue_id", "Optional Chainlink issue ID to close and commit CHANGELOG.md before merging")
    ]

-- | Core merge_pr I/O: self-merge guard + readiness check + merge + cleanup + git pull.
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
        if mpoPullOk output
          then "Verify build: cargo check --workspace. Especially important after parallel merges — changes may interact."
          else pullFailureNext output
   in successResult $
        object
          [ "success" .= mpoSuccess output,
            "message" .= mpoMessage output,
            "git_fetched" .= mpoGitFetched output,
            "next" .= (nextText :: Text)
          ]

-- | Forgejo reviewer readiness.
data Readiness = Ready | NotReady Text

data PullOutcome = PullOutcome
  { pullOutcomeOk :: Bool,
    pullOutcomeFailure :: Maybe Text
  }

pullFailureNext :: MergePROutput -> Text
pullFailureNext output =
  fromMaybe "git pull failed" (mpoPullFailure output)
    <> ". Run 'git pull --rebase' manually to sync your local branch. Then verify build: cargo check --workspace."

pullFailureSummary :: Int -> Text -> Text -> Text
pullFailureSummary exitCode stdout stderr =
  commandFailureSummary "git pull" exitCode stdout stderr

commandFailureSummary :: Text -> Int -> Text -> Text -> Text
commandFailureSummary label exitCode stdout stderr =
  label
    <> " failed (exit code "
    <> T.pack (show exitCode)
    <> "): "
    <> firstDiagnosticLine stderr stdout

firstDiagnosticLine :: Text -> Text -> Text
firstDiagnosticLine stderr stdout =
  let candidates = filter (not . T.null) (map T.strip (T.lines stderr <> T.lines stdout))
   in truncateText 240 (fromMaybe "no stderr or stdout captured" (safeHead candidates))

safeHead :: [a] -> Maybe a
safeHead [] = Nothing
safeHead (x : _) = Just x

truncateText :: Int -> Text -> Text
truncateText limit value =
  if T.length value <= limit
    then value
    else T.take limit value <> "..."

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
          let readiness = checkReviewerReadinessFromPR prNum resp
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
                  case watcherMergeGate prNum resp watcher of
                    Ready -> doMerge args
                    NotReady reason -> mergeBlocked reason

mergeBlocked :: Text -> Eff Effects (Either Text MergePROutput)
mergeBlocked reason = do
  void $ suspendEffect_ @LogError (Log.ErrorRequest {Log.errorRequestMessage = TL.fromStrict $ "MergePR: blocked: " <> reason, Log.errorRequestFields = ""})
  pure $ Left reason

watcherMergeGate :: Int -> GH.GetPullRequestResponse -> Agent.WatcherPrStateResponse -> Readiness
watcherMergeGate prNum resp watcher =
  let hostedHead = maybe "" (TL.toStrict . GH.pullRequestHeadSha) (GH.getPullRequestResponsePullRequest resp)
      observedHead = TL.toStrict (Agent.watcherPrStateResponseHeadSha watcher)
      reviewState = T.toLower (TL.toStrict (Agent.watcherPrStateResponseReviewState watcher))
      ciStatus = T.toLower (TL.toStrict (Agent.watcherPrStateResponseCiStatus watcher))
      prState = T.toLower (TL.toStrict (Agent.watcherPrStateResponsePrState watcher))
      prefix = "Canonical merge evidence for PR #" <> T.pack (show prNum) <> ": "
   in case () of
        _
          | not (Agent.watcherPrStateResponseSuccess watcher) ->
              NotReady $ prefix <> TL.toStrict (Agent.watcherPrStateResponseError watcher)
          | not (Agent.watcherPrStateResponseFound watcher) ->
              NotReady $ prefix <> "PR was not found"
          | T.null observedHead ->
              NotReady $ prefix <> "current PR head SHA is unavailable"
          | Agent.watcherPrStateResponseMerged watcher ->
              NotReady $ prefix <> "PR is already merged"
          | prState /= "open" ->
              NotReady $ prefix <> "PR is not open"
          | observedHead /= hostedHead ->
              NotReady $ prefix <> "hosted PR head SHA changed during the merge check"
          | reviewState /= "approved" ->
              NotReady $ prefix <> "review approval is not recorded for the current PR head"
          | otherwise ->
              case authenticatedReviewEvidenceGate prNum observedHead watcher of
                NotReady reason -> NotReady (prefix <> reason)
                Ready
                  | ciStatus == "success" || ciStatus == "neutral" -> Ready
                  | otherwise ->
                      NotReady $ prefix <> "CI status for the current PR head is " <> ciStatus

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

chainlinkChangelogCommitMessage :: Int -> Text
chainlinkChangelogCommitMessage issueId =
  "Update changelog for Chainlink issue #" <> T.pack (show issueId)

mergePRWorkingDir :: MergePRArgs -> Text
mergePRWorkingDir args =
  fromMaybe "." (mprWorkingDir args)

closeIssueAndCommitChangelog :: MergePRArgs -> Eff Effects (Either Text ())
closeIssueAndCommitChangelog args =
  case mprChainlinkIssueId args of
    Nothing -> pure $ Right ()
    Just issueId -> do
      let workingDir = mergePRWorkingDir args
      closeResult <-
        runPreMergeCommand
          workingDir
          "close Chainlink issue"
          "chainlink"
          ["close", T.pack (show issueId), "-q"]
      case closeResult of
        Left err -> pure $ Left err
        Right () -> do
          addResult <-
            runPreMergeCommand
              workingDir
              "stage CHANGELOG.md"
              "git"
              ["add", "CHANGELOG.md"]
          case addResult of
            Left err -> pure $ Left err
            Right () ->
              runPreMergeCommand
                workingDir
                "commit CHANGELOG.md"
                "git"
                ["commit", "-m", chainlinkChangelogCommitMessage issueId]

runPreMergeCommand :: Text -> Text -> Text -> [Text] -> Eff Effects (Either Text ())
runPreMergeCommand workingDir label command args = do
  void $
    suspendEffect_ @LogInfo
      ( Log.InfoRequest
          { Log.infoRequestMessage = TL.fromStrict $ "MergePR: " <> label,
            Log.infoRequestFields = ""
          }
      )
  result <-
    suspendEffect @ProcessRun
      Proc.RunRequest
        { Proc.runRequestCommand = TL.fromStrict command,
          Proc.runRequestArgs = V.fromList (map TL.fromStrict args),
          Proc.runRequestWorkingDir = TL.fromStrict workingDir,
          Proc.runRequestEnv = Map.empty,
          Proc.runRequestTimeoutMs = 30000
        }
  case result of
    Left err -> do
      let message = label <> " failed before exit code was captured: " <> T.pack (show err)
      logPreMergeFailure message ""
      pure $ Left message
    Right resp
      | Proc.runResponseExitCode resp == 0 -> pure $ Right ()
      | otherwise -> do
          let exitCode = fromIntegral (Proc.runResponseExitCode resp)
              stdoutText = TL.toStrict (Proc.runResponseStdout resp)
              stderrText = TL.toStrict (Proc.runResponseStderr resp)
              message = commandFailureSummary label exitCode stdoutText stderrText
              fields =
                TE.encodeUtf8 $
                  "exit_code="
                    <> T.pack (show exitCode)
                    <> "\nstdout:\n"
                    <> stdoutText
                    <> "\nstderr:\n"
                    <> stderrText
          logPreMergeFailure message fields
          pure $ Left message

logPreMergeFailure :: Text -> BS.ByteString -> Eff Effects ()
logPreMergeFailure message fields =
  void $
    suspendEffect_ @LogError
      ( Log.ErrorRequest
          { Log.errorRequestMessage = TL.fromStrict $ "MergePR: " <> message,
            Log.errorRequestFields = fields
          }
      )

-- | Execute the actual merge after readiness check passes.
doMerge :: MergePRArgs -> Eff Effects (Either Text MergePROutput)
doMerge args = do
  preMergeResult <- closeIssueAndCommitChangelog args
  case preMergeResult of
    Left err -> pure $ Left err
    Right () -> runMerge args

runMerge :: MergePRArgs -> Eff Effects (Either Text MergePROutput)
runMerge args = do
  let req =
        MP.MergePrRequest
          { MP.mergePrRequestPrNumber = fromIntegral (mprPrNumber args),
            MP.mergePrRequestStrategy = maybe "" TL.fromStrict (mprStrategy args),
            MP.mergePrRequestWorkingDir = maybe "" TL.fromStrict (mprWorkingDir args),
            MP.mergePrRequestExpectedBaseSha = maybe "" TL.fromStrict (mprExpectedBaseSha args),
            MP.mergePrRequestExpectedHeadSha = maybe "" TL.fromStrict (mprExpectedHeadSha args),
            MP.mergePrRequestExpectedPatchDigest = maybe "" TL.fromStrict (mprExpectedPatchDigest args),
            MP.mergePrRequestExpectedMergeTreeSha = maybe "" TL.fromStrict (mprExpectedMergeTreeSha args)
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

      pullOutcome <-
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

            -- Fast-forward local branch after merge
            pullOutcomeStatus <- do
              let pullReq =
                    Proc.RunRequest
                      { Proc.runRequestCommand = "git",
                        Proc.runRequestArgs = V.fromList ["pull"],
                        Proc.runRequestWorkingDir = maybe "" TL.fromStrict (mprWorkingDir args),
                        Proc.runRequestEnv = Map.empty,
                        Proc.runRequestTimeoutMs = 30000
                      }
              pullResult <- suspendEffect @ProcessRun pullReq
              case pullResult of
                Left err -> do
                  let failure = "git pull failed before exit code was captured: " <> T.pack (show err)
                  void $
                    suspendEffect_ @LogError
                      ( Log.ErrorRequest
                          { Log.errorRequestMessage = TL.fromStrict $ "MergePR: " <> failure,
                            Log.errorRequestFields = ""
                          }
                      )
                  pure PullOutcome {pullOutcomeOk = False, pullOutcomeFailure = Just failure}
                Right pullResp
                  | Proc.runResponseExitCode pullResp == 0 ->
                      pure PullOutcome {pullOutcomeOk = True, pullOutcomeFailure = Nothing}
                  | otherwise -> do
                      let exitCode = fromIntegral (Proc.runResponseExitCode pullResp)
                          stdoutText = TL.toStrict (Proc.runResponseStdout pullResp)
                          stderrText = TL.toStrict (Proc.runResponseStderr pullResp)
                          failure = pullFailureSummary exitCode stdoutText stderrText
                          fields =
                            TE.encodeUtf8 $
                              "exit_code="
                                <> T.pack (show exitCode)
                                <> "\nstdout:\n"
                                <> stdoutText
                                <> "\nstderr:\n"
                                <> stderrText
                      void $
                        suspendEffect_ @LogError
                          ( Log.ErrorRequest
                              { Log.errorRequestMessage = TL.fromStrict $ "MergePR: " <> failure,
                                Log.errorRequestFields = fields
                              }
                          )
                      pure PullOutcome {pullOutcomeOk = False, pullOutcomeFailure = Just failure}

            -- Auto-cleanup: close agent tab, remove worktree, unregister
            case extractAgentName branchName of
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
                    void $
                      suspendEffect_ @LogInfo
                        ( Log.InfoRequest
                            { Log.infoRequestMessage = TL.fromStrict $ "MergePR: cleanup failed (non-fatal): " <> T.pack (show cleanupErr),
                              Log.infoRequestFields = ""
                            }
                        )
                  Right _ ->
                    void $
                      suspendEffect_ @LogInfo
                        ( Log.InfoRequest
                            { Log.infoRequestMessage = TL.fromStrict $ "MergePR: cleaned up agent " <> slug,
                              Log.infoRequestFields = ""
                            }
                        )
              Nothing -> pure ()

            pure pullOutcomeStatus
          else pure PullOutcome {pullOutcomeOk = True, pullOutcomeFailure = Nothing}

      pure $
        Right $
          MergePROutput
            { mpoSuccess = mergeSuccess,
              mpoMessage = mergeMsg,
              mpoGitFetched = gitFetched,
              mpoBranchName = branchName,
              mpoPullOk = pullOutcomeOk pullOutcome,
              mpoPullFailure = pullOutcomeFailure pullOutcome
            }
