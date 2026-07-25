{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.Agents
  ( ListAgents (..),
    ListAgentsArgs (..),
    listAgentsDescription,
    listAgentsSchema,
    listAgentsCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), ToJSON (..), Value, object, withObject, (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Agent qualified as AgentProto
import ExoMonad.Effects.Agent qualified as Agent
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)
import Proto3.Suite.Class qualified as PB
import Proto3.Suite.Types qualified as PBT

newtype ListAgentsArgs = ListAgentsArgs
  { laFilterType :: Maybe Text
  }
  deriving (Generic, Show)

instance FromJSON ListAgentsArgs where
  parseJSON = withObject "ListAgentsArgs" $ \v ->
    ListAgentsArgs <$> v .:? "filter_type"

instance ToJSON ListAgentsArgs where
  toJSON args = object ["filter_type" .= laFilterType args]

listAgentsDescription :: Text
listAgentsDescription = "List known ExoMonad agents with type, birth branch, unread inbox state, last inbox check time, and liveness. Optionally filter by agent type."

listAgentsSchema :: Aeson.Object
listAgentsSchema =
  genericToolSchemaWith @ListAgentsArgs
    [("filter_type", "Optional agent type filter, such as claude, gemini, opencode, codex, or shoal.")]

listAgentsCore :: ListAgentsArgs -> Eff Effects (Either Text Value)
listAgentsCore args = do
  result <-
    suspendEffect @Agent.AgentList
      ( Agent.ListRequest
          { Agent.listRequestFilterAliveOnly = False,
            Agent.listRequestFilterRole = PB.def,
            Agent.listRequestSubrepo = "",
            Agent.listRequestFilterType = maybe "" TL.fromStrict (laFilterType args)
          }
      )
  pure $ case result of
    Left err -> Left ("agent.list failed: " <> T.pack (show err))
    Right resp ->
      let agents = V.toList (Agent.listResponseAgents resp)
       in Right $
            object
              [ "count" .= length agents,
                "agents" .= map agentInfoValue agents
              ]

agentInfoValue :: Agent.AgentInfo -> Value
agentInfoValue info =
  object
    [ "agent_id" .= strictText (Agent.agentInfoId info),
      "agent_type" .= agentTypeText (Agent.agentInfoAgentType info),
      "birth_branch" .= strictText (Agent.agentInfoBirthBranch info),
      "has_unread" .= Agent.agentInfoHasUnread info,
      "last_check_inbox_at" .= Agent.agentInfoLastCheckInboxAt info,
      "is_alive" .= Agent.agentInfoIsAlive info
    ]

agentTypeText :: PBT.Enumerated AgentProto.AgentType -> Text
agentTypeText value =
  case PBT.enumerated value of
    Left code -> "unknown:" <> T.pack (show code)
    Right AgentProto.AgentTypeAGENT_TYPE_UNSPECIFIED -> "unspecified"
    Right AgentProto.AgentTypeAGENT_TYPE_CLAUDE -> "claude"
    Right AgentProto.AgentTypeAGENT_TYPE_GEMINI -> "gemini"
    Right AgentProto.AgentTypeAGENT_TYPE_SHOAL -> "shoal"
    Right AgentProto.AgentTypeAGENT_TYPE_OPENCODE -> "opencode"
    Right AgentProto.AgentTypeAGENT_TYPE_CODEX -> "codex"

strictText :: TL.Text -> Text
strictText = TL.toStrict

data ListAgents = ListAgents

instance MCPTool ListAgents where
  type ToolArgs ListAgents = ListAgentsArgs
  toolName = "list_agents"
  toolDescription = listAgentsDescription
  toolSchema = listAgentsSchema
  toolHandlerEff args = do
    result <- listAgentsCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value
