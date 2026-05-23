{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.SessionStatus
  ( SessionStatus (..),
    SessionStatusArgs (..),
    sessionStatusDescription,
    sessionStatusSchema,
    sessionStatusCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), object, withObject, (.!=), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Session qualified as PS
import ExoMonad.Effects.Session qualified as Session
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Tools.Spawn (spawnErrorMessage)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

newtype SessionStatusArgs = SessionStatusArgs
  { ssaIncludeDead :: Bool
  }
  deriving (Generic, Show)

instance FromJSON SessionStatusArgs where
  parseJSON = withObject "SessionStatusArgs" $ \v ->
    SessionStatusArgs <$> v .:? "include_dead" .!= False

sessionStatusDescription :: Text
sessionStatusDescription = "List known ExoMonad agents with tmux liveness, issue, age, routing target, and birth branch. Pass include_dead=true to show stale registry entries whose windows or panes are gone."

sessionStatusSchema :: Aeson.Object
sessionStatusSchema =
  genericToolSchemaWith @SessionStatusArgs
    [("include_dead", "Include agents whose tmux window or pane is confirmed dead. Defaults to false.")]

sessionStatusCore :: SessionStatusArgs -> Eff Effects (Either Text Aeson.Value)
sessionStatusCore args = do
  let req = PS.ListAgentsRequest {PS.listAgentsRequestIncludeDead = ssaIncludeDead args}
  result <- suspendEffect @Session.SessionListAgents req
  pure $ case result of
    Left err -> Left (spawnErrorMessage err)
    Right resp ->
      let agents = V.toList (PS.listAgentsResponseAgents resp)
       in Right $
            object
              [ "table" .= renderAgentsTable agents,
                "agents" .= agents,
                "dead_agents" .= map agentNameText (filter (not . PS.agentStatusWindowAlive) agents),
                "note" .= deadAgentNote agents
              ]

data SessionStatus

instance MCPTool SessionStatus where
  type ToolArgs SessionStatus = SessionStatusArgs
  toolName = "session_status"
  toolDescription = sessionStatusDescription
  toolSchema = sessionStatusSchema
  toolHandlerEff args = do
    result <- sessionStatusCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value

renderAgentsTable :: [PS.AgentStatus] -> Text
renderAgentsTable agents =
  T.unlines $
    [renderRow ["AGENT", "ROLE", "ISSUE", "TARGET", "ALIVE", "AGE", "STATUS"]]
      <> map renderAgentRow agents

renderAgentRow :: PS.AgentStatus -> Text
renderAgentRow agent =
  renderRow
    [ agentNameText agent,
      lazyField PS.agentStatusRole agent,
      dashIfEmpty (lazyField PS.agentStatusIssue agent),
      agentTarget agent,
      if PS.agentStatusWindowAlive agent then "yes" else "NO",
      T.pack (show (PS.agentStatusAgeMins agent)) <> "m",
      agentLifecycleStatus agent
    ]

renderRow :: [Text] -> Text
renderRow fields = T.intercalate "  " (zipWith pad widths fields)
  where
    widths = [34, 9, 7, 8, 6, 6, 10]

pad :: Int -> Text -> Text
pad width value = value <> T.replicate (max 0 (width - T.length value)) " "

agentTarget :: PS.AgentStatus -> Text
agentTarget agent =
  case (lazyField PS.agentStatusWindowId agent, lazyField PS.agentStatusPaneId agent) of
    (windowId, _) | not (T.null windowId) -> windowId
    (_, paneId) | not (T.null paneId) -> paneId
    _ -> "-"

agentLifecycleStatus :: PS.AgentStatus -> Text
agentLifecycleStatus agent
  | PS.agentStatusWindowAlive agent = "LIVE"
  | not (T.null (lazyField PS.agentStatusIssue agent)) = "FINISHING"
  | otherwise = "ORPHAN"

deadAgentNote :: [PS.AgentStatus] -> Text
deadAgentNote agents
  | null dead = ""
  | otherwise = "Dead agents are safe to inspect before cleanup. FINISHING has an issue; ORPHAN has no issue and needs cleanup_orphan."
  where
    dead = filter (not . PS.agentStatusWindowAlive) agents

agentNameText :: PS.AgentStatus -> Text
agentNameText = lazyField PS.agentStatusName

lazyField :: (PS.AgentStatus -> TL.Text) -> PS.AgentStatus -> Text
lazyField getter = TL.toStrict . getter

dashIfEmpty :: Text -> Text
dashIfEmpty value
  | T.null value = "-"
  | otherwise = value
