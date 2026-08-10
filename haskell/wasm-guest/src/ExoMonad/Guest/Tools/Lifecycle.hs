{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.Lifecycle
  ( HasPendingWork (..),
    HasPendingWorkArgs (..),
    ShutdownServer (..),
    ShutdownServerArgs (..),
    hasPendingWorkDescription,
    hasPendingWorkSchema,
    hasPendingWorkCore,
    shutdownServerDescription,
    shutdownServerSchema,
    shutdownServerCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), ToJSON (..), Value, object, withObject, (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Agent qualified as AgentProto
import ExoMonad.Effects.Agent qualified as Agent
import ExoMonad.Effects.Lifecycle qualified as Lifecycle
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)
import Proto3.Suite.Types qualified as PBT

data HasPendingWorkArgs = HasPendingWorkArgs
  deriving (Generic, Show)

instance FromJSON HasPendingWorkArgs where
  parseJSON = withObject "HasPendingWorkArgs" $ \_ -> pure HasPendingWorkArgs

instance ToJSON HasPendingWorkArgs where
  toJSON HasPendingWorkArgs = object []

hasPendingWorkDescription :: Text
hasPendingWorkDescription = "Check whether root convergence still has pending work: open Chainlink issues or alive non-root agents."

hasPendingWorkSchema :: Aeson.Object
hasPendingWorkSchema = genericToolSchemaWith @HasPendingWorkArgs []

hasPendingWorkCore :: HasPendingWorkArgs -> Eff Effects (Either Text Value)
hasPendingWorkCore _args = do
  result <- suspendEffect @Lifecycle.LifecycleHasPendingWork Lifecycle.HasPendingWorkEffect {}
  pure $ case result of
    Left err -> Left ("lifecycle.has_pending_work failed: " <> T.pack (show err))
    Right resp ->
      Right $
        object
          [ "has_pending_work" .= Lifecycle.hasPendingWorkResultHasPendingWork resp,
            "open_issue_count" .= Lifecycle.hasPendingWorkResultOpenIssueCount resp,
            "alive_agent_count" .= Lifecycle.hasPendingWorkResultAliveAgentCount resp,
            "alive_agents" .= map agentInfoValue (V.toList (Lifecycle.hasPendingWorkResultAliveAgents resp))
          ]

data ShutdownServerArgs = ShutdownServerArgs
  deriving (Generic, Show)

instance FromJSON ShutdownServerArgs where
  parseJSON = withObject "ShutdownServerArgs" $ \_ -> pure ShutdownServerArgs

instance ToJSON ShutdownServerArgs where
  toJSON ShutdownServerArgs = object []

shutdownServerDescription :: Text
shutdownServerDescription = "Gracefully shut down the shared ExoMonad server after server-side verification finds no alive non-root agents."

shutdownServerSchema :: Aeson.Object
shutdownServerSchema = genericToolSchemaWith @ShutdownServerArgs []

shutdownServerCore :: ShutdownServerArgs -> Eff Effects (Either Text Value)
shutdownServerCore _args = do
  result <- suspendEffect @Lifecycle.LifecycleShutdownServer Lifecycle.ServerShutdownEffect {}
  pure $ case result of
    Left err -> Left ("lifecycle.shutdown_server failed: " <> T.pack (show err))
    Right resp ->
      Right $
        object
          [ "success" .= Lifecycle.serverShutdownResultSuccess resp,
            "error" .= strictText (Lifecycle.serverShutdownResultError resp),
            "message" .= strictText (Lifecycle.serverShutdownResultMessage resp)
          ]

agentInfoValue :: Agent.AgentInfo -> Value
agentInfoValue info =
  object
    [ "agent_id" .= strictText (Agent.agentInfoId info),
      "agent_type" .= agentTypeText (Agent.agentInfoAgentType info),
      "birth_branch" .= strictText (Agent.agentInfoBirthBranch info),
      "has_unread" .= Agent.agentInfoHasUnread info,
      "last_check_inbox_at" .= Agent.agentInfoLastCheckInboxAt info,
      "last_activity_at" .= Agent.agentInfoLastActivityAt info,
      "is_alive" .= Agent.agentInfoIsAlive info
    ]

agentTypeText :: PBT.Enumerated AgentProto.AgentType -> Text
agentTypeText value =
  case PBT.enumerated value of
    Left code -> "unknown:" <> T.pack (show code)
    Right AgentProto.AgentTypeAGENT_TYPE_UNSPECIFIED -> "unspecified"
    Right AgentProto.AgentTypeAGENT_TYPE_CLAUDE -> "claude"
    Right AgentProto.AgentTypeAGENT_TYPE_RETIRED -> "retired"
    Right AgentProto.AgentTypeAGENT_TYPE_SHOAL -> "shoal"
    Right AgentProto.AgentTypeAGENT_TYPE_OPENCODE -> "opencode"
    Right AgentProto.AgentTypeAGENT_TYPE_CODEX -> "codex"

strictText :: TL.Text -> Text
strictText = TL.toStrict

data HasPendingWork = HasPendingWork

instance MCPTool HasPendingWork where
  type ToolArgs HasPendingWork = HasPendingWorkArgs
  toolName = "has_pending_work"
  toolDescription = hasPendingWorkDescription
  toolSchema = hasPendingWorkSchema
  toolHandlerEff args = do
    result <- hasPendingWorkCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value

data ShutdownServer = ShutdownServer

instance MCPTool ShutdownServer where
  type ToolArgs ShutdownServer = ShutdownServerArgs
  toolName = "shutdown_server"
  toolDescription = shutdownServerDescription
  toolSchema = shutdownServerSchema
  toolHandlerEff args = do
    result <- shutdownServerCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value
