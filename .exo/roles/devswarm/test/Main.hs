{-# LANGUAGE NamedFieldPuns #-}
{-# LANGUAGE OverloadedStrings #-}

module Main where

import Control.Monad (forM_, unless)
import Control.Monad.Freer (runM)
import Control.Monad.Freer.Coroutine (runC)
import Control.Monad.Freer.Coroutine qualified as C
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text qualified as T
import DevPhase (DevEvent (..), DevPhase (..))
import ExoMonad.Guest.Effects.AgentControl (runAgentControlSuspend)
import ExoMonad.Guest.Effects.FileSystem (runFileSystemSuspend)
import ExoMonad.Guest.StateMachine (StateMachine (..), StopCheckResult (..), TransitionResult (..))
import ExoMonad.Guest.Types (HookEventType (..), HookInput (..), HookOutput (..), HookSpecificOutput (..), Runtime (..))
import ExoMonad.Types (HookConfig (..), RoleConfig (..))
import ReviewerPhase (ReviewerPhase (..))
import ReviewerRole qualified
import RootRole qualified
import TLRole qualified

denyTools :: [Text]
denyTools = ["Edit", "Write", "MultiEdit", "NotebookEdit"]

allowTools :: [Text]
allowTools = ["Read", "Grep", "Bash", "spawn_leaf", "spawn_worker", "send_message"]

main :: IO ()
main = do
  assertRoleDeny "tl" TLRole.config
  assertRoleDeny "root" RootRole.config
  assertRoleAllow "tl" TLRole.config
  assertRoleAllow "root" RootRole.config
  assertReviewerPostToolUseEventName
  assertReviewerCanExitDecisions
  assertDevNeedsHumanDirectionAfterOneFixRound

assertRoleDeny :: Text -> RoleConfig tools -> IO ()
assertRoleDeny role cfg =
  forM_ denyTools $ \toolName -> do
    output <- runPreToolUse cfg toolName
    assertBool (label role toolName "denies") (not (continue_ output))
    assertEqual (label role toolName "decision") (Just "deny") (permissionDecisionOf output)
    assertBool (label role toolName "message names tool") (messageContains toolName output)
    assertBool (label role toolName "message nudges redispatch") (messageContains "spawn_leaf or spawn_worker" output)
    assertBool (label role toolName "message mentions correction loop") (messageContains "Worker Correction Loop" output)

assertRoleAllow :: Text -> RoleConfig tools -> IO ()
assertRoleAllow role cfg =
  forM_ allowTools $ \toolName -> do
    output <- runPreToolUse cfg toolName
    assertBool (label role toolName "allows") (continue_ output)
    assertEqual (label role toolName "decision") (Just "allow") (permissionDecisionOf output)
    assertBool (label role toolName "does not emit deny") (not (messageContains "TL agents cannot use" output))

runPreToolUse :: RoleConfig tools -> Text -> IO HookOutput
runPreToolUse cfg toolName = do
  status <- runM $ runC $ runFileSystemSuspend $ runAgentControlSuspend (preToolUse (hooks cfg) (hookInput toolName))
  case status of
    C.Done output -> pure output
    C.Continue {} -> fail "PreToolUse hook unexpectedly suspended"

runPostToolUse :: RoleConfig tools -> IO HookOutput
runPostToolUse cfg = do
  status <- runM $ runC $ runFileSystemSuspend $ runAgentControlSuspend (postToolUse (hooks cfg) (hookInputFor PostToolUse "Bash"))
  case status of
    C.Done output -> pure output
    C.Continue {} -> fail "PostToolUse hook unexpectedly suspended"

hookInput :: Text -> HookInput
hookInput = hookInputFor PreToolUse

hookInputFor :: HookEventType -> Text -> HookInput
hookInputFor eventName toolName =
  HookInput
    { hiSessionId = "test-session",
      hiHookEventName = eventName,
      hiToolName = Just toolName,
      hiToolInput = Just Aeson.Null,
      hiStopHookActive = Nothing,
      hiPrompt = Nothing,
      hiPromptResponse = Nothing,
      hiTimestamp = Nothing,
      hiToolResponse = Nothing,
      hiAgentId = Just "test-agent",
      hiExomonadSessionId = Just "test-exomonad-session",
      hiExitStatus = Nothing,
      hiRuntime = Just Claude,
      hiCwd = Nothing,
      hiTranscriptPath = Nothing,
      hiLlmRequest = Nothing,
      hiLlmResponse = Nothing
    }

assertReviewerPostToolUseEventName :: IO ()
assertReviewerPostToolUseEventName = do
  output <- runPostToolUse ReviewerRole.config
  case hookSpecificOutput output of
    Just PostToolUseOutput {} -> pure ()
    other -> fail $ "reviewer PostToolUse should emit PostToolUseOutput, got " <> show other

assertReviewerCanExitDecisions :: IO ()
assertReviewerCanExitDecisions = do
  assertBlocks "approved awaiting CI" (canExit (ReviewerApprovedAwaitingCI 7))
  assertBlocks "requested changes" (canExit (ReviewerChangesRequested 7 "fix it"))
  assertBlocks "reviewing" (canExit (ReviewerReviewing 7 1))
  assertClean "done exits cleanly" (canExit ReviewerDone)
  assertClean "spawned exits cleanly" (canExit ReviewerSpawned)

assertDevNeedsHumanDirectionAfterOneFixRound :: IO ()
assertDevNeedsHumanDirectionAfterOneFixRound = do
  case transition (DevUnderReview 9 1) (ReviewReceivedEv 9 "still wrong") of
    Transitioned (DevNeedsHumanDirection 9 _) -> pure ()
    other -> fail $ "expected DevNeedsHumanDirection after first fix round, got " <> showDevTransition other
  assertBlocks "needs human direction" (canExit (DevNeedsHumanDirection 9 "still wrong"))

showDevTransition :: TransitionResult DevPhase -> String
showDevTransition (Transitioned phase) = "Transitioned " <> show phase
showDevTransition (InvalidTransition reason) = "InvalidTransition " <> T.unpack reason

assertBlocks :: String -> StopCheckResult -> IO ()
assertBlocks _ (MustBlock _) = pure ()
assertBlocks label_ other = fail $ label_ <> ": expected MustBlock, got " <> showStopCheck other

assertClean :: String -> StopCheckResult -> IO ()
assertClean _ Clean = pure ()
assertClean label_ other = fail $ label_ <> ": expected Clean, got " <> showStopCheck other

showStopCheck :: StopCheckResult -> String
showStopCheck (MustBlock msg) = "MustBlock " <> T.unpack msg
showStopCheck (ShouldNudge msg) = "ShouldNudge " <> T.unpack msg
showStopCheck Clean = "Clean"

permissionDecisionOf :: HookOutput -> Maybe Text
permissionDecisionOf output =
  case hookSpecificOutput output of
    Just PreToolUseOutput {permissionDecision} -> Just permissionDecision
    _ -> Nothing

messageContains :: Text -> HookOutput -> Bool
messageContains needle output =
  any (maybe False (needle `T.isInfixOf`)) [stopReason output, denyReason output]

denyReason :: HookOutput -> Maybe Text
denyReason output =
  case hookSpecificOutput output of
    Just PreToolUseOutput {permissionDecisionReason} -> permissionDecisionReason
    _ -> Nothing

label :: Text -> Text -> Text -> String
label role toolName assertion =
  T.unpack role <> " " <> T.unpack toolName <> " " <> T.unpack assertion

assertBool :: String -> Bool -> IO ()
assertBool msg condition =
  unless condition (fail msg)

assertEqual :: (Eq a, Show a) => String -> a -> a -> IO ()
assertEqual msg expected actual =
  unless (expected == actual) $
    fail (msg <> ": expected " <> show expected <> ", got " <> show actual)
