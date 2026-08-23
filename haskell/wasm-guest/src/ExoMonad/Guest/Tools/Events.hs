{-# LANGUAGE DataKinds #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeFamilies #-}

-- | Event tools: notify_parent, send_tmux_message, send_mailbox_message.
--
-- Core I/O functions are role-agnostic.
-- Role-specific MCPTool wrappers apply their own state transitions.
-- Message tools stay in the SDK (no state transitions needed).
module ExoMonad.Guest.Tools.Events
  ( -- * Marker types
    NotifyParent (..),
    SendTmuxMessage (..),
    SendMailboxMessage (..),

    -- * Core functions (role wrappers call these)
    notifyParentCore,

    -- * Shared descriptions/schemas (role wrappers reuse these)
    notifyParentDescription,
    notifyParentSchema,

    -- * Args types (role wrappers need these)
    NotifyParentArgs (..),
    NotifyStatus (..),
    BlockedCause (..),
    BlockedEvidence (..),
    BlockedReport (..),
    TaskReport (..),
    SendMessageArgs (..),

    -- * Helpers
    composeNotifyMessage,
  )
where

import Control.Monad (void)
import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), ToJSON (..), Value, object, withObject, (.!=), (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Aeson.Types (Parser)
import Data.ByteString.Lazy qualified as BSL
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Log qualified as Log
import ExoMonad.Effects.Events qualified as ProtoEvents
import ExoMonad.Effects.Log (LogEmitEvent)
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (JsonSchema (..), genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect, suspendEffect_)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)
import Proto3.Suite.Types (Enumerated (..))

-- | Notify parent tool (for workers/subtrees to call on completion)
data NotifyParent = NotifyParent

-- | Status for notify_parent tool.
data NotifyStatus = Success | Failure | Blocked
  deriving (Show, Eq, Generic)

instance JsonSchema NotifyStatus where
  toSchema =
    object
      [ "type" .= ("string" :: Text),
        "enum" .= (["success", "failure", "blocked"] :: [Text])
      ]

instance FromJSON NotifyStatus where
  parseJSON = Aeson.withText "NotifyStatus" $ \case
    "success" -> pure Success
    "failure" -> pure Failure
    "blocked" -> pure Blocked
    other -> fail $ "Unknown status: " <> T.unpack other

instance ToJSON NotifyStatus where
  toJSON Success = Aeson.String "success"
  toJSON Failure = Aeson.String "failure"
  toJSON Blocked = Aeson.String "blocked"

data BlockedCause
  = BaseCiUnstable
  | ExternalDependency
  | ScopeBoundary
  | HumanDecisionRequired
  | ToolingUnavailable
  deriving (Show, Eq, Generic)

instance JsonSchema BlockedCause where
  toSchema =
    object
      [ "type" .= ("string" :: Text),
        "enum" .= (["base_ci_unstable", "external_dependency", "scope_boundary", "human_decision_required", "tooling_unavailable"] :: [Text])
      ]

instance FromJSON BlockedCause where
  parseJSON = Aeson.withText "BlockedCause" $ \case
    "base_ci_unstable" -> pure BaseCiUnstable
    "external_dependency" -> pure ExternalDependency
    "scope_boundary" -> pure ScopeBoundary
    "human_decision_required" -> pure HumanDecisionRequired
    "tooling_unavailable" -> pure ToolingUnavailable
    other -> fail $ "Unknown blocked cause: " <> T.unpack other

instance ToJSON BlockedCause where
  toJSON BaseCiUnstable = Aeson.String "base_ci_unstable"
  toJSON ExternalDependency = Aeson.String "external_dependency"
  toJSON ScopeBoundary = Aeson.String "scope_boundary"
  toJSON HumanDecisionRequired = Aeson.String "human_decision_required"
  toJSON ToolingUnavailable = Aeson.String "tooling_unavailable"

data BlockedEvidence = BlockedEvidence
  { beBaseSha :: Maybe Text,
    beHeadSha :: Maybe Text,
    beFailedChecks :: [Text],
    beEvidenceSummary :: Text
  }
  deriving (Generic, Show, Eq)

instance FromJSON BlockedEvidence where
  parseJSON = withObject "BlockedEvidence" $ \v ->
    BlockedEvidence
      <$> v .:? "base_sha"
      <*> v .:? "head_sha"
      <*> v .:? "failed_checks" .!= []
      <*> v .: "evidence_summary"

instance ToJSON BlockedEvidence where
  toJSON evidence =
    object
      [ "base_sha" .= beBaseSha evidence,
        "head_sha" .= beHeadSha evidence,
        "failed_checks" .= beFailedChecks evidence,
        "evidence_summary" .= beEvidenceSummary evidence
      ]

instance JsonSchema BlockedEvidence where
  toSchema =
    Aeson.Object $
      genericToolSchemaWith @BlockedEvidence
        [ ("base_sha", "Verified base commit SHA, when available."),
          ("head_sha", "Verified task head SHA, when available."),
          ("failed_checks", "Failed checks attributable to the blocker."),
          ("evidence_summary", "Bounded, human-readable evidence summary.")
        ]

data BlockedReport = BlockedReport
  { brCause :: BlockedCause,
    brNeedsHuman :: Bool,
    brScopeAttribution :: Text,
    brRetryable :: Bool,
    brRecoveryAction :: Text,
    brEvidence :: BlockedEvidence,
    brSliceId :: Text,
    brDeclaredDifficulty :: Text,
    brMatchedDifficultyRule :: Text,
    brAttempt :: Int
  }
  deriving (Generic, Show, Eq)

instance FromJSON BlockedReport where
  parseJSON = withObject "BlockedReport" $ \v -> do
    report <-
      BlockedReport
        <$> v .: "cause"
        <*> v .: "needs_human"
        <*> v .: "scope_attribution"
        <*> v .: "retryable"
        <*> v .: "recovery_action"
        <*> v .: "evidence"
        <*> v .: "slice_id"
        <*> v .: "declared_difficulty"
        <*> v .: "matched_difficulty_rule"
        <*> v .: "attempt"
    validateBlockedReport report
    pure report

instance ToJSON BlockedReport where
  toJSON report =
    object
      [ "cause" .= brCause report,
        "needs_human" .= brNeedsHuman report,
        "scope_attribution" .= brScopeAttribution report,
        "retryable" .= brRetryable report,
        "recovery_action" .= brRecoveryAction report,
        "evidence" .= brEvidence report,
        "slice_id" .= brSliceId report,
        "declared_difficulty" .= brDeclaredDifficulty report,
        "matched_difficulty_rule" .= brMatchedDifficultyRule report,
        "attempt" .= brAttempt report
      ]

instance JsonSchema BlockedReport where
  toSchema =
    Aeson.Object $
      genericToolSchemaWith @BlockedReport
        [ ("cause", "Closed vocabulary for the external blocker."),
          ("needs_human", "Must be true for a blocked handoff."),
          ("scope_attribution", "Whether the blocker is task, base, or external scope."),
          ("retryable", "Whether the same task can be retried after recovery."),
          ("recovery_action", "Concrete human or system recovery action."),
          ("evidence", "Structured base/head/check evidence."),
          ("slice_id", "Canonical task slice identity."),
          ("declared_difficulty", "Declared task difficulty: trivial, standard, or hard."),
          ("matched_difficulty_rule", "Deterministic classifier rule that matched the task."),
          ("attempt", "One-based attempt number for this task invocation.")
        ]

validateBlockedReport :: BlockedReport -> Parser ()
validateBlockedReport report
  | not (brNeedsHuman report) = fail "blocked handoff requires needs_human=true"
  | T.null (T.strip (brScopeAttribution report)) = fail "blocked handoff requires scope_attribution"
  | T.null (T.strip (brRecoveryAction report)) = fail "blocked handoff requires recovery_action"
  | T.null (T.strip (brSliceId report)) = fail "blocked handoff requires slice_id"
  | brDeclaredDifficulty report `notElem` ["trivial", "standard", "hard"] = fail "blocked handoff requires a supported declared_difficulty"
  | T.null (T.strip (brMatchedDifficultyRule report)) = fail "blocked handoff requires matched_difficulty_rule"
  | brAttempt report <= 0 = fail "blocked handoff requires a positive attempt"
  | T.null (T.strip (beEvidenceSummary (brEvidence report))) = fail "blocked handoff requires evidence_summary"
  | null (beFailedChecks (brEvidence report)) && beBaseSha (brEvidence report) == Nothing && beHeadSha (brEvidence report) == Nothing =
      fail "blocked handoff requires a check, base SHA, or head SHA"
  | otherwise = pure ()

-- | Structured task report for enriched notifications.
data TaskReport = TaskReport
  { trWhat :: Text,
    trHow :: Text
  }
  deriving (Generic, Show, Eq)

instance JsonSchema TaskReport where
  toSchema =
    Aeson.Object $
      genericToolSchemaWith @TaskReport
        [ ("what", "task description"),
          ("how", "verification command that was run")
        ]

instance FromJSON TaskReport where
  parseJSON = withObject "TaskReport" $ \v ->
    TaskReport <$> v .: "what" <*> v .: "how"

instance ToJSON TaskReport where
  toJSON (TaskReport w h) = object ["what" .= w, "how" .= h]

data NotifyParentArgs = NotifyParentArgs
  { npStatus :: NotifyStatus,
    npMessage :: Text,
    npPrNumber :: Maybe Int,
    npTasksCompleted :: Maybe [TaskReport],
    npBlocked :: Maybe BlockedReport
  }
  deriving (Generic, Show, Eq)

instance FromJSON NotifyParentArgs where
  parseJSON = withObject "NotifyParentArgs" $ \v -> do
    args <-
      NotifyParentArgs
        <$> v .: "status"
        <*> v .: "message"
        <*> v .:? "pr_number"
        <*> v .:? "tasks_completed"
        <*> v .:? "blocked"
    case (npStatus args, npBlocked args) of
      (Blocked, Nothing) -> fail "status=blocked requires blocked evidence"
      (Blocked, Just _) | T.null (T.strip (npMessage args)) -> fail "blocked handoff requires message"
      (Success, Just _) -> fail "blocked evidence is only valid with status=blocked"
      (Failure, Just _) -> fail "blocked evidence is only valid with status=blocked"
      _ -> pure args

instance ToJSON NotifyParentArgs where
  toJSON args =
    object
      [ "status" .= npStatus args,
        "message" .= npMessage args,
        "pr_number" .= npPrNumber args,
        "tasks_completed" .= npTasksCompleted args,
        "blocked" .= npBlocked args
      ]

-- | Shared tool description for notify_parent.
notifyParentDescription :: Text
notifyParentDescription = "Send a message to your parent agent. Use for status updates, progress reports, or failure escalation. Messages are delivered as-is with lightweight attribution. For PR-based workflows, the system auto-notifies your parent when Copilot approves — you don't need to signal completion yourself."

-- | Shared tool schema for notify_parent.
notifyParentSchema :: Aeson.Object
notifyParentSchema =
  genericToolSchemaWith @NotifyParentArgs
    [ ("status", "'success' = normal message. 'failure' = task failure. 'blocked' = typed external blocker requiring human action."),
      ("message", "The message to send. Be concise — one or two sentences."),
      ("pr_number", "PR number if relevant. Helps parent locate the PR without searching."),
      ("tasks_completed", "Array of {what, how} pairs. 'what' = task description, 'how' = verification command that was run."),
      ("blocked", "Required when status=blocked: cause, human-guidance requirement, scope, retry policy, recovery action, structured evidence, slice identity, difficulty, matched rule, and attempt.")
    ]

-- | Aggregate-only task-blocked telemetry. Evidence and message remain local.
blockedTelemetryPayload :: BlockedReport -> Aeson.Value
blockedTelemetryPayload report =
  object
    [ "outcome" .= ("blocked" :: Text),
      "slice_id" .= brSliceId report,
      "cause" .= brCause report,
      "scope_attribution" .= brScopeAttribution report,
      "needs_human" .= brNeedsHuman report,
      "retryable" .= brRetryable report,
      "recovery_action" .= brRecoveryAction report,
      "declared_difficulty" .= brDeclaredDifficulty report,
      "matched_difficulty_rule" .= brMatchedDifficultyRule report,
      "attempt" .= brAttempt report
    ]

-- | Core notify_parent I/O: emit event + deliver message to parent.
-- Returns Left on delivery failure, Right () on success.
notifyParentCore :: NotifyParentArgs -> Eff Effects (Either Text ())
notifyParentCore args = do
  -- Emit event via suspend
  let eventPayload =
        BSL.toStrict $
          Aeson.encode $
            case npBlocked args of
              Just report -> blockedTelemetryPayload report
              Nothing ->
                object
                  [ "status" .= npStatus args,
                    "message" .= npMessage args,
                    "pr_number" .= npPrNumber args,
                    "tasks_completed" .= npTasksCompleted args,
                    "head_sha" .= (Nothing :: Maybe Text),
                    "head_sha_finding" .= ("not_available_without_verified_pr_context" :: Text)
                  ]
  void $
    suspendEffect_ @LogEmitEvent
      ( Log.EmitEventRequest
          { Log.emitEventRequestEventType = case npStatus args of
              Blocked -> "agent.task_blocked"
              _ -> "agent.completed",
            Log.emitEventRequestPayload = eventPayload,
            Log.emitEventRequestTimestamp = 0
          }
      )

  let richMessage = composeNotifyMessage args
  let statusText = case npStatus args of
        Success -> "success" :: Text
        Failure -> "failure"
        Blocked -> "blocked"
  result <-
    suspendEffect @ProtoEvents.EventsNotifyParent
      ( ProtoEvents.NotifyParentRequest
          { ProtoEvents.notifyParentRequestAgentId = "",
            ProtoEvents.notifyParentRequestStatus = TL.fromStrict statusText,
            ProtoEvents.notifyParentRequestMessage = TL.fromStrict richMessage,
            ProtoEvents.notifyParentRequestOverrideRecipient = Nothing,
            ProtoEvents.notifyParentRequestTaskOutcome = protoBlocked (npBlocked args)
          }
      )
  case result of
    Left err -> pure $ Left (T.pack (show err))
    Right _ -> pure $ Right ()

-- | Compose enriched notification message with PR number and task reports.
composeNotifyMessage :: NotifyParentArgs -> Text
composeNotifyMessage args =
  let base = npMessage args
      prSuffix = case npPrNumber args of
        Just n -> " (PR #" <> T.pack (show n) <> ")"
        Nothing -> ""
      taskLines = case npTasksCompleted args of
        Just tasks -> T.concat ["\n  - " <> trWhat t <> " (verified: " <> trHow t <> ")" | t <- tasks]
        Nothing -> ""
      blockedLine = case npBlocked args of
        Just report -> "\n  blocker: " <> T.pack (show (brCause report)) <> "; recovery: " <> brRecoveryAction report
        Nothing -> ""
   in base <> prSuffix <> taskLines <> blockedLine

protoBlocked :: Maybe BlockedReport -> Maybe ProtoEvents.NotifyParentRequestTaskOutcome
protoBlocked Nothing = Nothing
protoBlocked (Just report) =
  Just
    ( ProtoEvents.NotifyParentRequestTaskOutcomeBlocked $
        ProtoEvents.TaskBlocked
          { ProtoEvents.taskBlockedCause = Enumerated (Right (protoCause (brCause report))),
            ProtoEvents.taskBlockedNeedsHuman = brNeedsHuman report,
            ProtoEvents.taskBlockedScopeAttribution = TL.fromStrict (brScopeAttribution report),
            ProtoEvents.taskBlockedRetryable = brRetryable report,
            ProtoEvents.taskBlockedRecoveryAction = TL.fromStrict (brRecoveryAction report),
            ProtoEvents.taskBlockedEvidence = Just (protoEvidence (brEvidence report)),
            ProtoEvents.taskBlockedSliceId = TL.fromStrict (brSliceId report),
            ProtoEvents.taskBlockedDeclaredDifficulty = TL.fromStrict (brDeclaredDifficulty report),
            ProtoEvents.taskBlockedMatchedDifficultyRule = TL.fromStrict (brMatchedDifficultyRule report),
            ProtoEvents.taskBlockedAttempt = fromIntegral (brAttempt report)
          }
    )

protoEvidence :: BlockedEvidence -> ProtoEvents.TaskBlockedEvidence
protoEvidence evidence =
  ProtoEvents.TaskBlockedEvidence
    { ProtoEvents.taskBlockedEvidenceBaseSha = TL.fromStrict (maybe "" id (beBaseSha evidence)),
      ProtoEvents.taskBlockedEvidenceHeadSha = TL.fromStrict (maybe "" id (beHeadSha evidence)),
      ProtoEvents.taskBlockedEvidenceFailedChecks = V.fromList (TL.fromStrict <$> beFailedChecks evidence),
      ProtoEvents.taskBlockedEvidenceEvidenceSummary = TL.fromStrict (beEvidenceSummary evidence)
    }

protoCause :: BlockedCause -> ProtoEvents.TaskBlockCause
protoCause BaseCiUnstable = ProtoEvents.TaskBlockCauseTASK_BLOCK_CAUSE_BASE_CI_UNSTABLE
protoCause ExternalDependency = ProtoEvents.TaskBlockCauseTASK_BLOCK_CAUSE_EXTERNAL_DEPENDENCY
protoCause ScopeBoundary = ProtoEvents.TaskBlockCauseTASK_BLOCK_CAUSE_SCOPE_BOUNDARY
protoCause HumanDecisionRequired = ProtoEvents.TaskBlockCauseTASK_BLOCK_CAUSE_HUMAN_DECISION_REQUIRED
protoCause ToolingUnavailable = ProtoEvents.TaskBlockCauseTASK_BLOCK_CAUSE_TOOLING_UNAVAILABLE

-- | Shared args for agent-to-agent message tools.
data SendMessageArgs = SendMessageArgs
  { smRecipient :: Text,
    smContent :: Text,
    smSummary :: Maybe Text
  }
  deriving (Generic, Show)

instance FromJSON SendMessageArgs where
  parseJSON = withObject "SendMessageArgs" $ \v ->
    SendMessageArgs
      <$> v .: "recipient"
      <*> v .: "content"
      <*> v .:? "summary"

instance ToJSON SendMessageArgs where
  toJSON args =
    object
      [ "recipient" .= smRecipient args,
        "content" .= smContent args,
        "summary" .= smSummary args
      ]

sendMessageAddress :: SendMessageArgs -> ProtoEvents.Address
sendMessageAddress args =
  ProtoEvents.Address
    { ProtoEvents.addressKind = Just (ProtoEvents.AddressKindAgent (TL.fromStrict (smRecipient args)))
    }

sendTmuxMessageDescription :: Text
sendTmuxMessageDescription = "Send a message to an exomonad-spawned agent by injecting it into that agent's tmux pane. Use this for Codex, OpenCode, Retired, and any non-Claude runtime, or when you need to steer a live pane directly."

sendMailboxMessageDescription :: Text
sendMailboxMessageDescription = "Send a message through the Claude Teams inbox mailbox protocol. This only works when the current session has mailbox support configured and validated."

sendMessageSchema :: Aeson.Object
sendMessageSchema =
  genericToolSchemaWith @SendMessageArgs
    [ ("recipient", "The name of the agent to receive the message"),
      ("content", "The content of the message"),
      ("summary", "An optional summary of the message")
    ]

-- | Tmux-only message tool.
data SendTmuxMessage = SendTmuxMessage

instance MCPTool SendTmuxMessage where
  type ToolArgs SendTmuxMessage = SendMessageArgs
  toolName = "send_tmux_message"
  toolDescription = sendTmuxMessageDescription
  toolSchema = sendMessageSchema
  toolHandlerEff args = do
    result <-
      suspendEffect @ProtoEvents.EventsSendTmuxMessage
        ( ProtoEvents.SendTmuxMessageRequest
            { ProtoEvents.sendTmuxMessageRequestRecipient = Just (sendMessageAddress args),
              ProtoEvents.sendTmuxMessageRequestContent = TL.fromStrict (smContent args),
              ProtoEvents.sendTmuxMessageRequestSummary = maybe "" TL.fromStrict (smSummary args)
            }
        )
    case result of
      Left err -> pure $ errorResult (T.pack (show err))
      Right resp ->
        pure $
          successResult $
            object
              [ "success" .= ProtoEvents.sendTmuxMessageResponseSuccess resp,
                "delivery_method" .= ProtoEvents.sendTmuxMessageResponseDeliveryMethod resp
              ]

-- | Mailbox-only message tool.
data SendMailboxMessage = SendMailboxMessage

instance MCPTool SendMailboxMessage where
  type ToolArgs SendMailboxMessage = SendMessageArgs
  toolName = "send_mailbox_message"
  toolDescription = sendMailboxMessageDescription
  toolSchema = sendMessageSchema
  toolHandlerEff args = do
    result <-
      suspendEffect @ProtoEvents.EventsSendMailboxMessage
        ( ProtoEvents.SendMailboxMessageRequest
            { ProtoEvents.sendMailboxMessageRequestRecipient = Just (sendMessageAddress args),
              ProtoEvents.sendMailboxMessageRequestContent = TL.fromStrict (smContent args),
              ProtoEvents.sendMailboxMessageRequestSummary = maybe "" TL.fromStrict (smSummary args)
            }
        )
    case result of
      Left err -> pure $ errorResult (T.pack (show err))
      Right resp ->
        pure $
          successResult $
            object
              [ "success" .= ProtoEvents.sendMailboxMessageResponseSuccess resp,
                "delivery_method" .= ProtoEvents.sendMailboxMessageResponseDeliveryMethod resp
              ]
