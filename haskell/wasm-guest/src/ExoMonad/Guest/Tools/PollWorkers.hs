{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.PollWorkers
  ( PollWorkers (..),
    PollWorkersArgs (..),
    pollWorkersDescription,
    pollWorkersSchema,
    pollWorkersCore,
  )
where

import Control.Monad (forM)
import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), Result (..), Value (..), fromJSON, object, withObject, (.!=), (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Aeson.Key (Key)
import Data.Aeson.KeyMap qualified as KM
import Data.List (nub)
import Data.Maybe (mapMaybe)
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Session qualified as PS
import ExoMonad.Effects.Session qualified as Session
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Tools.Chainlink (ChainlinkIssueShowArgs (..), chainlinkIssueShowCore, chainlinkSessionStatusCore)
import ExoMonad.Guest.Tools.Chainlink.Pure (ChainlinkIssueShowOutput (..))
import ExoMonad.Guest.Tools.Spawn (spawnErrorMessage)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)
import Text.Read (readMaybe)

data PollWorkersArgs = PollWorkersArgs
  { pwaIncludeDead :: Bool,
    pwaAgents :: Maybe [Text]
  }
  deriving (Generic, Show)

instance FromJSON PollWorkersArgs where
  parseJSON = withObject "PollWorkersArgs" $ \v ->
    PollWorkersArgs
      <$> v .:? "include_dead" .!= True
      <*> v .:? "agents"

pollWorkersDescription :: Text
pollWorkersDescription =
  "Poll spawned ExoMonad agents and workers for TL-owned liveness checks. Returns per-agent pane/window liveness, Chainlink session state, active issue status, age, routing target, and birth branch. Call once after spawning a wave or worker before idling; do not busy-wait."

pollWorkersSchema :: Aeson.Object
pollWorkersSchema =
  genericToolSchemaWith @PollWorkersArgs
    [ ("include_dead", "Include agents whose tmux target is confirmed dead. Defaults to true so stale routing is visible."),
      ("agents", "Optional list of agent slugs to poll. Omit to poll all known spawned agents.")
    ]

pollWorkersCore :: PollWorkersArgs -> Eff Effects (Either Text Value)
pollWorkersCore args = do
  let req = PS.ListAgentsRequest {PS.listAgentsRequestIncludeDead = pwaIncludeDead args}
  agentsResult <- suspendEffect @Session.SessionListAgents req
  case agentsResult of
    Left err -> pure $ Left (spawnErrorMessage err)
    Right resp -> do
      sessionResult <- chainlinkSessionStatusCore
      let sessionValue = either (String . ("ERROR: " <>)) id sessionResult
          activeSessionIssue = either (const Nothing) activeIssueId sessionResult
          agents = filterSelected (pwaAgents args) (V.toList (PS.listAgentsResponseAgents resp))
      rows <- forM agents (agentPollRow activeSessionIssue)
      pure $
        Right $
          object
            [ "table" .= renderWorkersTable rows,
              "workers" .= rows,
              "chainlink_session" .= sessionValue,
              "dead_workers" .= mapMaybe deadWorkerName rows,
              "stale_workers" .= mapMaybe staleWorkerName rows,
              "note" .= pollWorkersNote rows
            ]

data ChainlinkSessionSnapshot = ChainlinkSessionSnapshot
  { cssActiveIssue :: Maybe ActiveIssueSnapshot
  }

data ActiveIssueSnapshot = ActiveIssueSnapshot
  { aisIssueId :: Int
  }

instance FromJSON ChainlinkSessionSnapshot where
  parseJSON = withObject "ChainlinkSessionSnapshot" $ \v ->
    ChainlinkSessionSnapshot <$> v .:? "active_issue"

instance FromJSON ActiveIssueSnapshot where
  parseJSON = withObject "ActiveIssueSnapshot" $ \v ->
    ActiveIssueSnapshot <$> v .: "id"

activeIssueId :: Value -> Maybe Int
activeIssueId value =
  case fromJSON value of
    Success snapshot -> aisIssueId <$> cssActiveIssue snapshot
    Error _ -> Nothing

agentPollRow :: Maybe Int -> PS.AgentStatus -> Eff Effects Value
agentPollRow activeSessionIssue agent = do
  issueResult <- lookupIssue (agentIssueId agent)
  let issueStatus = either (const Nothing) (Just . cisoStatus) issueResult
      issueError = either Just (const Nothing) issueResult
      sessionState = chainlinkSessionState activeSessionIssue (agentIssueId agent) issueStatus issueError
      alive = PS.agentStatusWindowAlive agent
  pure $
    object
      [ "name" .= strictField PS.agentStatusName agent,
        "role" .= strictField PS.agentStatusRole agent,
        "active_issue" .= strictField PS.agentStatusIssue agent,
        "issue_status" .= maybeText issueStatus,
        "issue_title" .= either (const ("" :: Text)) cisoTitle issueResult,
        "issue_priority" .= either (const Nothing) cisoPriority issueResult,
        "issue_labels" .= either (const ([] :: [Text])) cisoLabels issueResult,
        "issue_error" .= maybeText issueError,
        "chainlink_session_state" .= sessionState,
        "window_id" .= strictField PS.agentStatusWindowId agent,
        "pane_id" .= strictField PS.agentStatusPaneId agent,
        "pane_alive" .= alive,
        "age_mins" .= PS.agentStatusAgeMins agent,
        "birth_branch" .= strictField PS.agentStatusBirthBranch agent,
        "lifecycle_status" .= strictField PS.agentStatusLifecycleStatus agent
      ]

lookupIssue :: Maybe Int -> Eff Effects (Either Text ChainlinkIssueShowOutput)
lookupIssue Nothing = pure $ Left "no active issue"
lookupIssue (Just issueId) = chainlinkIssueShowCore (ChainlinkIssueShowArgs issueId)

agentIssueId :: PS.AgentStatus -> Maybe Int
agentIssueId agent =
  readMaybe (T.unpack (T.strip (strictField PS.agentStatusIssue agent)))

chainlinkSessionState :: Maybe Int -> Maybe Int -> Maybe Text -> Maybe Text -> Text
chainlinkSessionState activeSessionIssue maybeIssue maybeIssueStatus maybeIssueError =
  case (maybeIssue, maybeIssueStatus, maybeIssueError) of
    (Nothing, _, _) -> "no_active_issue"
    (Just _, _, Just _) -> "issue_lookup_failed"
    (Just issueId, Just status, _)
      | T.toLower status == "closed" -> "issue_closed"
      | Just issueId == activeSessionIssue -> "active_in_current_session"
      | otherwise -> "issue_" <> T.toLower status
    (Just issueId, Nothing, _)
      | Just issueId == activeSessionIssue -> "active_in_current_session"
      | otherwise -> "issue_unknown"

filterSelected :: Maybe [Text] -> [PS.AgentStatus] -> [PS.AgentStatus]
filterSelected Nothing agents = agents
filterSelected (Just names) agents = filter ((`elem` nub names) . strictField PS.agentStatusName) agents

renderWorkersTable :: [Value] -> Text
renderWorkersTable rows =
  T.unlines $
    renderRow ["AGENT", "ROLE", "ISSUE", "PANE", "ALIVE", "AGE", "SESSION", "ISSUE_STATUS"]
      : map renderWorkerRow rows

renderWorkerRow :: Value -> Text
renderWorkerRow (Object row) =
  renderRow
    [ textField "name" row,
      textField "role" row,
      dashIfEmpty (textField "active_issue" row),
      dashIfEmpty (textField "pane_id" row),
      if boolField "pane_alive" row then "yes" else "NO",
      numberField "age_mins" row <> "m",
      textField "chainlink_session_state" row,
      dashIfEmpty (textField "issue_status" row)
    ]
renderWorkerRow _ = ""

renderRow :: [Text] -> Text
renderRow fields = T.intercalate "  " (zipWith pad widths fields)
  where
    widths = [34, 9, 7, 10, 6, 6, 27, 12]

pad :: Int -> Text -> Text
pad width value = value <> T.replicate (max 0 (width - T.length value)) " "

pollWorkersNote :: [Value] -> Text
pollWorkersNote rows
  | any isDead rows = "Some workers have dead tmux targets. Re-spec, close_worker_pane, or respawn instead of waiting silently."
  | any isStale rows = "Some workers have been alive for 60+ minutes. Inspect Chainlink issue/session state before idling again."
  | otherwise = ""

deadWorkerName :: Value -> Maybe Text
deadWorkerName row
  | isDead row = Just (valueText "name" row)
  | otherwise = Nothing

staleWorkerName :: Value -> Maybe Text
staleWorkerName row
  | isStale row = Just (valueText "name" row)
  | otherwise = Nothing

isDead :: Value -> Bool
isDead row = not (valueBool "pane_alive" row)

isStale :: Value -> Bool
isStale row = valueBool "pane_alive" row && valueWord "age_mins" row >= 60

strictField :: (PS.AgentStatus -> TL.Text) -> PS.AgentStatus -> Text
strictField getter = TL.toStrict . getter

maybeText :: Maybe Text -> Text
maybeText = maybe "" id

dashIfEmpty :: Text -> Text
dashIfEmpty value
  | T.null value = "-"
  | otherwise = value

textField :: Key -> KM.KeyMap Value -> Text
textField key row = case KM.lookup key row of
  Just (String value) -> value
  _ -> ""

boolField :: Key -> KM.KeyMap Value -> Bool
boolField key row = case KM.lookup key row of
  Just (Bool value) -> value
  _ -> False

numberField :: Key -> KM.KeyMap Value -> Text
numberField key row = case KM.lookup key row of
  Just (Number value) -> T.pack (show (floor value :: Integer))
  _ -> "0"

valueText :: Key -> Value -> Text
valueText key (Object row) = textField key row
valueText _ _ = ""

valueBool :: Key -> Value -> Bool
valueBool key (Object row) = boolField key row
valueBool _ _ = False

valueWord :: Key -> Value -> Word
valueWord key (Object row) = case KM.lookup key row of
  Just (Number value) -> floor value
  _ -> 0
valueWord _ _ = 0

data PollWorkers

instance MCPTool PollWorkers where
  type ToolArgs PollWorkers = PollWorkersArgs
  toolName = "poll_workers"
  toolDescription = pollWorkersDescription
  toolSchema = pollWorkersSchema
  toolHandlerEff args = do
    result <- pollWorkersCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value
