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
    RoleRoute (..),
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

-- | Accepted route identity carried across the guest/host boundary.
data RoleRoute = RoleRoute
  { roleRouteRole :: Text,
    roleRouteHarness :: Text,
    roleRouteAgentType :: Maybe AgentType,
    roleRouteModel :: Maybe Text,
    roleRouteEffort :: Maybe Text,
    roleRouteConfigurationFingerprint :: Maybe Text,
    roleRoutePolicyFingerprint :: Maybe Text,
    roleRouteManifestDigest :: Maybe Text,
    roleRouteFingerprint :: Maybe Text,
    roleRouteDispatchIntentId :: Maybe Text,
    roleRouteScopeId :: Maybe Text
  }
  deriving (Show, Eq, Generic)

instance FromJSON RoleRoute where
  parseJSON = withObject "RoleRoute" $ \value ->
    RoleRoute
      <$> value .: "role"
      <*> value .: "harness"
      <*> value .:? "agent_type"
      <*> value .:? "model"
      <*> value .:? "effort"
      <*> value .:? "configuration_fingerprint"
      <*> value .:? "policy_fingerprint"
      <*> value .:? "manifest_digest"
      <*> value .:? "route_fingerprint"
      <*> value .:? "dispatch_intent_id"
      <*> value .:? "scope_id"

instance JsonSchema RoleRoute

-- | Result of spawning an agent.
data SpawnResult = SpawnResult
  { agentId :: Maybe Text,
    worktreePath :: Text,
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
    invocationOutcome :: Maybe Text,
    roleRouteEvidence :: Maybe Value
  }
  deriving (Show, Eq, Generic)

instance FromJSON SpawnResult where
  parseJSON = withObject "SpawnResult" $ \v ->
    SpawnResult
      <$> v .:? "agent_id"
      <*> v .: "worktree_path"
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
      <*> v .:? "role_route"

instance ToJSON SpawnResult where
  toJSON
    ( SpawnResult
        agentId
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
        roleRouteEvidence
      ) =
      object
        [ "worktree_path" .= w,
          "agent_id" .= agentId,
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
          "invocation_outcome" .= invocationOutcome,
          "role_route" .= roleRouteEvidence
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
    slcRoleRoute :: Maybe RoleRoute,
    slcPerms :: PermissionFlags,
    slcStandaloneRepo :: Bool,
    slcAllowedDirs :: [Text],
    slcBlockedIssueId :: Maybe Word64,
    slcExpectedInvocationId :: Maybe Text,
    slcExpectedBranch :: Maybe Text,
    slcExpectedWorktreeFingerprint :: Maybe Text,
    slcHumanApproved :: Bool
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
    swcRoleRoute :: Maybe RoleRoute,
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
              PA.spawnLeafSubtreeRequestIntentId = fromText (fromMaybe "" (slcIntentId cfg)),
              PA.spawnLeafSubtreeRequestBlockedIssueId = fromMaybe 0 (slcBlockedIssueId cfg),
              PA.spawnLeafSubtreeRequestExpectedInvocationId = fromText (fromMaybe "" (slcExpectedInvocationId cfg)),
              PA.spawnLeafSubtreeRequestExpectedBranch = fromText (fromMaybe "" (slcExpectedBranch cfg)),
              PA.spawnLeafSubtreeRequestExpectedWorktreeFingerprint = fromText (fromMaybe "" (slcExpectedWorktreeFingerprint cfg)),
              PA.spawnLeafSubtreeRequestHumanApproved = slcHumanApproved cfg,
              PA.spawnLeafSubtreeRequestRoleRoute = roleRouteToProto <$> slcRoleRoute cfg
            }
    result <- suspendEffect @Agent.AgentSpawnLeafSubtree req
    pure $ case result of
      Left err -> Left err
      Right resp -> case PA.spawnLeafSubtreeResponseAgent resp of
        Nothing -> Left (EffectError (Just (EffectErrorKindInvalidInput (InvalidInput "SpawnLeafSubtree succeeded but no agent info returned"))))
        Just info ->
          Right
            (applyInvocationHandoff (protoAgentInfoToSpawnResult info) (PA.spawnLeafSubtreeResponseInvocation resp))
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
              PA.spawnLeafSubtreeRequestIntentId = fromText "",
              PA.spawnLeafSubtreeRequestBlockedIssueId = 0,
              PA.spawnLeafSubtreeRequestExpectedInvocationId = fromText "",
              PA.spawnLeafSubtreeRequestExpectedBranch = fromText "",
              PA.spawnLeafSubtreeRequestExpectedWorktreeFingerprint = fromText "",
              PA.spawnLeafSubtreeRequestHumanApproved = False,
              PA.spawnLeafSubtreeRequestRoleRoute = Nothing
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
              PA.spawnWorkerRequestModel = fromText (fromMaybe "" (swcModel cfg)),
              PA.spawnWorkerRequestRoleRoute = roleRouteToProto <$> swcRoleRoute cfg
            }
    result <- suspendEffect @Agent.AgentSpawnWorker req
    pure $ case result of
      Left err -> Left err
      Right resp -> case PA.spawnWorkerResponseAgent resp of
        Nothing -> Left (EffectError (Just (EffectErrorKindInvalidInput (InvalidInput "SpawnWorker succeeded but no agent info returned"))))
        Just info ->
          let result =
                (protoAgentInfoToSpawnResult info)
                  { roleRouteEvidence = roleRouteToJson <$> PA.spawnWorkerResponseRoleRoute resp
                  }
           in Right
                (applyInvocationHandoff result (PA.spawnWorkerResponseInvocation resp))
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

roleRouteToProto :: RoleRoute -> PA.RoleRoute
roleRouteToProto route =
  PA.RoleRoute
    { PA.roleRouteRole = fromText (roleRouteRole route),
      PA.roleRouteHarness = fromText (roleRouteHarness route),
      PA.roleRouteAgentType = Enumerated (Right (maybe PA.AgentTypeAGENT_TYPE_UNSPECIFIED toProtoAgentType (roleRouteAgentType route))),
      PA.roleRouteModel = fromText (fromMaybe "" (roleRouteModel route)),
      PA.roleRouteEffort = fromText (fromMaybe "" (roleRouteEffort route)),
      PA.roleRouteConfigurationFingerprint = fromText (fromMaybe "" (roleRouteConfigurationFingerprint route)),
      PA.roleRoutePolicyFingerprint = fromText (fromMaybe "" (roleRoutePolicyFingerprint route)),
      PA.roleRouteManifestDigest = fromText (fromMaybe "" (roleRouteManifestDigest route)),
      PA.roleRouteRouteFingerprint = fromText (fromMaybe "" (roleRouteFingerprint route)),
      PA.roleRouteDispatchIntentId = fromText (fromMaybe "" (roleRouteDispatchIntentId route)),
      PA.roleRouteScopeId = fromText (fromMaybe "" (roleRouteScopeId route))
    }

roleRouteToJson :: PA.RoleRoute -> Value
roleRouteToJson route =
  object
    [ "role" .= toText (PA.roleRouteRole route),
      "harness" .= toText (PA.roleRouteHarness route),
      "agent_type" .= protoAgentTypeLabel (PA.roleRouteAgentType route),
      "model" .= optionalText (PA.roleRouteModel route),
      "effort" .= optionalText (PA.roleRouteEffort route),
      "configuration_fingerprint" .= optionalText (PA.roleRouteConfigurationFingerprint route),
      "policy_fingerprint" .= optionalText (PA.roleRoutePolicyFingerprint route),
      "manifest_digest" .= optionalText (PA.roleRouteManifestDigest route),
      "route_fingerprint" .= optionalText (PA.roleRouteRouteFingerprint route),
      "dispatch_intent_id" .= optionalText (PA.roleRouteDispatchIntentId route),
      "scope_id" .= optionalText (PA.roleRouteScopeId route)
    ]
  where
    optionalText value =
      let text = toText value
       in if T.null text then Aeson.Null else Aeson.String text

protoAgentTypeLabel :: Enumerated PA.AgentType -> Value
protoAgentTypeLabel (Enumerated (Right PA.AgentTypeAGENT_TYPE_CLAUDE)) = Aeson.String "claude"
protoAgentTypeLabel (Enumerated (Right PA.AgentTypeAGENT_TYPE_OPENCODE)) = Aeson.String "opencode"
protoAgentTypeLabel (Enumerated (Right PA.AgentTypeAGENT_TYPE_CODEX)) = Aeson.String "codex"
protoAgentTypeLabel _ = Aeson.Null

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
    { agentId =
        let value = toText (PA.agentInfoId info)
         in if T.null value then Nothing else Just value,
      worktreePath = toText (PA.agentInfoWorktreePath info),
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
      invocationOutcome = Nothing,
      roleRouteEvidence = Nothing
    }

applyInvocationHandoff :: SpawnResult -> Maybe PA.InvocationHandoff -> SpawnResult
applyInvocationHandoff result Nothing = result
applyInvocationHandoff result (Just handoff) =
  result
    { invocationId = Just (toText (PA.invocationHandoffInvocationId handoff)),
      invocationTrigger = Just (toText (PA.invocationHandoffTrigger handoff)),
      invocationRuntime = Just (toText (PA.invocationHandoffRuntime handoff)),
      routingTargetType = Just (toText (PA.invocationHandoffTargetType handoff)),
      routingTargetId = Just (toText (PA.invocationHandoffTargetId handoff)),
      invocationFresh = Just (PA.invocationHandoffFresh handoff),
      invocationReady = Just (PA.invocationHandoffReady handoff),
      invocationOutcome = Just (toText (PA.invocationHandoffOutcome handoff)),
      roleRouteEvidence = fmap roleRouteToJson (PA.invocationHandoffRoleRoute handoff)
    }

resumeHandoffError :: Text -> EffectError
resumeHandoffError message =
  EffectError (Just (EffectErrorKindInvalidInput (InvalidInput (fromText message))))
