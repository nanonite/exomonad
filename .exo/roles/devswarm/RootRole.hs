{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}
{-# LANGUAGE TypeOperators #-}

-- | Root TL role: orchestration-only. Lifecycle convergence is owned by the
--   controller; shutdown remains an operator/server path.
--   Used for the root human-facing TL window (exomonad init).
module RootRole (config, Tools) where

import Control.Monad (void, when)
import Data.Text (Text)
import ExoMonad
import ExoMonad.Guest.Effects.AgentControl (SpawnResult (..))
import ExoMonad.Guest.Effects.StopHook (getCurrentBranch)
import ExoMonad.Guest.ReviewHandoff (reviewHandoffInstructions)
import ExoMonad.Guest.StateMachine (applyEvent)
import ExoMonad.Guest.Tools.Agents (ListAgents (..))
import ExoMonad.Guest.Tools.Chainlink
  ( ChainlinkBlock (..),
    ChainlinkCascade (..),
    ChainlinkIssueClose (..),
    ChainlinkIssueComment (..),
    ChainlinkIssueCreate (..),
    ChainlinkIssueList (..),
    ChainlinkIssueShow (..),
    ChainlinkIssueUpdate (..),
    ChainlinkMilestoneCreate (..),
    ChainlinkMilestoneList (..),
    ChainlinkRelate (..),
    ChainlinkSessionEnd (..),
    ChainlinkSessionStart (..),
    ChainlinkSessionStatus (..),
    ChainlinkSessionWork (..),
    ChainlinkSubissueCreate (..),
    ChainlinkTimerStart (..),
    ChainlinkTimerStatus (..),
    ChainlinkTimerStop (..),
  )
import ExoMonad.Guest.Tools.Cleanup (Cleanup (..))
import ExoMonad.Guest.Tools.CleanupLeaf (CleanupLeaf (..))
import ExoMonad.Guest.Tools.CleanupOrphan (CleanupOrphan (..))
import ExoMonad.Guest.Tools.CleanupReviewerLeaf (CleanupReviewerLeaf (..))
import ExoMonad.Guest.Tools.CloseIssueAndCleanup (CloseIssueAndCleanup (..))
import ExoMonad.Guest.Tools.CloseReviewerWindow (CloseReviewerWindow (..))
import ExoMonad.Guest.Tools.DisposeLeaf (DisposeLeaf (..))
import ExoMonad.Guest.Tools.Memory (ContinuationBrief (..), MemoryAppend (..), MemoryList (..))
import ExoMonad.Guest.Tools.MergePR (MergePRArgs (..), MergePROutput (..), extractAgentName, mergePRCore, mergePRDescription, mergePRRender, mergePRSchema)
import ExoMonad.Guest.Tools.PollWorkers (PollWorkers (..))
import ExoMonad.Guest.Tools.PostMergeRecovery
  ( PostMergeChangelog,
    PostMergeParentSync,
    PostMergePush,
    PostMergeRemoteReconcile,
  )
import ExoMonad.Guest.Tools.ReplaceClosedPr (ReplaceClosedPr (..))
import ExoMonad.Guest.Tools.RestartReview (RestartReview (..))
import ExoMonad.Guest.Tools.ResumeBlockedLeaf (ResumeBlockedLeaf (..))
import ExoMonad.Guest.Tools.ResumePr (ResumePr (..))
import ExoMonad.Guest.Tools.RootBranchFinalize (RootBranchFinalize)
import ExoMonad.Guest.Tools.SessionStatus (SessionStatus (..))
import ExoMonad.Guest.Tools.Spawn
  ( CloseWorkerPaneArgs,
    SpawnLeafArgs (..),
    SpawnLeafSubtreeArgs,
    SpawnWorkerToolArgs,
    closeWorkerPaneCore,
    closeWorkerPaneDescription,
    closeWorkerPaneSchema,
    spawnLeafCore,
    spawnLeafDescription,
    spawnLeafRender,
    spawnLeafSchema,
    spawnWorkerToolCore,
    spawnWorkerToolDescription,
    spawnWorkerToolSchema,
  )
import ExoMonad.Guest.Tools.SpawnCodex (SpawnCodex, handleSpawnCodex, spawnCodexDescription, spawnCodexSchema)
import ExoMonad.Guest.Tools.SpawnReviewer (SpawnReviewer (..))
import ExoMonad.Guest.Tools.WatcherPrState (WatcherPrState (..))
import ExoMonad.Guest.Types (AfterModelOutput (..), BeforeModelOutput (..), allowResponse, allowStopResponse)
import ExoMonad.Types (Effects, HookConfig (..), teamRegistrationPostToolUse, tlSessionStartHook)
import HookPolicy (preToolUseWithImplementationBlock)
import PRReviewHandler (tlPRReviewEventHandlers)
import TLPhase (ChildHandle (..), TLEvent (..), TLPhase (..))

rootRedispatchMessage :: Text -> Text
rootRedispatchMessage toolName =
  "TL agents cannot use "
    <> toolName
    <> ". The TL plans and dispatches; implementation belongs to leaves and workers.\n"
    <> "Reviewer comments are delivered to the TL; they are not auto-applied to a leaf. Read the review, analyze the root cause, and prepare the repair handoff before steering the owner.\n"
    <> reviewHandoffInstructions
    <> "\nIf a worker is blocked, use send_tmux_message to inject a clarification into the worker's pane. See Worker Correction Loop in .exo/roles/devswarm/context/root.md.\n"
    <> "If neither path fits, re-decompose with spawn_leaf or spawn_worker.\n"
    <> "See CLAUDE.md § Tech Lead Praxis for the full protocol."

data RootSpawnLeaf

instance MCPTool RootSpawnLeaf where
  type ToolArgs RootSpawnLeaf = SpawnLeafArgs
  toolName = "spawn_leaf"
  toolDescription = spawnLeafDescription
  toolSchema = spawnLeafSchema
  toolHandlerEff args = do
    result <- spawnLeafCore args
    case result of
      Left err -> pure $ errorResult err
      Right (slug, sr) -> do
        let handle = ChildHandle {chSlug = slug, chBranch = branchName sr, chAgentType = agentTypeResult sr}
        branch <- getCurrentBranch
        void $ applyEvent @TLPhase @TLEvent branch TLPlanning (ChildSpawned handle)
        pure $ spawnLeafRender (Right (slug, sr))

data RootSpawnWorker

instance MCPTool RootSpawnWorker where
  type ToolArgs RootSpawnWorker = SpawnWorkerToolArgs
  toolName = "spawn_worker"
  toolDescription = spawnWorkerToolDescription
  toolSchema = spawnWorkerToolSchema
  toolHandlerEff args = spawnWorkerToolCore args

data RootCloseWorkerPane

instance MCPTool RootCloseWorkerPane where
  type ToolArgs RootCloseWorkerPane = CloseWorkerPaneArgs
  toolName = "close_worker_pane"
  toolDescription = closeWorkerPaneDescription
  toolSchema = closeWorkerPaneSchema
  toolHandlerEff args = closeWorkerPaneCore args

data RootSpawnCodex

instance MCPTool RootSpawnCodex where
  type ToolArgs RootSpawnCodex = SpawnLeafSubtreeArgs
  toolName = "spawn_codex"
  toolDescription = spawnCodexDescription
  toolSchema = spawnCodexSchema
  toolHandlerEff args = do
    result <- handleSpawnCodex args
    case result of
      Left err -> pure $ errorResult err
      Right (slug, sr) -> do
        let handle = ChildHandle {chSlug = slug, chBranch = branchName sr, chAgentType = agentTypeResult sr}
        branch <- getCurrentBranch
        void $ applyEvent @TLPhase @TLEvent branch TLPlanning (ChildSpawned handle)
        pure $ spawnLeafRender (Right (slug, sr))

data RootMergePR

instance MCPTool RootMergePR where
  type ToolArgs RootMergePR = MergePRArgs
  toolName = "merge_pr"
  toolDescription = mergePRDescription
  toolSchema = mergePRSchema
  toolHandlerEff args = do
    result <- mergePRCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> do
        when (mpoSuccess output) $ do
          case extractAgentName (mpoBranchName output) of
            Just slug -> do
              branch <- getCurrentBranch
              void $ applyEvent @TLPhase @TLEvent branch TLPlanning (ChildCompleted slug)
            Nothing -> pure ()
        pure $ mergePRRender output

data Tools mode = Tools
  { spawnLeaf :: mode :- RootSpawnLeaf,
    spawnWorker :: mode :- RootSpawnWorker,
    spawnReviewer :: mode :- SpawnReviewer,
    cleanupReviewerLeaf :: mode :- CleanupReviewerLeaf,
    closeReviewerWindow :: mode :- CloseReviewerWindow,
    restartReview :: mode :- RestartReview,
    replaceClosedPr :: mode :- ReplaceClosedPr,
    resumePr :: mode :- ResumePr,
    resumeBlockedLeaf :: mode :- ResumeBlockedLeaf,
    disposeLeaf :: mode :- DisposeLeaf,
    watcherPrState :: mode :- WatcherPrState,
    postMergeParentSync :: mode :- PostMergeParentSync,
    postMergeRemoteReconcile :: mode :- PostMergeRemoteReconcile,
    postMergeChangelog :: mode :- PostMergeChangelog,
    postMergePush :: mode :- PostMergePush,
    rootBranchFinalize :: mode :- RootBranchFinalize,
    closeWorkerPane :: mode :- RootCloseWorkerPane,
    spawnCodex :: mode :- RootSpawnCodex,
    sessionStatus :: mode :- SessionStatus,
    pollWorkers :: mode :- PollWorkers,
    memoryAppend :: mode :- MemoryAppend,
    memoryList :: mode :- MemoryList,
    continuationBrief :: mode :- ContinuationBrief,
    listAgents :: mode :- ListAgents,
    mergePr :: mode :- RootMergePR,
    sendTmuxMessage :: mode :- SendTmuxMessage,
    sendMailboxMessage :: mode :- SendMailboxMessage,
    chainlinkIssueCreate :: mode :- ChainlinkIssueCreate,
    chainlinkSessionStart :: mode :- ChainlinkSessionStart,
    chainlinkSessionStatus :: mode :- ChainlinkSessionStatus,
    chainlinkIssueShow :: mode :- ChainlinkIssueShow,
    chainlinkIssueComment :: mode :- ChainlinkIssueComment,
    chainlinkSubissueCreate :: mode :- ChainlinkSubissueCreate,
    chainlinkSessionWork :: mode :- ChainlinkSessionWork,
    chainlinkSessionEnd :: mode :- ChainlinkSessionEnd,
    chainlinkIssueClose :: mode :- ChainlinkIssueClose,
    closeIssueAndCleanup :: mode :- CloseIssueAndCleanup,
    cleanupOrphan :: mode :- CleanupOrphan,
    cleanupLeaf :: mode :- CleanupLeaf,
    cleanup :: mode :- Cleanup,
    chainlinkTimerStart :: mode :- ChainlinkTimerStart,
    chainlinkTimerStop :: mode :- ChainlinkTimerStop,
    chainlinkTimerStatus :: mode :- ChainlinkTimerStatus,
    chainlinkIssueList :: mode :- ChainlinkIssueList,
    chainlinkIssueUpdate :: mode :- ChainlinkIssueUpdate,
    chainlinkIssueBlock :: mode :- ChainlinkBlock,
    chainlinkIssueRelate :: mode :- ChainlinkRelate,
    chainlinkIssueCascade :: mode :- ChainlinkCascade,
    chainlinkMilestoneCreate :: mode :- ChainlinkMilestoneCreate,
    chainlinkMilestoneList :: mode :- ChainlinkMilestoneList
  }
  deriving (Generic)

config :: RoleConfig (Tools AsHandler)
config =
  RoleConfig
    { roleName = "root",
      tools =
        Tools
          { spawnLeaf = mkHandler @RootSpawnLeaf,
            spawnWorker = mkHandler @RootSpawnWorker,
            spawnReviewer = mkHandler @SpawnReviewer,
            cleanupReviewerLeaf = mkHandler @CleanupReviewerLeaf,
            closeReviewerWindow = mkHandler @CloseReviewerWindow,
            restartReview = mkHandler @RestartReview,
            replaceClosedPr = mkHandler @ReplaceClosedPr,
            resumePr = mkHandler @ResumePr,
            resumeBlockedLeaf = mkHandler @ResumeBlockedLeaf,
            disposeLeaf = mkHandler @DisposeLeaf,
            watcherPrState = mkHandler @WatcherPrState,
            postMergeParentSync = mkHandler @PostMergeParentSync,
            postMergeRemoteReconcile = mkHandler @PostMergeRemoteReconcile,
            postMergeChangelog = mkHandler @PostMergeChangelog,
            postMergePush = mkHandler @PostMergePush,
            rootBranchFinalize = mkHandler @RootBranchFinalize,
            closeWorkerPane = mkHandler @RootCloseWorkerPane,
            spawnCodex = mkHandler @RootSpawnCodex,
            sessionStatus = mkHandler @SessionStatus,
            pollWorkers = mkHandler @PollWorkers,
            memoryAppend = mkHandler @MemoryAppend,
            memoryList = mkHandler @MemoryList,
            continuationBrief = mkHandler @ContinuationBrief,
            listAgents = mkHandler @ListAgents,
            mergePr = mkHandler @RootMergePR,
            sendTmuxMessage = mkHandler @SendTmuxMessage,
            sendMailboxMessage = mkHandler @SendMailboxMessage,
            chainlinkIssueCreate = mkHandler @ChainlinkIssueCreate,
            chainlinkSessionStart = mkHandler @ChainlinkSessionStart,
            chainlinkSessionStatus = mkHandler @ChainlinkSessionStatus,
            chainlinkIssueShow = mkHandler @ChainlinkIssueShow,
            chainlinkIssueComment = mkHandler @ChainlinkIssueComment,
            chainlinkSubissueCreate = mkHandler @ChainlinkSubissueCreate,
            chainlinkSessionWork = mkHandler @ChainlinkSessionWork,
            chainlinkSessionEnd = mkHandler @ChainlinkSessionEnd,
            chainlinkIssueClose = mkHandler @ChainlinkIssueClose,
            closeIssueAndCleanup = mkHandler @CloseIssueAndCleanup,
            cleanupOrphan = mkHandler @CleanupOrphan,
            cleanupLeaf = mkHandler @CleanupLeaf,
            cleanup = mkHandler @Cleanup,
            chainlinkTimerStart = mkHandler @ChainlinkTimerStart,
            chainlinkTimerStop = mkHandler @ChainlinkTimerStop,
            chainlinkTimerStatus = mkHandler @ChainlinkTimerStatus,
            chainlinkIssueList = mkHandler @ChainlinkIssueList,
            chainlinkIssueUpdate = mkHandler @ChainlinkIssueUpdate,
            chainlinkIssueBlock = mkHandler @ChainlinkBlock,
            chainlinkIssueRelate = mkHandler @ChainlinkRelate,
            chainlinkIssueCascade = mkHandler @ChainlinkCascade,
            chainlinkMilestoneCreate = mkHandler @ChainlinkMilestoneCreate,
            chainlinkMilestoneList = mkHandler @ChainlinkMilestoneList
          },
      hooks =
        HookConfig
          { preToolUse = preToolUseWithImplementationBlock rootRedispatchMessage (\_ -> pure (allowResponse Nothing)),
            postToolUse = teamRegistrationPostToolUse,
            onStop = \_ -> pure allowStopResponse,
            onSubagentStop = \_ -> pure allowStopResponse,
            onSessionStart = tlSessionStartHook,
            beforeModel = \_ -> pure (BeforeModelAllow Nothing),
            afterModel = \_ -> pure (AfterModelAllow Nothing)
          },
      eventHandlers = tlPRReviewEventHandlers
    }
