{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.CleanupOrphan
  ( CleanupOrphan (..),
    CleanupOrphanArgs (..),
    cleanupOrphanDescription,
    cleanupOrphanSchema,
    cleanupOrphanCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), ToJSON (..), object, withObject, (.!=), (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Agent qualified as PA
import Effects.Session qualified as PS
import ExoMonad.Effects.Agent qualified as Agent
import ExoMonad.Effects.Session qualified as Session
import ExoMonad.Guest.Proto (fromText)
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Tools.Spawn (spawnErrorMessage)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

newtype CleanupOrphan = CleanupOrphan ()

data CleanupOrphanArgs = CleanupOrphanArgs
  { coaName :: Text,
    coaDryRun :: Bool
  }
  deriving (Generic, Show)

instance FromJSON CleanupOrphanArgs where
  parseJSON = withObject "CleanupOrphanArgs" $ \v ->
    CleanupOrphanArgs
      <$> v .: "name"
      <*> v .:? "dry_run" .!= False

instance ToJSON CleanupOrphanArgs where
  toJSON args = object ["name" .= coaName args, "dry_run" .= coaDryRun args]

cleanupOrphanDescription :: Text
cleanupOrphanDescription = "Remove stale resources for an orphan agent whose tmux window or pane is no longer alive. Refuses to clean live agents. Use dry_run=true to inspect what would be removed."

cleanupOrphanSchema :: Aeson.Object
cleanupOrphanSchema =
  genericToolSchemaWith @CleanupOrphanArgs
    [ ("name", "Agent slug to clean up, as shown by session_status"),
      ("dry_run", "When true, report what would be removed without removing it. Defaults to false.")
    ]

cleanupOrphanCore :: CleanupOrphanArgs -> Eff Effects (Either Text Aeson.Value)
cleanupOrphanCore args
  | T.null (T.strip (coaName args)) = pure $ Left "name is required"
  | otherwise = do
      statusResult <- listAgents
      case statusResult of
        Left err -> pure $ Left err
        Right agents -> case findAgent agents (coaName args) of
          Just agent
            | PS.agentStatusWindowAlive agent ->
                pure $ Left ("Agent " <> coaName args <> " window is still alive. Use dispose_leaf to close it gracefully.")
          found
            | coaDryRun args -> pure $ Right (dryRunOutput args found)
            | otherwise -> disposeOrphan args found

listAgents :: Eff Effects (Either Text [PS.AgentStatus])
listAgents = do
  let req = PS.ListAgentsRequest {PS.listAgentsRequestIncludeDead = True}
  result <- suspendEffect @Session.SessionListAgents req
  pure $ case result of
    Left err -> Left (spawnErrorMessage err)
    Right resp -> Right (V.toList (PS.listAgentsResponseAgents resp))

findAgent :: [PS.AgentStatus] -> Text -> Maybe PS.AgentStatus
findAgent agents name =
  let needle = T.strip name
   in case filter (\agent -> lazyText (PS.agentStatusName agent) == needle) agents of
        [] -> Nothing
        (agent : _) -> Just agent

dryRunOutput :: CleanupOrphanArgs -> Maybe PS.AgentStatus -> Aeson.Value
dryRunOutput args found =
  object
    [ "success" .= True,
      "dry_run" .= True,
      "agent" .= coaName args,
      "found" .= maybe False (const True) found,
      "window_alive" .= maybe False PS.agentStatusWindowAlive found,
      "would_remove" .= (not (maybe False PS.agentStatusWindowAlive found))
    ]

disposeOrphan :: CleanupOrphanArgs -> Maybe PS.AgentStatus -> Eff Effects (Either Text Aeson.Value)
disposeOrphan args found = do
  let req = PA.DisposeOrphanRequest {PA.disposeOrphanRequestAgentSlug = fromText (coaName args)}
  result <- suspendEffect @Agent.AgentDisposeOrphan req
  pure $ case result of
    Left err -> Left (spawnErrorMessage err)
    Right resp ->
      Right $
        object
          [ "success" .= True,
            "agent" .= coaName args,
            "removed_worktree" .= PA.disposeOrphanResponseRemovedWorktree resp,
            "removed_agent_dir" .= PA.disposeOrphanResponseRemovedAgentDir resp,
            "message" .= lazyText (PA.disposeOrphanResponseMessage resp),
            "was_listed" .= maybe False (const True) found
          ]

lazyText :: TL.Text -> Text
lazyText = TL.toStrict

instance MCPTool CleanupOrphan where
  type ToolArgs CleanupOrphan = CleanupOrphanArgs
  toolName = "cleanup_orphan"
  toolDescription = cleanupOrphanDescription
  toolSchema = cleanupOrphanSchema
  toolHandlerEff args = do
    result <- cleanupOrphanCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value
