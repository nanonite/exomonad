{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}
{-# LANGUAGE TypeOperators #-}

-- | Root TL role: orchestration-only. No file_pr, notify_parent, or shutdown.
--   Used for the root human-facing TL window (exomonad init).
module RootRole (config, Tools) where

import Control.Monad (forM_, void, when)
import Data.Text (Text)
import ExoMonad
import ExoMonad.Guest.Effects.AgentControl (SpawnResult (..))
import ExoMonad.Guest.Effects.StopHook (getCurrentBranch)
import ExoMonad.Guest.StateMachine (applyEvent)
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
import ExoMonad.Guest.Tools.DisposeLeaf (DisposeLeaf (..))
import ExoMonad.Guest.Tools.MergePR (MergePRArgs (..), MergePROutput (..), extractAgentName, mergePRCore, mergePRDescription, mergePRRender, mergePRSchema)
import ExoMonad.Guest.Tools.Spawn
  ( CloseWorkerPaneArgs,
    ForkWaveArgs (..),
    ForkWaveResult (..),
    SpawnLeafArgs (..),
    SpawnLeafSubtreeArgs,
    SpawnWorkerToolArgs,
    closeWorkerPaneCore,
    closeWorkerPaneDescription,
    closeWorkerPaneSchema,
    forkWaveCore,
    forkWaveDescription,
    forkWaveRender,
    forkWaveSchema,
    spawnLeafCore,
    spawnLeafDescription,
    spawnLeafRender,
    spawnLeafSchema,
    spawnWorkerToolCore,
    spawnWorkerToolDescription,
    spawnWorkerToolSchema,
  )
import ExoMonad.Guest.Tools.SpawnCodex (SpawnCodex, handleSpawnCodex, spawnCodexDescription, spawnCodexSchema)
import ExoMonad.Guest.Types (AfterModelOutput (..), BeforeModelOutput (..), allowResponse, allowStopResponse)
import ExoMonad.Types (Effects, HookConfig (..), defaultSessionStartHook, teamRegistrationPostToolUse)
import HookPolicy (preToolUseWithImplementationBlock)
import PRReviewHandler (tlPRReviewEventHandlers)
import TLPhase (ChildHandle (..), TLEvent (..), TLPhase (..))

rootRedispatchMessage :: Text -> Text
rootRedispatchMessage toolName =
  "TL agents cannot use "
    <> toolName
    <> ". The TL plans and dispatches; implementation belongs to leaves and workers.\n"
    <> "If a leaf needs to fix code based on review feedback, the leaf does it; reviewer comments are injected into its pane automatically.\n"
    <> "If a worker is blocked, use send_message to inject a clarification into the worker's pane. See Worker Correction Loop in .exo/roles/devswarm/context/root.md.\n"
    <> "If neither path fits, re-decompose with spawn_leaf or spawn_worker.\n"
    <> "See CLAUDE.md § Tech Lead Praxis for the full protocol."

data RootForkWave

instance MCPTool RootForkWave where
  type ToolArgs RootForkWave = ForkWaveArgs
  toolName = "fork_wave"
  toolDescription = forkWaveDescription
  toolSchema = forkWaveSchema
  toolHandlerEff args = do
    result <- forkWaveCore args
    case result of
      Left err -> pure $ errorResult err
      Right fwResult -> do
        forM_ (fwrSpawned fwResult) $ \(slug, sr) -> do
          let handle = ChildHandle {chSlug = slug, chBranch = branchName sr, chAgentType = agentTypeResult sr}
          branch <- getCurrentBranch
          void $ applyEvent @TLPhase @TLEvent branch TLPlanning (ChildSpawned handle)
        pure $ forkWaveRender fwResult

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
  { forkWave :: mode :- RootForkWave,
    spawnLeaf :: mode :- RootSpawnLeaf,
    spawnWorker :: mode :- RootSpawnWorker,
    closeWorkerPane :: mode :- RootCloseWorkerPane,
    spawnCodex :: mode :- RootSpawnCodex,
    mergePr :: mode :- RootMergePR,
    sendMessage :: mode :- SendMessage,
    chainlinkIssueCreate :: mode :- ChainlinkIssueCreate,
    chainlinkSessionStart :: mode :- ChainlinkSessionStart,
    chainlinkSessionStatus :: mode :- ChainlinkSessionStatus,
    chainlinkIssueShow :: mode :- ChainlinkIssueShow,
    chainlinkIssueComment :: mode :- ChainlinkIssueComment,
    chainlinkSubissueCreate :: mode :- ChainlinkSubissueCreate,
    chainlinkSessionWork :: mode :- ChainlinkSessionWork,
    chainlinkSessionEnd :: mode :- ChainlinkSessionEnd,
    chainlinkIssueClose :: mode :- ChainlinkIssueClose,
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
          { forkWave = mkHandler @RootForkWave,
            spawnLeaf = mkHandler @RootSpawnLeaf,
            spawnWorker = mkHandler @RootSpawnWorker,
            closeWorkerPane = mkHandler @RootCloseWorkerPane,
            spawnCodex = mkHandler @RootSpawnCodex,
            mergePr = mkHandler @RootMergePR,
            sendMessage = mkHandler @SendMessage,
            chainlinkIssueCreate = mkHandler @ChainlinkIssueCreate,
            chainlinkSessionStart = mkHandler @ChainlinkSessionStart,
            chainlinkSessionStatus = mkHandler @ChainlinkSessionStatus,
            chainlinkIssueShow = mkHandler @ChainlinkIssueShow,
            chainlinkIssueComment = mkHandler @ChainlinkIssueComment,
            chainlinkSubissueCreate = mkHandler @ChainlinkSubissueCreate,
            chainlinkSessionWork = mkHandler @ChainlinkSessionWork,
            chainlinkSessionEnd = mkHandler @ChainlinkSessionEnd,
            chainlinkIssueClose = mkHandler @ChainlinkIssueClose,
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
            onSessionStart = defaultSessionStartHook,
            beforeModel = \_ -> pure (BeforeModelAllow Nothing),
            afterModel = \_ -> pure (AfterModelAllow Nothing)
          },
      eventHandlers = tlPRReviewEventHandlers
    }
