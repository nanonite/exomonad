{-# LANGUAGE OverloadedStrings #-}

-- | HTTP-native hook configuration for dev agents.
--
-- Hooks run server-side via WASM. The permission cascade validates
-- tool calls and guards against known failure modes.
module HttpDevHooks
  ( httpDevHooks,
  )
where

import Control.Monad (void)
import Control.Monad.Freer (Eff)
import Data.Aeson (Value (..))
import Data.Aeson qualified as Aeson
import Data.Aeson.KeyMap qualified as KM
import Data.ByteString.Lazy qualified as BSL
import Data.Maybe (fromMaybe)
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Text.Lazy.Encoding qualified as TLE
import Effects.Log qualified as Log
import ExoMonad.Effects.Log (LogEmitEvent, LogInfo)
import ExoMonad.Guest.Effects.StopHook (getCurrentBranch, checkUncommittedWork, checkPRNotFiled)
import ExoMonad.Guest.StateMachine (StopCheckResult(..), checkExit, describeStopResult)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect_)
import ExoMonad.Guest.Types (HookInput (..), HookOutput (..), Runtime (..), StopDecision(..), StopHookOutput(..), BeforeModelOutput (..), AfterModelOutput (..), allowResponse, denyResponse, postToolUseResponse, allowStopResponse, silentBlockStopResponse)
import ExoMonad.Permissions (PermissionCheck (..), checkAgentPermissions)
import ExoMonad.Types (HookConfig (..), Effects, defaultSessionStartHook)
import DevPhase (DevPhase(..), DevEvent)
import HookPolicy (preToolUseWithGhBlock)

-- ============================================================================
-- Tool Types
-- ============================================================================

-- ============================================================================
-- Chainlink Guards
-- ============================================================================

-- | Block direct sqlite access to Chainlink state from dev agents.
-- Dev agents should use the scoped Chainlink MCP tools exposed by DevRole.
checkChainlinkSqlAccess :: HookInput -> Maybe Text
checkChainlinkSqlAccess hookInput =
  case hiToolInput hookInput of
    Just (Object obj)
      | Just (String cmd) <- KM.lookup "command" obj,
        let normalized = T.toCaseFold cmd,
        "sqlite3" `T.isInfixOf` normalized,
        ".chainlink" `T.isInfixOf` normalized ->
            Just $
              "BLOCKED: Do not access .chainlink/issues.db directly via sqlite3. "
                <> "Use the scoped chainlink MCP tools such as chainlink_issue_show and chainlink_issue_comment instead."
    _ -> Nothing

-- ============================================================================
-- Hook Config
-- ============================================================================

httpDevHooks :: HookConfig
httpDevHooks =
  HookConfig
    { preToolUse = preToolUseWithGhBlock permissionCascade,
      postToolUse = \_ -> pure (postToolUseResponse Nothing),
      onStop = \_ -> devStopCheck,
      onSubagentStop = \_ -> devStopCheck,
      onSessionStart = defaultSessionStartHook,
      beforeModel = \_ -> pure (BeforeModelAllow Nothing),
      afterModel = \_ -> pure (AfterModelAllow Nothing)
    }

devStopCheck :: Eff Effects StopHookOutput
devStopCheck = do
  branch <- getCurrentBranch
  if branch `elem` ["main", "master"]
    then pure allowStopResponse
    else do
      result <- checkExit @DevPhase @DevEvent branch DevSpawned
      -- Log for observability
      void $ suspendEffect_ @LogEmitEvent (Log.EmitEventRequest
        { Log.emitEventRequestEventType = "agent.stop_check",
          Log.emitEventRequestPayload = BSL.toStrict $ Aeson.encode $ Aeson.object ["branch" Aeson..= branch, "result" Aeson..= describeStopResult result],
          Log.emitEventRequestTimestamp = 0
        })
      case result of
        MustBlock _ -> pure silentBlockStopResponse
        ShouldNudge msg -> pure $ StopHookOutput Allow (Just msg)
        Clean -> do
          uncommitted <- checkUncommittedWork branch
          case uncommitted of
            Just msg -> pure $ StopHookOutput Allow (Just msg)
            Nothing -> do
              noPR <- checkPRNotFiled branch
              case noPR of
                Just msg -> pure $ StopHookOutput Allow (Just msg)
                Nothing -> pure allowStopResponse

-- | Permission cascade with tool-specific guards.
permissionCascade :: HookInput -> Eff Effects HookOutput
permissionCascade hookInput = do
  let tool = fromMaybe "" (hiToolName hookInput)
      args = fromMaybe (Aeson.Object mempty) (hiToolInput hookInput)
      argsJson = TLE.decodeUtf8 $ Aeson.encode args
  void $ suspendEffect_ @LogInfo $ Log.InfoRequest
    { Log.infoRequestMessage = "[PreToolUse] tool=" <> TL.fromStrict tool <> " input=" <> argsJson
    , Log.infoRequestFields = ""
    }
  case checkChainlinkSqlAccess hookInput of
    Just reason -> pure (denyResponse reason)
    Nothing ->
      case checkAgentPermissions "dev" tool args of
        Allowed -> pure (allowResponse Nothing)
        Escalate -> pure (allowResponse (Just "escalation-needed"))
        Denied reason -> pure (denyResponse reason)
