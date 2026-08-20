{-# LANGUAGE DataKinds #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE FlexibleContexts #-}
{-# LANGUAGE GADTs #-}
{-# LANGUAGE LambdaCase #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeOperators #-}

-- | High-level agent control effects.
--
-- These effects provide semantic operations for agent lifecycle management.
-- The Rust host handles all I/O (git, tmux, filesystem) via yield_effect.
module ExoMonad.Guest.Effects.AgentControl
  ( -- * Effect type
    AgentControl (..),

    -- * Smart constructors
    spawnSubtree,
    spawnLeafSubtree,
    resumePr,
    spawnWorker,
    closeWorkerPane,

    -- * Interpreters
    runAgentControlSuspend,

    -- * Types
    AgentType (..),
    SpawnResult (..),
    PermissionFlags (..),
    defaultPermFlags,
    SpawnSubtreeConfig (..),
    SpawnLeafSubtreeConfig (..),
    ResumePrConfig (..),
    SpawnWorkerConfig (..),

    -- * Helpers
    agentTypeLabel,
  )
where

import Control.Monad.Freer (Eff, Member, interpret, send)
import Data.Aeson (FromJSON (..), ToJSON (..), Value, object, withObject, withText, (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Maybe (fromMaybe)
import Data.Text (Text)
import Data.Text qualified as T
import Data.Vector qualified as V
import Data.Word (Word64)
import Effects.Agent qualified as PA
import Effects.EffectError (EffectError (..), EffectErrorKind (..), InvalidInput (..))
import ExoMonad.Effects.Agent qualified as Agent
import ExoMonad.Guest.Proto (fromText, toText)
import ExoMonad.Guest.Tool.Schema (JsonSchema (..))
import ExoMonad.Guest.Tool.Suspend.Types (SuspendYield)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Types.Permissions
import GHC.Generics (Generic)
import Proto3.Suite.Types (Enumerated (..))

-- ============================================================================
-- Types
-- ============================================================================

-- | Agent type for spawned agents.
data AgentType = Claude | Retired | Shoal | OpenCode | Codex
  deriving (Show, Eq, Generic)

instance ToJSON AgentType where
  toJSON Claude = "claude"
  toJSON Retired = "retired"
  toJSON Shoal = "shoal"
  toJSON OpenCode = "opencode"
  toJSON Codex = "codex"

instance FromJSON AgentType where
  parseJSON = withText "AgentType" $ \value ->
    let retiredName = T.concat ["ge", "mini"]
     in case value of
          "claude" -> pure Claude
          value' | value' == retiredName -> pure Retired
          "retired" -> pure Retired
          "shoal" -> pure Shoal
          "opencode" -> pure OpenCode
          "codex" -> pure Codex
          other -> fail $ "Invalid agent type: " <> T.unpack other

instance JsonSchema AgentType where
  toSchema =
    object
      [ "type" .= ("string" :: Text),
        "enum" .= (["claude", "shoal", "opencode", "codex"] :: [Text])
      ]

-- | Result of spawning an agent.
data SpawnResult = SpawnResult
  { worktreePath :: Text,
    branchName :: Text,
    tabName :: Text,
    issueTitle :: Text,
    agentTypeResult :: Text,
    paneId :: Maybe Text,
    invocationId :: Maybe Text,
    invocationTrigger :: Maybe Text,
    invocationRuntime :: Maybe Text,
    routingTargetType :: Maybe Text,
    routingTargetId :: Maybe Text,
    invocationFresh :: Maybe Bool,
    invocationReady :: Maybe Bool,
    invocationOutcome :: Maybe Text
  }
  deriving (Show, Eq, Generic)

instance FromJSON SpawnResult where
  parseJSON = withObject "SpawnResult" $ \v ->
    SpawnResult
      <$> v .: "worktree_path"
      <*> v .: "branch_name"
      <*> v .: "tab_name"
      <*> v .: "issue_title"
      <*> v .: "agent_type"
      <*> v .:? "pane_id"
      <*> v .:? "invocation_id"
      <*> v .:? "invocation_trigger"
      <*> v .:? "invocation_runtime"
      <*> v .:? "routing_target_type"
      <*> v .:? "routing_target_id"
      <*> v .:? "invocation_fresh"
      <*> v .:? "invocation_ready"
      <*> v .:? "invocation_outcome"

instance ToJSON SpawnResult where
  toJSON
    ( SpawnResult
        w
        b
        t
        i
        a
        p
        invocationId
        invocationTrigger
        invocationRuntime
        routingTargetType
        routingTargetId
        invocationFresh
        invocationReady
        invocationOutcome
      ) =
      object
        [ "worktree_path" .= w,
          "branch_name" .= b,
          "tab_name" .= t,
          "issue_title" .= i,
          "agent_type" .= a,
          "pane_id" .= p,
          "invocation_id" .= invocationId,
          "invocation_trigger" .= invocationTrigger,
          "invocation_runtime" .= invocationRuntime,
          "routing_target_type" .= routingTargetType,
          "routing_target_id" .= routingTargetId,
          "invocation_fresh" .= invocationFresh,
          "invocation_ready" .= invocationReady,
          "invocation_outcome" .= invocationOutcome
        ]

-- ============================================================================
-- Effect type
-- ============================================================================

-- | Permission flags for spawned agents.
data PermissionFlags = PermissionFlags
  { permMode :: Maybe Text,
    allowedTools :: [Text],
    disallowedTools :: [Text]
  }
  deriving (Show, Eq, Generic)

-- | Default permission flags (no restrictions, backwards compat).
defaultPermFlags :: PermissionFlags
defaultPermFlags = PermissionFlags Nothing [] []

-- | Configuration for spawning a Claude subtree agent.
data SpawnSubtreeConfig = SpawnSubtreeConfig
  { stcTask :: Text,
    stcBranchName :: Text,
    stcForkSession :: Bool,
    stcRole :: Maybe Text,
    stcAgentType :: Maybe AgentType,
    stcPerms :: PermissionFlags,
    stcWorkingDir :: Maybe Text,
    stcPermissions :: Maybe ClaudePermissions,
    stcStandaloneRepo :: Bool,
    stcAllowedDirs :: [Text]
  }
  deriving (Show, Eq, Generic)

data SpawnLeafSubtreeConfig = SpawnLeafSubtreeConfig
  { slcTask :: Text,
    slcBranchName :: Text,
    slcIntentId :: Maybe Text,
    slcRole :: Maybe Text,
    slcAgentType :: Maybe AgentType,
    slcModel :: Maybe Text,
    slcPerms :: PermissionFlags,
    slcStandaloneRepo :: Bool,
    slcAllowedDirs :: [Text]
  }
  deriving (Show, Eq, Generic)

-- | Typed target for resuming an existing open pull request.
-- The host resolves the owning branch, slug, and runtime from PR state.
data ResumePrConfig = ResumePrConfig
  { rpcTask :: Text,
    rpcPrNumber :: Word64,
    rpcExpectedHeadSha :: Text,
    rpcModel :: Maybe Text
  }
  deriving (Show, Eq, Generic)

-- | Configuration for spawning a worker agent.
data SpawnWorkerConfig = SpawnWorkerConfig
  { swcName :: Text,
    swcPrompt :: Text,
    swcIntentId :: Maybe Text,
    swcAgentType :: Maybe AgentType,
    swcModel :: Maybe Text,
    swcPerms :: PermissionFlags
  }
  deriving (Show, Eq, Generic)

-- | Agent control effect for spawning agents.
data AgentControl a where
  SpawnSubtreeC :: SpawnSubtreeConfig -> AgentControl (Either EffectError SpawnResult)
  SpawnLeafSubtreeC :: SpawnLeafSubtreeConfig -> AgentControl (Either EffectError SpawnResult)
  ResumePrC :: ResumePrConfig -> AgentControl (Either EffectError SpawnResult)
  SpawnWorkerC :: SpawnWorkerConfig -> AgentControl (Either EffectError SpawnResult)
  CloseWorkerPaneC :: Text -> AgentControl (Either EffectError PA.CloseWorkerPaneResponse)

-- Smart constructors (manually written - makeSem doesn't work with WASM cross-compilation)
spawnSubtree :: (Member AgentControl r) => SpawnSubtreeConfig -> Eff r (Either EffectError SpawnResult)
spawnSubtree cfg = send (SpawnSubtreeC cfg)

spawnLeafSubtree :: (Member AgentControl r) => SpawnLeafSubtreeConfig -> Eff r (Either EffectError SpawnResult)
spawnLeafSubtree cfg = send (SpawnLeafSubtreeC cfg)

resumePr :: (Member AgentControl r) => ResumePrConfig -> Eff r (Either EffectError SpawnResult)
resumePr cfg = send (ResumePrC cfg)

spawnWorker :: (Member AgentControl r) => SpawnWorkerConfig -> Eff r (Either EffectError SpawnResult)
spawnWorker cfg = send (SpawnWorkerC cfg)

closeWorkerPane :: (Member AgentControl r) => Text -> Eff r (Either EffectError PA.CloseWorkerPaneResponse)
closeWorkerPane pane = send (CloseWorkerPaneC pane)

-- ============================================================================
-- Interpreter (uses yield_effect via Effect typeclass)
-- ============================================================================

-- | Interpret AgentControl via coroutine suspend (trampoline path).
-- Effects dispatched async without holding the WASM plugin lock.
runAgentControlSuspend :: (Member SuspendYield r) => Eff (AgentControl ': r) a -> Eff r a
runAgentControlSuspend = interpret $ \case
  SpawnSubtreeC cfg -> do
    let req =
          PA.SpawnSubtreeRequest
            { PA.spawnSubtreeRequestTask = fromText (stcTask cfg),
              PA.spawnSubtreeRequestBranchName = fromText (stcBranchName cfg),
              PA.spawnSubtreeRequestParentSessionId = fromText "",
              PA.spawnSubtreeRequestForkSession = stcForkSession cfg,
              PA.spawnSubtreeRequestRole = fromText (fromMaybe "" (stcRole cfg)),
              PA.spawnSubtreeRequestAgentType = Enumerated (Right (maybe PA.AgentTypeAGENT_TYPE_UNSPECIFIED toProtoAgentType (stcAgentType cfg))),
              PA.spawnSubtreeRequestPermissionMode = fromText (fromMaybe "" (permMode (stcPerms cfg))),
              PA.spawnSubtreeRequestAllowedTools = V.fromList (map fromText (allowedTools (stcPerms cfg))),
              PA.spawnSubtreeRequestDisallowedTools = V.fromList (map fromText (disallowedTools (stcPerms cfg))),
              PA.spawnSubtreeRequestWorkingDir = fromText (fromMaybe "" (stcWorkingDir cfg)),
              PA.spawnSubtreeRequestPermissions = fmap permissionsToProto (stcPermissions cfg),
              PA.spawnSubtreeRequestStandaloneRepo = stcStandaloneRepo cfg,
              PA.spawnSubtreeRequestAllowedDirs = V.fromList (map fromText (stcAllowedDirs cfg))
            }
    result <- suspendEffect @Agent.AgentSpawnSubtree req
    pure $ case result of
      Left err -> Left err
      Right resp -> case PA.spawnSubtreeResponseAgent resp of
        Nothing -> Left (EffectError (Just (EffectErrorKindInvalidInput (InvalidInput "SpawnSubtree succeeded but no agent info returned"))))
        Just info -> Right (protoAgentInfoToSpawnResult info)
  SpawnLeafSubtreeC cfg -> do
    let req =
          PA.SpawnLeafSubtreeRequest
            { PA.spawnLeafSubtreeRequestTask = fromText (slcTask cfg),
              PA.spawnLeafSubtreeRequestBranchName = fromText (slcBranchName cfg),
              PA.spawnLeafSubtreeRequestRole = fromText (fromMaybe "" (slcRole cfg)),
              PA.spawnLeafSubtreeRequestAgentType = Enumerated (Right (maybe PA.AgentTypeAGENT_TYPE_UNSPECIFIED toProtoAgentType (slcAgentType cfg))),
              PA.spawnLeafSubtreeRequestModel = fromText (fromMaybe "" (slcModel cfg)),
              PA.spawnLeafSubtreeRequestPermissionMode = fromText (fromMaybe "" (permMode (slcPerms cfg))),
              PA.spawnLeafSubtreeRequestAllowedTools = V.fromList (map fromText (allowedTools (slcPerms cfg))),
              PA.spawnLeafSubtreeRequestDisallowedTools = V.fromList (map fromText (disallowedTools (slcPerms cfg))),
              PA.spawnLeafSubtreeRequestStandaloneRepo = slcStandaloneRepo cfg,
              PA.spawnLeafSubtreeRequestAllowedDirs = V.fromList (map fromText (slcAllowedDirs cfg)),
              PA.spawnLeafSubtreeRequestResumePrNumber = 0,
              PA.spawnLeafSubtreeRequestExpectedHeadSha = fromText "",
              PA.spawnLeafSubtreeRequestIntentId = fromText (fromMaybe "" (slcIntentId cfg))
            }
    result <- suspendEffect @Agent.AgentSpawnLeafSubtree req
    pure $ case result of
      Left err -> Left err
      Right resp -> case PA.spawnLeafSubtreeResponseAgent resp of
        Nothing -> Left (EffectError (Just (EffectErrorKindInvalidInput (InvalidInput "SpawnLeafSubtree succeeded but no agent info returned"))))
        Just info -> Right (protoAgentInfoToSpawnResult info)
  ResumePrC cfg -> do
    let req =
          PA.SpawnLeafSubtreeRequest
            { PA.spawnLeafSubtreeRequestTask = fromText (rpcTask cfg),
              PA.spawnLeafSubtreeRequestBranchName = fromText "",
              PA.spawnLeafSubtreeRequestRole = fromText "",
              PA.spawnLeafSubtreeRequestAgentType = Enumerated (Right PA.AgentTypeAGENT_TYPE_UNSPECIFIED),
              PA.spawnLeafSubtreeRequestModel = maybe (fromText "") fromText (rpcModel cfg),
              PA.spawnLeafSubtreeRequestPermissionMode = fromText "",
              PA.spawnLeafSubtreeRequestAllowedTools = V.empty,
              PA.spawnLeafSubtreeRequestDisallowedTools = V.empty,
              PA.spawnLeafSubtreeRequestStandaloneRepo = False,
              PA.spawnLeafSubtreeRequestAllowedDirs = V.empty,
              PA.spawnLeafSubtreeRequestResumePrNumber = rpcPrNumber cfg,
              PA.spawnLeafSubtreeRequestExpectedHeadSha = fromText (rpcExpectedHeadSha cfg),
              PA.spawnLeafSubtreeRequestIntentId = fromText ""
            }
    result <- suspendEffect @Agent.AgentSpawnLeafSubtree req
    pure $ case result of
      Left err -> Left err
      Right resp -> case (PA.spawnLeafSubtreeResponseAgent resp, PA.spawnLeafSubtreeResponseInvocation resp) of
        (Nothing, _) -> Left (resumeHandoffError "ResumePr succeeded but no agent info returned")
        (Just _, Nothing) -> Left (resumeHandoffError "ResumePr succeeded but no invocation handoff metadata returned")
        (Just info, Just handoff)
          | not (PA.invocationHandoffFresh handoff) ->
              Left (resumeHandoffError "ResumePr returned an already-running invocation; no fresh process was started")
          | not (PA.invocationHandoffReady handoff) ->
              Left (resumeHandoffError "ResumePr returned an invocation that was not ready on its exact tmux target")
          | otherwise ->
              Right
                ( (protoAgentInfoToSpawnResult info)
                    { invocationId = Just (toText (PA.invocationHandoffInvocationId handoff)),
                      invocationTrigger = Just (toText (PA.invocationHandoffTrigger handoff)),
                      invocationRuntime = Just (toText (PA.invocationHandoffRuntime handoff)),
                      routingTargetType = Just (toText (PA.invocationHandoffTargetType handoff)),
                      routingTargetId = Just (toText (PA.invocationHandoffTargetId handoff)),
                      invocationFresh = Just (PA.invocationHandoffFresh handoff),
                      invocationReady = Just (PA.invocationHandoffReady handoff),
                      invocationOutcome = Just (toText (PA.invocationHandoffOutcome handoff))
                    }
                )
  SpawnWorkerC cfg -> do
    let req =
          PA.SpawnWorkerRequest
            { PA.spawnWorkerRequestName = fromText (swcName cfg),
              PA.spawnWorkerRequestPrompt = fromText (swcPrompt cfg),
              PA.spawnWorkerRequestPermissionMode = fromText (fromMaybe "" (permMode (swcPerms cfg))),
              PA.spawnWorkerRequestAllowedTools = V.fromList (map fromText (allowedTools (swcPerms cfg))),
              PA.spawnWorkerRequestDisallowedTools = V.fromList (map fromText (disallowedTools (swcPerms cfg))),
              PA.spawnWorkerRequestAgentType = Enumerated (Right (maybe PA.AgentTypeAGENT_TYPE_UNSPECIFIED toProtoAgentType (swcAgentType cfg))),
              PA.spawnWorkerRequestIntentId = fromText (fromMaybe "" (swcIntentId cfg)),
              PA.spawnWorkerRequestModel = fromText (fromMaybe "" (swcModel cfg))
            }
    result <- suspendEffect @Agent.AgentSpawnWorker req
    pure $ case result of
      Left err -> Left err
      Right resp -> case PA.spawnWorkerResponseAgent resp of
        Nothing -> Left (EffectError (Just (EffectErrorKindInvalidInput (InvalidInput "SpawnWorker succeeded but no agent info returned"))))
        Just info -> Right (protoAgentInfoToSpawnResult info)
  CloseWorkerPaneC pane -> do
    let req =
          PA.CloseWorkerPaneRequest
            { PA.closeWorkerPaneRequestPaneId = fromText pane
            }
    suspendEffect @Agent.AgentCloseWorkerPane req

-- ============================================================================
-- Conversion helpers
-- ============================================================================

toProtoAgentType :: AgentType -> PA.AgentType
toProtoAgentType Claude = PA.AgentTypeAGENT_TYPE_CLAUDE
toProtoAgentType Retired = PA.AgentTypeAGENT_TYPE_RETIRED
toProtoAgentType Shoal = PA.AgentTypeAGENT_TYPE_SHOAL
toProtoAgentType OpenCode = PA.AgentTypeAGENT_TYPE_OPENCODE
toProtoAgentType Codex = PA.AgentTypeAGENT_TYPE_CODEX

agentTypeLabel :: AgentType -> Text
agentTypeLabel Claude = "claude"
agentTypeLabel Retired = "retired"
agentTypeLabel Shoal = "shoal"
agentTypeLabel OpenCode = "opencode"
agentTypeLabel Codex = "codex"

permissionsToProto :: ClaudePermissions -> PA.Permissions
permissionsToProto perms =
  PA.Permissions
    { PA.permissionsAllow = V.fromList (map (fromText . renderToolPattern) (cpAllow perms)),
      PA.permissionsDeny = V.fromList (map (fromText . renderToolPattern) (cpDeny perms))
    }

protoAgentInfoToSpawnResult :: PA.AgentInfo -> SpawnResult
protoAgentInfoToSpawnResult info =
  SpawnResult
    { worktreePath = toText (PA.agentInfoWorktreePath info),
      branchName = toText (PA.agentInfoBranchName info),
      tabName = toText (PA.agentInfoMuxWindow info),
      issueTitle = toText (PA.agentInfoIssue info),
      agentTypeResult = case PA.agentInfoAgentType info of
        Enumerated (Right PA.AgentTypeAGENT_TYPE_CLAUDE) -> "claude"
        Enumerated (Right PA.AgentTypeAGENT_TYPE_RETIRED) -> "retired"
        Enumerated (Right PA.AgentTypeAGENT_TYPE_SHOAL) -> "shoal"
        Enumerated (Right PA.AgentTypeAGENT_TYPE_OPENCODE) -> "opencode"
        Enumerated (Right PA.AgentTypeAGENT_TYPE_CODEX) -> "codex"
        _ -> "unknown",
      paneId =
        let value = toText (PA.agentInfoPaneId info)
         in if T.null value then Nothing else Just value,
      invocationId = Nothing,
      invocationTrigger = Nothing,
      invocationRuntime = Nothing,
      routingTargetType = Nothing,
      routingTargetId = Nothing,
      invocationFresh = Nothing,
      invocationReady = Nothing,
      invocationOutcome = Nothing
    }

resumeHandoffError :: Text -> EffectError
resumeHandoffError message =
  EffectError (Just (EffectErrorKindInvalidInput (InvalidInput (fromText message))))
