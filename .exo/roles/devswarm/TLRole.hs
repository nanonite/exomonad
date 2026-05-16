{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}
{-# LANGUAGE TypeOperators #-}

-- | TL role config: spawn, PR, merge tools with state transitions and stop hook checks.
module TLRole (config, Tools) where

import Control.Monad (void, forM_, when)
import Control.Monad.Freer (Eff)
import Data.Aeson (object, (.=))
import Data.Aeson qualified as Aeson
import ExoMonad
import ExoMonad.Guest.StateMachine (applyEvent, StopCheckResult(..), checkExit)
import ExoMonad.Guest.Effects.StopHook (checkUncommittedWork, getCurrentBranch)
import ExoMonad.Guest.Tools.FilePR (filePRCore, filePRDescription, filePRSchema, FilePRArgs, FilePROutput (..))
import ExoMonad.Guest.Tools.Chainlink
  ( ChainlinkIssueCreate (..),
    ChainlinkSessionStart (..),
    ChainlinkSessionStatus (..),
    ChainlinkIssueShow (..),
    ChainlinkIssueComment (..),
    ChainlinkSubissueCreate (..),
    ChainlinkSessionWork (..),
    ChainlinkSessionEnd (..),
    ChainlinkIssueClose (..),
    ChainlinkTimerStart (..),
    ChainlinkTimerStop (..),
    ChainlinkTimerStatus (..),
    ChainlinkIssueList (..),
    ChainlinkIssueUpdate (..),
    ChainlinkBlock (..),
    ChainlinkRelate (..),
    ChainlinkCascade (..),
    ChainlinkMilestoneCreate (..),
    ChainlinkMilestoneList (..)
  )
import ExoMonad.Guest.Tools.Events
  ( notifyParentCore, notifyParentDescription, notifyParentSchema, NotifyParentArgs (..)
  )
import ExoMonad.Guest.Tools.MergePR (mergePRCore, mergePRDescription, mergePRSchema, mergePRRender, MergePRArgs (..), MergePROutput (..), extractAgentName)
import ExoMonad.Guest.Tools.Spawn
  ( forkWaveCore, forkWaveDescription, forkWaveSchema, forkWaveRender, ForkWaveArgs (..), ForkWaveResult (..),
      spawnLeafCore, spawnLeafDescription, spawnLeafSchema, SpawnLeafArgs, SpawnLeafSubtreeArgs,
      spawnLeafRender,
      spawnWorkerToolCore, spawnWorkerToolDescription, spawnWorkerToolSchema, SpawnWorkerToolArgs,
      closeWorkerPaneCore, closeWorkerPaneDescription, closeWorkerPaneSchema, CloseWorkerPaneArgs,
      spawnAcpCore, SpawnAcpArgs
  )
import ExoMonad.Guest.Tools.SpawnCodex (handleSpawnCodex, spawnCodexDescription, spawnCodexSchema, SpawnCodex)
import ExoMonad.Guest.Effects.AgentControl (SpawnResult (..))
import ExoMonad.Guest.Types (StopDecision(..), StopHookOutput(..), blockStopResponse, allowStopResponse, allowResponse, BeforeModelOutput (..), AfterModelOutput (..))
import ExoMonad.Types (HookConfig (..), Effects, defaultSessionStartHook, teamRegistrationPostToolUse)
import HookPolicy (preToolUseWithGhBlock)
import PRReviewHandler (prReviewEventHandlers)
import TLPhase (TLPhase (..), TLEvent (..), ChildHandle (..))
import TLStopCheck (tlStopCheck)

-- | TL-specific file_pr: files PR, transitions TLPhase.
data TLFilePR

instance MCPTool TLFilePR where
  type ToolArgs TLFilePR = FilePRArgs
  toolName = "file_pr"
  toolDescription = filePRDescription
  toolSchema = filePRSchema
  toolHandlerEff args = do
    result <- filePRCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> do
        branch <- getCurrentBranch
        void $ applyEvent @TLPhase @TLEvent branch TLPlanning
          (OwnPRFiled (fpoNumber output) (fpoUrl output) (fpoHeadBranch output))
        pure $ successResult (Aeson.toJSON output)

-- | TL-specific merge_pr: merges child PR, transitions TLPhase via ChildCompleted.
data TLMergePR

instance MCPTool TLMergePR where
  type ToolArgs TLMergePR = MergePRArgs
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

-- | TL-specific fork_wave: spawns Claude subtrees, fires ChildSpawned per child.
data TLForkWave

instance MCPTool TLForkWave where
  type ToolArgs TLForkWave = ForkWaveArgs
  toolName = "fork_wave"
  toolDescription = forkWaveDescription
  toolSchema = forkWaveSchema
  toolHandlerEff args = do
    result <- forkWaveCore args
    case result of
      Left err -> pure $ errorResult err
      Right fwResult -> do
        forM_ (fwrSpawned fwResult) $ \(slug, sr) -> do
          let handle = ChildHandle
                { chSlug = slug
                , chBranch = branchName sr
                , chAgentType = agentTypeResult sr
                }
          branch <- getCurrentBranch
          void $ applyEvent @TLPhase @TLEvent branch TLPlanning (ChildSpawned handle)
        pure $ forkWaveRender fwResult

-- | TL-specific spawn_leaf: worktree spawn fires ChildSpawned.
data TLSpawnLeaf

instance MCPTool TLSpawnLeaf where
  type ToolArgs TLSpawnLeaf = SpawnLeafArgs
  toolName = "spawn_leaf"
  toolDescription = spawnLeafDescription
  toolSchema = spawnLeafSchema
  toolHandlerEff args = do
    result <- spawnLeafCore args
    case result of
      Left err -> pure $ errorResult err
      Right (slug, sr) -> do
        let handle = ChildHandle
              { chSlug = slug
              , chBranch = branchName sr
              , chAgentType = agentTypeResult sr
              }
        branch <- getCurrentBranch
        void $ applyEvent @TLPhase @TLEvent branch TLPlanning (ChildSpawned handle)
        pure $ spawnLeafRender (Right (slug, sr))

-- | TL-specific spawn_worker: ephemeral pane, no state transition.
data TLSpawnWorker

instance MCPTool TLSpawnWorker where
  type ToolArgs TLSpawnWorker = SpawnWorkerToolArgs
  toolName = "spawn_worker"
  toolDescription = spawnWorkerToolDescription
  toolSchema = spawnWorkerToolSchema
  toolHandlerEff args = spawnWorkerToolCore args

data TLCloseWorkerPane

instance MCPTool TLCloseWorkerPane where
  type ToolArgs TLCloseWorkerPane = CloseWorkerPaneArgs
  toolName = "close_worker_pane"
  toolDescription = closeWorkerPaneDescription
  toolSchema = closeWorkerPaneSchema
  toolHandlerEff args = closeWorkerPaneCore args

data TLSpawnCodex

instance MCPTool TLSpawnCodex where
  type ToolArgs TLSpawnCodex = SpawnLeafSubtreeArgs
  toolName = "spawn_codex"
  toolDescription = spawnCodexDescription
  toolSchema = spawnCodexSchema
  toolHandlerEff args = do
    result <- handleSpawnCodex args
    case result of
      Left err -> pure $ errorResult err
      Right (slug, sr) -> do
        let handle = ChildHandle
              { chSlug = slug
              , chBranch = branchName sr
              , chAgentType = agentTypeResult sr
              }
        branch <- getCurrentBranch
        void $ applyEvent @TLPhase @TLEvent branch TLPlanning (ChildSpawned handle)
        pure $ spawnLeafRender (Right (slug, sr))

-- | TL notify_parent: thin wrapper, no phase transitions.
data TLNotifyParent

instance MCPTool TLNotifyParent where
  type ToolArgs TLNotifyParent = NotifyParentArgs
  toolName = "notify_parent"
  toolDescription = notifyParentDescription
  toolSchema = notifyParentSchema
  toolHandlerEff args = do
    result <- notifyParentCore args
    case result of
      Left err -> pure $ errorResult err
      Right _ -> pure $ successResult $ object ["success" .= True]

data Tools mode = Tools
  { forkWave :: mode :- TLForkWave,
      spawnLeaf :: mode :- TLSpawnLeaf,
      spawnWorker :: mode :- TLSpawnWorker,
      closeWorkerPane :: mode :- TLCloseWorkerPane,
      spawnCodex :: mode :- TLSpawnCodex,
    pr :: mode :- TLFilePR,
    mergePr :: mode :- TLMergePR,
    notifyParent :: mode :- TLNotifyParent,
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
      { roleName = "tl",
        tools =
          Tools
            { forkWave = mkHandler @TLForkWave,
                spawnLeaf = mkHandler @TLSpawnLeaf,
                spawnWorker = mkHandler @TLSpawnWorker,
                closeWorkerPane = mkHandler @TLCloseWorkerPane,
                spawnCodex = mkHandler @TLSpawnCodex,
              pr = mkHandler @TLFilePR,
              mergePr = mkHandler @TLMergePR,
              notifyParent = mkHandler @TLNotifyParent,
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
          { preToolUse = preToolUseWithGhBlock (\_ -> pure (allowResponse Nothing)),
            postToolUse = teamRegistrationPostToolUse,
            onStop = \_ -> tlStopCheck,
            onSubagentStop = \_ -> tlStopCheck,
            onSessionStart = defaultSessionStartHook,
            beforeModel = \_ -> pure (BeforeModelAllow Nothing),
            afterModel = \_ -> pure (AfterModelAllow Nothing)
          },
      eventHandlers = prReviewEventHandlers
    }
