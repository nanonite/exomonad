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
import AllRoles (lookupRole, roleListTools)
import DevPhase (DevEvent (..), DevPhase (..))
import ExoMonad.Guest.Effects.AgentControl (runAgentControlSuspend)
import ExoMonad.Guest.Effects.FileSystem (runFileSystemSuspend)
import ExoMonad.Guest.StateMachine (StateMachine (..), StopCheckResult (..), TransitionResult (..))
import ExoMonad.Guest.Tool.Class (ToolDefinition (tdName))
import ExoMonad.Guest.Types (HookEventType (..), HookInput (..), HookOutput (..), HookSpecificOutput (..), Runtime (..))
import ExoMonad.Types (HookConfig (..), RoleConfig (..))
import ReviewerPhase (ReviewerEvent, ReviewerPhase (..))
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
  assertReviewerToolList
  assertReviewerPostToolUseEventName
  assertReviewerCanExitDecisions
  assertDevNeedsHumanDirectionAfterOneFixRound
  assertReviewApprovedAfterFixRoundTransitionsToApproved
  assertReviewApprovedFromUnderReviewRoundZero
  assertFixesPushedFromChangesRequestedYieldsRoundOne
  assertFixesPushedIncrementsUnderReviewRound
  assertApprovedMergeReadyTransitionsToDoneAndExits

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

assertReviewerToolList :: IO ()
assertReviewerToolList =
  case lookupRole "reviewer" of
    Nothing -> fail "reviewer role missing from registry"
    Just roleCfg -> do
      let names = map tdName (roleListTools roleCfg)
      assertEqual
        "reviewer tools"
        ["approve_pr", "request_changes", "post_review_comment"]
        names
      assertBool "reviewer must not expose send_message" ("send_message" `notElem` names)
      assertBool "reviewer must not expose notify_parent" ("notify_parent" `notElem` names)

assertReviewerCanExitDecisions :: IO ()
assertReviewerCanExitDecisions = do
  assertBlocks "reviewing" (canExit @ReviewerPhase @ReviewerEvent (ReviewerReviewing 7))
  assertClean "done exits cleanly" (canExit @ReviewerPhase @ReviewerEvent ReviewerDone)
  assertClean "spawned exits cleanly" (canExit @ReviewerPhase @ReviewerEvent ReviewerSpawned)
  assertClean "posted exits cleanly" (canExit @ReviewerPhase @ReviewerEvent (ReviewerPosted 7))

assertDevNeedsHumanDirectionAfterOneFixRound :: IO ()
assertDevNeedsHumanDirectionAfterOneFixRound = do
  case transition (DevUnderReview 9 1) (ReviewReceivedEv 9 "still wrong") of
    Transitioned (DevNeedsHumanDirection 9 _) -> pure ()
    other -> fail $ "expected DevNeedsHumanDirection after first fix round, got " <> showDevTransition other
  assertBlocks "needs human direction" (canExit @DevPhase @DevEvent (DevNeedsHumanDirection 9 "still wrong"))

-- Intended semantics: after the dev has pushed a fix (round_ >= 1), an
-- *approval* must transition to DevApproved, NOT DevNeedsHumanDirection.
-- The watcher is responsible for firing ReviewApprovedEv (not
-- ReviewReceivedEv) when the reviewer's verdict is "approved".
assertReviewApprovedAfterFixRoundTransitionsToApproved :: IO ()
assertReviewApprovedAfterFixRoundTransitionsToApproved = do
  case transition (DevUnderReview 9 1) (ReviewApprovedEv 9) of
    Transitioned (DevApproved 9) -> pure ()
    other -> fail $ "expected DevApproved after fix round + approval, got " <> showDevTransition other

-- Approvals on the initial review pass (round 0) should also transition to
-- DevApproved — the round counter must not gate the approval path.
assertReviewApprovedFromUnderReviewRoundZero :: IO ()
assertReviewApprovedFromUnderReviewRoundZero = do
  case transition (DevUnderReview 9 0) (ReviewApprovedEv 9) of
    Transitioned (DevApproved 9) -> pure ()
    other -> fail $ "expected DevApproved from initial review, got " <> showDevTransition other

-- A fix push from DevChangesRequested initializes the round counter to 1,
-- not 0 — round 0 is the pre-fix initial-review window.
assertFixesPushedFromChangesRequestedYieldsRoundOne :: IO ()
assertFixesPushedFromChangesRequestedYieldsRoundOne = do
  case transition (DevChangesRequested 9 ["needs header"]) (FixesPushedEv 9 "ci") of
    Transitioned (DevUnderReview 9 1) -> pure ()
    other -> fail $ "expected DevUnderReview 9 1 after first fix push, got " <> showDevTransition other

-- Subsequent fix pushes increment the round counter monotonically.
assertFixesPushedIncrementsUnderReviewRound :: IO ()
assertFixesPushedIncrementsUnderReviewRound = do
  case transition (DevUnderReview 9 1) (FixesPushedEv 9 "ci") of
    Transitioned (DevUnderReview 9 2) -> pure ()
    other -> fail $ "expected DevUnderReview 9 2 after second fix push, got " <> showDevTransition other

-- DevApproved blocks exit until MergeReadyEv arrives; once it arrives, the
-- dev transitions to DevDone and may exit cleanly. The intermediate
-- DevApproved -> DevDone transition is the merge-ready signal the TL has
-- already merged the PR.
assertApprovedMergeReadyTransitionsToDoneAndExits :: IO ()
assertApprovedMergeReadyTransitionsToDoneAndExits = do
  case transition (DevApproved 9) (MergeReadyEv 9 "success" "main.feature") of
    Transitioned DevDone -> pure ()
    other -> fail $ "expected DevDone after MergeReadyEv from approved, got " <> showDevTransition other
  assertBlocks "approved waiting for CI" (canExit @DevPhase @DevEvent (DevApproved 9))
  assertClean "done exits cleanly" (canExit @DevPhase @DevEvent DevDone)

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
