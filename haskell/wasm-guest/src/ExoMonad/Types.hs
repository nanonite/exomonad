{-# LANGUAGE DataKinds #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE FlexibleContexts #-}
{-# LANGUAGE TypeOperators #-}

module ExoMonad.Types
  ( RoleConfig (..),
    HookConfig (..),
    Effects,
    EventHandlerConfig,
    defaultEventHandlers,
    defaultHooks,
    ChainlinkDbPathState (..),
    validateChainlinkDbEnv,
    defaultSessionStartHook,
    teamRegistrationPostToolUse,
    andThenPostToolUse,
  )
where

import Control.Monad (void)
import Control.Monad.Freer (Eff)
import Data.Aeson qualified as Aeson
import Data.Aeson.Key qualified as Key
import Data.Aeson.KeyMap qualified as KM
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import ExoMonad.Effects.KV (CleanupStalePhasesRequest (..), KVCleanupStalePhases)
import ExoMonad.Effects.Log (LogError, LogInfo)
import ExoMonad.Effects.Log qualified as Log
import ExoMonad.Effects.Session qualified as Session
import ExoMonad.Guest.Effects.FileSystem qualified as FS
import ExoMonad.Guest.Events (EventHandlerConfig, defaultEventHandlers)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect, suspendEffect_)
import ExoMonad.Guest.Types (AfterModelOutput (..), BeforeModelOutput (..), Effects, HookInput (..), HookOutput (..), HookSpecificOutput (..), StopHookOutput, allowResponse, allowStopResponse, postToolUseResponse)
import GHC.Generics (Generic)
import System.FilePath ((</>))

-- | Role configuration.
-- Defines the role name, available tools, and lifecycle hooks.
data RoleConfig tools = RoleConfig
  { roleName :: Text,
    tools :: tools,
    hooks :: HookConfig,
    eventHandlers :: EventHandlerConfig
  }
  deriving (Generic)

-- | Configuration for lifecycle hooks.
data HookConfig = HookConfig
  { -- | Called before any tool use. Can allow, block, or modify the tool call.
    preToolUse :: HookInput -> Eff Effects HookOutput,
    -- | Called after any tool use. Can inject additional context into the conversation.
    postToolUse :: HookInput -> Eff Effects HookOutput,
    -- | Called when the agent stops (e.g. /stop or session end).
    onStop :: HookInput -> Eff Effects StopHookOutput,
    -- | Called when a sub-agent stops.
    onSubagentStop :: HookInput -> Eff Effects StopHookOutput,
    -- | Called on session start. Default: registers Claude session ID for fork-session.
    onSessionStart :: HookInput -> Eff Effects HookOutput,
    -- | Gemini BeforeModel hook. Can bypass the LLM with a synthetic response.
    beforeModel :: HookInput -> Eff Effects BeforeModelOutput,
    -- | Gemini AfterModel hook. Fires per LLM response chunk, enables rewriting.
    afterModel :: HookInput -> Eff Effects AfterModelOutput
  }

-- | Default hooks that allow everything.
defaultHooks :: HookConfig
defaultHooks =
  HookConfig
    { preToolUse = \_ -> pure (allowResponse Nothing),
      postToolUse = \_ -> pure (postToolUseResponse Nothing),
      onStop = \_ -> pure allowStopResponse,
      onSubagentStop = \_ -> pure allowStopResponse,
      onSessionStart = defaultSessionStartHook,
      beforeModel = \_ -> pure (BeforeModelAllow Nothing),
      afterModel = \_ -> pure (AfterModelAllow Nothing)
    }

-- | Extract the conversation UUID from the transcript path.
-- The transcript path is e.g. "/home/user/.claude/projects/-home-user-project/abc123-def456.jsonl"
-- The UUID is the basename minus the .jsonl extension.
extractUuidFromTranscriptPath :: Text -> Maybe Text
extractUuidFromTranscriptPath path =
  let basename = T.takeWhileEnd (/= '/') path
   in case T.stripSuffix ".jsonl" basename of
        Just uuid | not (T.null uuid) -> Just uuid
        _ -> Nothing

data ChainlinkDbPathState
  = ChainlinkDbPathMissing
  | ChainlinkDbPathFile
  | ChainlinkDbPathDirectory
  deriving (Show, Eq)

validateChainlinkDbEnv :: Maybe Text -> ChainlinkDbPathState -> ChainlinkDbPathState -> Either Text ()
validateChainlinkDbEnv maybeDb dbState issuesState = do
  dbPath <- case T.strip <$> maybeDb of
    Nothing -> Left chainlinkDbUnsetMessage
    Just "" -> Left chainlinkDbUnsetMessage
    Just path -> Right path
  case dbState of
    ChainlinkDbPathMissing -> Left $ chainlinkDbMissingMessage dbPath
    ChainlinkDbPathFile -> Left $ chainlinkDbNotDirectoryMessage dbPath
    ChainlinkDbPathDirectory ->
      case issuesState of
        ChainlinkDbPathFile -> Right ()
        _ -> Left $ chainlinkDbPhantomMessage dbPath

chainlinkDbUnsetMessage :: Text
chainlinkDbUnsetMessage =
  "CHAINLINK_DB not set. ExoMonad spawn env is broken; refusing to start agent. Investigate build_agent_command in services/agent_control/internal.rs."

chainlinkDbMissingMessage :: Text -> Text
chainlinkDbMissingMessage path =
  "CHAINLINK_DB points at a missing path: "
    <> path
    <> ". ExoMonad spawn env is broken; refusing to start agent. Investigate build_agent_command in services/agent_control/internal.rs."

chainlinkDbNotDirectoryMessage :: Text -> Text
chainlinkDbNotDirectoryMessage path =
  "CHAINLINK_DB must point at a directory containing issues.db, got: "
    <> path
    <> ". Refusing to start agent."

chainlinkDbPhantomMessage :: Text -> Text
chainlinkDbPhantomMessage path =
  "CHAINLINK_DB points at a phantom DB directory without issues.db: "
    <> path
    <> ". Refusing to start agent before Chainlink can create a divergent tracker."

sessionStartDenyResponse :: Text -> HookOutput
sessionStartDenyResponse message =
  HookOutput
    { continue_ = False,
      stopReason = Just message,
      suppressOutput = Nothing,
      systemMessage = Just message,
      hookSpecificOutput = Just $ SessionStartOutput {ssAdditionalContext = Just message}
    }

pathStateFromExists :: FS.FileExistsOutput -> ChainlinkDbPathState
pathStateFromExists output
  | not (FS.feoExists output) = ChainlinkDbPathMissing
  | FS.feoIsDirectory output = ChainlinkDbPathDirectory
  | otherwise = ChainlinkDbPathFile

readPathState :: Text -> Eff Effects ChainlinkDbPathState
readPathState path = do
  result <- FS.fileExists path
  pure $ case result of
    Left _ -> ChainlinkDbPathMissing
    Right output -> pathStateFromExists output

checkChainlinkDbEnv :: HookInput -> Eff Effects (Either Text ())
checkChainlinkDbEnv hookInput =
  case T.strip <$> hiChainlinkDb hookInput of
    Nothing -> pure $ validateChainlinkDbEnv Nothing ChainlinkDbPathMissing ChainlinkDbPathMissing
    Just "" -> pure $ validateChainlinkDbEnv (hiChainlinkDb hookInput) ChainlinkDbPathMissing ChainlinkDbPathMissing
    Just dbPath -> do
      dbState <- readPathState dbPath
      issuesState <- readPathState (T.pack (T.unpack dbPath </> "issues.db"))
      pure $ validateChainlinkDbEnv (Just dbPath) dbState issuesState

-- | Default SessionStart hook: first validates the Chainlink DB anchor, then
-- registers Claude conversation UUID and instructs Claude to create a team.
-- Team name registration happens in PostToolUse (teamRegistrationPostToolUse)
-- after TeamCreate returns the actual auto-generated name.
--
-- Uses transcript_path (the .jsonl filename) as the true conversation UUID.
-- The session_id field is an ephemeral execution token that does NOT correspond
-- to the .jsonl file needed for --resume --fork-session.
defaultSessionStartHook :: HookInput -> Eff Effects HookOutput
defaultSessionStartHook hookInput = do
  chainlinkDbCheck <- checkChainlinkDbEnv hookInput
  case chainlinkDbCheck of
    Left message -> pure $ sessionStartDenyResponse message
    Right () -> do
      let claudeUuid = case hiTranscriptPath hookInput >>= extractUuidFromTranscriptPath of
            Just uuid -> uuid
            Nothing -> hiSessionId hookInput
      void $
        suspendEffect_ @Session.SessionRegisterClaudeId
          (Session.RegisterClaudeSessionRequest {Session.registerClaudeSessionRequestClaudeSessionId = TL.fromStrict claudeUuid})
      void $ suspendEffect_ @KVCleanupStalePhases (CleanupStalePhasesRequest {})
      let instruction = "Create a team using TeamCreate before proceeding."
      pure $
        HookOutput
          { continue_ = True,
            stopReason = Nothing,
            suppressOutput = Nothing,
            systemMessage = Nothing,
            hookSpecificOutput = Just $ SessionStartOutput {ssAdditionalContext = Just instruction}
          }

-- | PostToolUse hook that registers the team after TeamCreate completes.
-- Extracts the auto-generated team name from TeamCreate's tool_response
-- and registers it with the server so notify_parent can route via Teams inbox.
teamRegistrationPostToolUse :: HookInput -> Eff Effects HookOutput
teamRegistrationPostToolUse hookInput =
  case hiToolName hookInput of
    Just "TeamCreate" -> do
      -- Log raw response for debugging team registration
      void $
        suspendEffect_ @Log.LogInfo
          ( Log.InfoRequest
              { Log.infoRequestMessage =
                  TL.fromStrict $
                    "[PostToolUse] TeamCreate tool_response: " <> T.pack (show (hiToolResponse hookInput)),
                Log.infoRequestFields = ""
              }
          )
      case extractTeamName (hiToolResponse hookInput) of
        Just teamName -> do
          let inboxName = "team-lead"
          teamResult <-
            suspendEffect @Session.SessionRegisterTeam
              ( Session.RegisterTeamRequest
                  { Session.registerTeamRequestTeamName = TL.fromStrict teamName,
                    Session.registerTeamRequestInboxName = TL.fromStrict inboxName
                  }
              )
          case teamResult of
            Left _err ->
              void $
                suspendEffect_ @Log.LogError
                  ( Log.ErrorRequest
                      { Log.errorRequestMessage = TL.fromStrict $ "[PostToolUse] registerTeam failed for team: " <> teamName,
                        Log.errorRequestFields = ""
                      }
                  )
            Right _ ->
              void $
                suspendEffect_ @Log.LogInfo
                  ( Log.InfoRequest
                      { Log.infoRequestMessage = TL.fromStrict $ "[PostToolUse] Registered team: " <> teamName,
                        Log.infoRequestFields = ""
                      }
                  )
        Nothing ->
          void $
            suspendEffect_ @Log.LogError
              ( Log.ErrorRequest
                  { Log.errorRequestMessage = "[PostToolUse] TeamCreate response missing team_name field",
                    Log.errorRequestFields = ""
                  }
              )
      pure (postToolUseResponse Nothing)
    Just "TeamDelete" -> do
      void $
        suspendEffect_ @Session.SessionDeregisterTeam
          Session.DeregisterTeamRequest
      pure (postToolUseResponse Nothing)
    _ -> pure (postToolUseResponse Nothing)

-- | Extract team_name from TeamCreate's tool_response JSON.
-- TeamCreate returns: {"team_name": "...", "team_file_path": "...", "lead_agent_id": "..."}
extractTeamName :: Maybe Aeson.Value -> Maybe Text
extractTeamName (Just (Aeson.Object obj)) =
  -- Try "team_name" first (documented), then "name" (config.json format)
  case KM.lookup (Key.fromText "team_name") obj of
    Just (Aeson.String name) -> Just name
    _ -> case KM.lookup (Key.fromText "name") obj of
      Just (Aeson.String name) -> Just name
      _ -> Nothing
extractTeamName _ = Nothing

-- | Compose two PostToolUse hooks sequentially.
-- The first runs for side effects only; the second's output is returned.
andThenPostToolUse ::
  (HookInput -> Eff Effects HookOutput) ->
  (HookInput -> Eff Effects HookOutput) ->
  HookInput ->
  Eff Effects HookOutput
andThenPostToolUse first second input = do
  _ <- first input
  second input
