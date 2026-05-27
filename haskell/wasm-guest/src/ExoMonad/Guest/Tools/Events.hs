{-# LANGUAGE DataKinds #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeFamilies #-}

-- | Event tools: notify_parent, send_tmux_message, send_mailbox_message, shutdown.
--
-- Core I/O functions ('notifyParentCore', 'shutdownCore') are role-agnostic.
-- Role-specific MCPTool wrappers apply their own state transitions.
-- Message tools stay in the SDK (no state transitions needed).
module ExoMonad.Guest.Tools.Events
  ( -- * Marker types
    NotifyParent (..),
    SendTmuxMessage (..),
    SendMailboxMessage (..),
    Shutdown,

    -- * Core functions (role wrappers call these)
    notifyParentCore,
    shutdownCore,

    -- * Shared descriptions/schemas (role wrappers reuse these)
    notifyParentDescription,
    notifyParentSchema,
    shutdownDescription,
    shutdownSchema,

    -- * Args types (role wrappers need these)
    NotifyParentArgs (..),
    NotifyStatus (..),
    TaskReport (..),
    SendMessageArgs (..),
    ShutdownArgs (..),

    -- * Helpers
    composeNotifyMessage,
  )
where

import Control.Monad (void)
import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), ToJSON (..), Value, object, withObject, (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.ByteString.Lazy qualified as BSL
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Effects.Agent qualified as AgentProto
import Effects.Log qualified as Log
import ExoMonad.Effects.Agent qualified as ProtoAgent
import ExoMonad.Effects.Events qualified as ProtoEvents
import ExoMonad.Effects.Log (LogEmitEvent)
import ExoMonad.Guest.Tool.Class (MCPCallOutput, MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (JsonSchema (..), genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect, suspendEffect_)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

-- | Notify parent tool (for workers/subtrees to call on completion)
data NotifyParent = NotifyParent

-- | Status for notify_parent tool.
data NotifyStatus = Success | Failure
  deriving (Show, Eq, Generic, JsonSchema)

instance FromJSON NotifyStatus where
  parseJSON = Aeson.withText "NotifyStatus" $ \case
    "success" -> pure Success
    "failure" -> pure Failure
    other -> fail $ "Unknown status: " <> T.unpack other

instance ToJSON NotifyStatus where
  toJSON Success = Aeson.String "success"
  toJSON Failure = Aeson.String "failure"

-- | Structured task report for enriched notifications.
data TaskReport = TaskReport
  { trWhat :: Text,
    trHow :: Text
  }
  deriving (Generic, Show)

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
    npTasksCompleted :: Maybe [TaskReport]
  }
  deriving (Generic, Show)

instance FromJSON NotifyParentArgs where
  parseJSON = withObject "NotifyParentArgs" $ \v ->
    NotifyParentArgs
      <$> v .: "status"
      <*> v .: "message"
      <*> v .:? "pr_number"
      <*> v .:? "tasks_completed"

instance ToJSON NotifyParentArgs where
  toJSON args =
    object
      [ "status" .= npStatus args,
        "message" .= npMessage args,
        "pr_number" .= npPrNumber args,
        "tasks_completed" .= npTasksCompleted args
      ]

-- | Shared tool description for notify_parent.
notifyParentDescription :: Text
notifyParentDescription = "Send a message to your parent agent. Use for status updates, progress reports, or failure escalation. Messages are delivered as-is with lightweight attribution. For PR-based workflows, the system auto-notifies your parent when Copilot approves — you don't need to signal completion yourself."

-- | Shared tool schema for notify_parent.
notifyParentSchema :: Aeson.Object
notifyParentSchema =
  genericToolSchemaWith @NotifyParentArgs
    [ ("status", "'success' = normal message (status update, progress report). 'failure' = escalation, something went wrong."),
      ("message", "The message to send. Be concise — one or two sentences."),
      ("pr_number", "PR number if relevant. Helps parent locate the PR without searching."),
      ("tasks_completed", "Array of {what, how} pairs. 'what' = task description, 'how' = verification command that was run.")
    ]

-- | Core notify_parent I/O: emit event + deliver message to parent.
-- Returns Left on delivery failure, Right () on success.
notifyParentCore :: NotifyParentArgs -> Eff Effects (Either Text ())
notifyParentCore args = do
  -- Emit event via suspend
  let eventPayload =
        BSL.toStrict $
          Aeson.encode $
            object
              [ "status" .= npStatus args,
                "message" .= npMessage args,
                "pr_number" .= npPrNumber args,
                "tasks_completed" .= npTasksCompleted args
              ]
  void $
    suspendEffect_ @LogEmitEvent
      ( Log.EmitEventRequest
          { Log.emitEventRequestEventType = "agent.completed",
            Log.emitEventRequestPayload = eventPayload,
            Log.emitEventRequestTimestamp = 0
          }
      )

  let richMessage = composeNotifyMessage args
  let statusText = case npStatus args of
        Success -> "success" :: Text
        Failure -> "failure"
  result <-
    suspendEffect @ProtoEvents.EventsNotifyParent
      ( ProtoEvents.NotifyParentRequest
          { ProtoEvents.notifyParentRequestAgentId = "",
            ProtoEvents.notifyParentRequestStatus = TL.fromStrict statusText,
            ProtoEvents.notifyParentRequestMessage = TL.fromStrict richMessage,
            ProtoEvents.notifyParentRequestOverrideRecipient = Nothing
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
   in base <> prSuffix <> taskLines

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
sendTmuxMessageDescription = "Send a message to an exomonad-spawned agent by injecting it into that agent's tmux pane. Use this for Codex, OpenCode, Gemini, and any non-Claude runtime, or when you need to steer a live pane directly."

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

-- | Shutdown tool for cooperative agent exit
data Shutdown = Shutdown

data ShutdownArgs = ShutdownArgs
  { sdMessage :: Maybe Text
  }
  deriving (Generic, Show)

instance FromJSON ShutdownArgs where
  parseJSON = withObject "ShutdownArgs" $ \v ->
    ShutdownArgs <$> v .:? "message"

instance ToJSON ShutdownArgs where
  toJSON args = object ["message" .= sdMessage args]

-- | Shared tool description for shutdown.
shutdownDescription :: Text
shutdownDescription = "Gracefully shut down this agent. Sends a final message to your parent, then exits. Call this when instructed to shut down or when your work is complete."

-- | Shared tool schema for shutdown.
shutdownSchema :: Aeson.Object
shutdownSchema =
  genericToolSchemaWith @ShutdownArgs
    [("message", "Optional final message to send to parent before shutting down")]

-- | Core shutdown I/O: notify parent + close self.
shutdownCore :: ShutdownArgs -> Eff Effects MCPCallOutput
shutdownCore args = do
  let msg = maybe "Shutting down." id (sdMessage args)
  let statusText = "success" :: Text
  void $
    suspendEffect @ProtoEvents.EventsNotifyParent
      ( ProtoEvents.NotifyParentRequest
          { ProtoEvents.notifyParentRequestAgentId = "",
            ProtoEvents.notifyParentRequestStatus = TL.fromStrict statusText,
            ProtoEvents.notifyParentRequestMessage = TL.fromStrict msg,
            ProtoEvents.notifyParentRequestOverrideRecipient = Nothing
          }
      )
  void $
    suspendEffect @ProtoAgent.AgentCloseSelf
      ( AgentProto.CloseSelfRequest
          { AgentProto.closeSelfRequestReason = TL.fromStrict msg
          }
      )
  pure $ successResult $ object ["shutdown" .= True]
