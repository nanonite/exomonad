{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}
{-# LANGUAGE TypeOperators #-}

-- | TL role config: an RPC surface for the programmatic Python controller.
module TLRole (config, Tools) where

import Data.Aeson (FromJSON (..), ToJSON (..), object, withObject, (.:), (.=))
import Data.Aeson qualified as Aeson
import Data.Aeson.KeyMap qualified as KM
import Data.ByteString.Lazy qualified as BSL
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import ExoMonad
import ExoMonad.Effects.Tl qualified as Tl
import ExoMonad.Guest.ReviewHandoff (reviewHandoffInstructions)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
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
import ExoMonad.Guest.Tools.DiscardWorkerOutput (DiscardWorkerOutput (..))
import ExoMonad.Guest.Tools.DisposeLeaf (DisposeLeaf (..))
import ExoMonad.Guest.Tools.Events
  ( NotifyParentArgs (..),
    notifyParentCore,
    notifyParentDescription,
    notifyParentSchema,
  )
import ExoMonad.Guest.Tools.FilePR (FilePRArgs, filePRCore, filePRDescription, filePRSchema)
import ExoMonad.Guest.Tools.Memory (ContinuationBrief (..), MemoryAppend (..), MemoryList (..))
import ExoMonad.Guest.Tools.MergePR (MergePRArgs, mergePRCore, mergePRDescription, mergePRRender, mergePRSchema)
import ExoMonad.Guest.Tools.PollWorkers (PollWorkers (..))
import ExoMonad.Guest.Tools.PostMergeRecovery
  ( PostMergeChangelog,
    PostMergeParentSync,
    PostMergePush,
    PostMergeRemoteReconcile,
  )
import ExoMonad.Guest.Tools.ReplaceClosedPr (ReplaceClosedPr (..))
import ExoMonad.Guest.Tools.ResolveLivePrForSlice (ResolveLivePrForSlice (..))
import ExoMonad.Guest.Tools.RestartReview (RestartReview (..))
import ExoMonad.Guest.Tools.ResumeBlockedLeaf (ResumeBlockedLeaf (..))
import ExoMonad.Guest.Tools.ResumePr (ResumePr (..))
import ExoMonad.Guest.Tools.RootBranchFinalize (RootBranchFinalize)
import ExoMonad.Guest.Tools.SessionStatus (SessionStatus (..))
import ExoMonad.Guest.Tools.Spawn
  ( CloseWorkerPaneArgs,
    SpawnLeafArgs,
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
import ExoMonad.Guest.Tools.SpawnCodex (handleSpawnCodex, spawnCodexDescription, spawnCodexSchema)
import ExoMonad.Guest.Tools.SpawnReviewer (SpawnReviewer (..))
import ExoMonad.Guest.Tools.WatcherPrState (WatcherPrState (..))
import ExoMonad.Guest.Types (AfterModelOutput (..), BeforeModelOutput (..), allowResponse, allowStopResponse)
import ExoMonad.Types (HookConfig (..), teamRegistrationPostToolUse, tlSessionStartHook)
import HookPolicy (preToolUseWithImplementationBlock)
import PRReviewHandler (tlPRReviewEventHandlers)

tlRedispatchMessage :: Text -> Text
tlRedispatchMessage toolName =
  "TL agents cannot use "
    <> toolName
    <> ". The TL plans and dispatches; implementation belongs to leaves and workers.\n"
    <> "Reviewer comments are delivered to the TL; they are not auto-applied to a leaf. Read the review, analyze the root cause, and prepare the repair handoff before steering the owner.\n"
    <> reviewHandoffInstructions
    <> "\nIf a worker is blocked, use send_tmux_message to inject a clarification into the worker's pane. See Worker Correction Loop in .exo/roles/devswarm/context/root.md.\n"
    <> "If neither path fits, re-decompose with spawn_leaf or spawn_worker.\n"
    <> "See CLAUDE.md § Tech Lead Praxis for the full protocol."

-- | TL-specific file_pr RPC wrapper.
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
      Right output -> pure $ successResult (Aeson.toJSON output)

-- | TL-specific merge_pr RPC wrapper.
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
      Right output -> pure $ mergePRRender output

-- | TL-specific spawn_leaf RPC wrapper.
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
      Right result -> pure $ spawnLeafRender (Right result)

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
      Right result -> pure $ spawnLeafRender (Right result)

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

data TLEmitControllerEventArgs = TLEmitControllerEventArgs
  { tleEventType :: Text,
    tlePayload :: Aeson.Value
  }
  deriving (Generic, Show)

instance FromJSON TLEmitControllerEventArgs where
  parseJSON = withObject "TLEmitControllerEventArgs" $ \value ->
    TLEmitControllerEventArgs <$> value .: "event_type" <*> value .: "payload"

instance ToJSON TLEmitControllerEventArgs where
  toJSON args = object ["event_type" .= tleEventType args, "payload" .= tlePayload args]

controllerPayloadSchema :: Aeson.Object
controllerPayloadSchema =
  KM.fromList
    [ ("accepted", Aeson.object ["type" .= ("boolean" :: Text)]),
      ("attempt", Aeson.object ["type" .= ("integer" :: Text)]),
      ("attempts", Aeson.object ["type" .= ("integer" :: Text)]),
      ("decision", Aeson.object ["type" .= ("string" :: Text)]),
      ("from_phase", Aeson.object ["type" .= ("string" :: Text)]),
      ("from_status", Aeson.object ["type" .= ("string" :: Text)]),
      ("gate_name", Aeson.object ["type" .= ("string" :: Text)]),
      ("head_sha_hash", Aeson.object ["type" .= ("string" :: Text)]),
      ("judgment", Aeson.object ["type" .= ("string" :: Text)]),
      ("latency_ms", Aeson.object ["type" .= ("integer" :: Text)]),
      ("model", Aeson.object ["type" .= ("string" :: Text)]),
      ("outcome", Aeson.object ["type" .= ("string" :: Text)]),
      ("park_cause", Aeson.object ["type" .= ("string" :: Text)]),
      ("pr_number", Aeson.object ["type" .= ("integer" :: Text)]),
      ("redacted_result", Aeson.object ["type" .= ("string" :: Text)]),
      ("rejection_reason", Aeson.object ["type" .= ("string" :: Text)]),
      ("replayed", Aeson.object ["type" .= ("boolean" :: Text)]),
      ("run_id", Aeson.object ["type" .= ("string" :: Text)]),
      ("slice_id", Aeson.object ["type" .= ("string" :: Text)]),
      ("source", Aeson.object ["type" .= ("string" :: Text)]),
      ("to_phase", Aeson.object ["type" .= ("string" :: Text)]),
      ("to_status", Aeson.object ["type" .= ("string" :: Text)]),
      ("tokens", Aeson.object ["type" .= ("integer" :: Text)])
    ]

data TLEmitControllerEvent

instance MCPTool TLEmitControllerEvent where
  type ToolArgs TLEmitControllerEvent = TLEmitControllerEventArgs
  toolName = "emit_controller_event"
  toolDescription = "Emit bounded controller observability dimensions to the canonical ledger."
  toolSchema =
    KM.fromList
      [ ("type", Aeson.String "object"),
        ( "properties",
          Aeson.Object $
            KM.fromList
              [ ("event_type", Aeson.object ["type" .= ("string" :: Text), "description" .= ("Declared tl.* controller event type" :: Text)]),
                ( "payload",
                  Aeson.object
                    [ "type" .= ("object" :: Text),
                      "description" .= ("Bounded aggregate dimensions only; no bodies or prose" :: Text),
                      "properties" .= Aeson.Object controllerPayloadSchema
                    ]
                )
              ]
        ),
        ("required", Aeson.toJSON (["event_type", "payload"] :: [Text]))
      ]
  toolHandlerEff args = do
    result <-
      suspendEffect @Tl.TlEmitEvent
        ( Tl.EmitEventRequest
            { Tl.emitEventRequestEventType = TL.fromStrict (tleEventType args),
              Tl.emitEventRequestPayload = BSL.toStrict (Aeson.encode (tlePayload args))
            }
        )
    pure $ case result of
      Left err -> errorResult (T.pack (show err))
      Right response -> successResult (object ["event_id" .= Tl.emitEventResponseEventId response])

data Tools mode = Tools
  { spawnLeaf :: mode :- TLSpawnLeaf,
    spawnWorker :: mode :- TLSpawnWorker,
    spawnReviewer :: mode :- SpawnReviewer,
    cleanupReviewerLeaf :: mode :- CleanupReviewerLeaf,
    closeReviewerWindow :: mode :- CloseReviewerWindow,
    restartReview :: mode :- RestartReview,
    replaceClosedPr :: mode :- ReplaceClosedPr,
    resumePr :: mode :- ResumePr,
    resumeBlockedLeaf :: mode :- ResumeBlockedLeaf,
    resolveLivePrForSlice :: mode :- ResolveLivePrForSlice,
    watcherPrState :: mode :- WatcherPrState,
    postMergeParentSync :: mode :- PostMergeParentSync,
    postMergeRemoteReconcile :: mode :- PostMergeRemoteReconcile,
    postMergeChangelog :: mode :- PostMergeChangelog,
    postMergePush :: mode :- PostMergePush,
    rootBranchFinalize :: mode :- RootBranchFinalize,
    discardWorkerOutput :: mode :- DiscardWorkerOutput,
    disposeLeaf :: mode :- DisposeLeaf,
    closeWorkerPane :: mode :- TLCloseWorkerPane,
    spawnCodex :: mode :- TLSpawnCodex,
    sessionStatus :: mode :- SessionStatus,
    pollWorkers :: mode :- PollWorkers,
    memoryAppend :: mode :- MemoryAppend,
    memoryList :: mode :- MemoryList,
    continuationBrief :: mode :- ContinuationBrief,
    listAgents :: mode :- ListAgents,
    pr :: mode :- TLFilePR,
    mergePr :: mode :- TLMergePR,
    notifyParent :: mode :- TLNotifyParent,
    emitControllerEvent :: mode :- TLEmitControllerEvent,
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
    { roleName = "tl",
      tools =
        Tools
          { spawnLeaf = mkHandler @TLSpawnLeaf,
            spawnWorker = mkHandler @TLSpawnWorker,
            spawnReviewer = mkHandler @SpawnReviewer,
            cleanupReviewerLeaf = mkHandler @CleanupReviewerLeaf,
            closeReviewerWindow = mkHandler @CloseReviewerWindow,
            restartReview = mkHandler @RestartReview,
            replaceClosedPr = mkHandler @ReplaceClosedPr,
            resumePr = mkHandler @ResumePr,
            resumeBlockedLeaf = mkHandler @ResumeBlockedLeaf,
            resolveLivePrForSlice = mkHandler @ResolveLivePrForSlice,
            watcherPrState = mkHandler @WatcherPrState,
            postMergeParentSync = mkHandler @PostMergeParentSync,
            postMergeRemoteReconcile = mkHandler @PostMergeRemoteReconcile,
            postMergeChangelog = mkHandler @PostMergeChangelog,
            postMergePush = mkHandler @PostMergePush,
            rootBranchFinalize = mkHandler @RootBranchFinalize,
            discardWorkerOutput = mkHandler @DiscardWorkerOutput,
            disposeLeaf = mkHandler @DisposeLeaf,
            closeWorkerPane = mkHandler @TLCloseWorkerPane,
            spawnCodex = mkHandler @TLSpawnCodex,
            sessionStatus = mkHandler @SessionStatus,
            pollWorkers = mkHandler @PollWorkers,
            memoryAppend = mkHandler @MemoryAppend,
            memoryList = mkHandler @MemoryList,
            continuationBrief = mkHandler @ContinuationBrief,
            listAgents = mkHandler @ListAgents,
            pr = mkHandler @TLFilePR,
            mergePr = mkHandler @TLMergePR,
            notifyParent = mkHandler @TLNotifyParent,
            emitControllerEvent = mkHandler @TLEmitControllerEvent,
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
          { preToolUse = preToolUseWithImplementationBlock tlRedispatchMessage (\_ -> pure (allowResponse Nothing)),
            postToolUse = teamRegistrationPostToolUse,
            onStop = \_ -> pure allowStopResponse,
            onSubagentStop = \_ -> pure allowStopResponse,
            onSessionStart = tlSessionStartHook,
            beforeModel = \_ -> pure (BeforeModelAllow Nothing),
            afterModel = \_ -> pure (AfterModelAllow Nothing)
          },
      eventHandlers = tlPRReviewEventHandlers
    }
