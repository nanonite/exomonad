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
import ExoMonad.Types (ChainlinkDbPathState (..), HookConfig (..), RoleConfig (..), validateChainlinkDbEnv)
import ReviewerPhase (ReviewerEvent (..), ReviewerPhase (..))
import ReviewerRole qualified
import RootRole qualified
import TLRole qualified
import WorkerRole qualified

denyTools :: [Text]
denyTools = ["Edit", "Write", "MultiEdit", "NotebookEdit"]

allowTools :: [Text]
allowTools = ["Read", "Grep", "Bash", "spawn_leaf", "spawn_worker", "send_tmux_message", "send_mailbox_message"]

main :: IO ()
main = do
  assertRoleDeny "tl" TLRole.config
  assertRoleDeny "root" RootRole.config
  assertReviewerDenyImplementationTools
  assertRuntimeImplementationPolicy
  assertChainlinkCLIBlockPolicy
  assertChainlinkDbSessionStartFailsafe
  assertReviewerGitAuthorMutationPolicy
  assertRoleAllow "tl" TLRole.config
  assertRoleAllow "root" RootRole.config
  assertReviewerToolList
  assertNoRoleExposesShutdown
  assertReviewerPostToolUseEventName
  assertReviewerCanExitDecisions
  assertReviewerVerdictsAreTerminal
  assertAppendVerdictLocksPerHeadSha
  assertAppendVerdictAllowsNewHeadSha
  assertAppendVerdictRecordsAuthorAndHeadSha
  assertDevNeedsHumanDirectionAfterOneFixRound
  assertReviewApprovedAfterFixRoundTransitionsToApproved
  assertReviewApprovedFromUnderReviewRoundZero
  assertFixesPushedFromChangesRequestedYieldsRoundOne
  assertFixesPushedIncrementsUnderReviewRound
  assertApprovedCanExitOnWatcherMergeReady
  assertCITriggeredMergeReadyTransitionsToDoneAndExits
  assertCIFailureBlocksAfterTrigger

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

assertChainlinkDbSessionStartFailsafe :: IO ()
assertChainlinkDbSessionStartFailsafe = do
  assertValidationFails "unset CHAINLINK_DB" Nothing ChainlinkDbPathMissing ChainlinkDbPathMissing "CHAINLINK_DB not set"
  assertValidationFails "missing CHAINLINK_DB directory" (Just "/tmp/missing-chainlink") ChainlinkDbPathMissing ChainlinkDbPathMissing "missing path"
  assertValidationFails "phantom CHAINLINK_DB directory" (Just "/tmp/empty-chainlink") ChainlinkDbPathDirectory ChainlinkDbPathMissing "phantom DB directory without issues.db"
  assertEqual
    "valid CHAINLINK_DB directory"
    (Right ())
    (validateChainlinkDbEnv (Just "/tmp/project/.chainlink") ChainlinkDbPathDirectory ChainlinkDbPathFile)

assertValidationFails :: String -> Maybe Text -> ChainlinkDbPathState -> ChainlinkDbPathState -> Text -> IO ()
assertValidationFails labelText maybeDb dbState issuesState expected =
  case validateChainlinkDbEnv maybeDb dbState issuesState of
    Left message -> assertBool labelText (expected `T.isInfixOf` message)
    Right () -> fail (labelText <> ": expected validation failure")

assertReviewerDenyImplementationTools :: IO ()
assertReviewerDenyImplementationTools =
  forM_ denyTools $ \toolName -> do
    output <- runPreToolUse ReviewerRole.config toolName
    assertBool (label "reviewer" toolName "denies") (not (continue_ output))
    assertEqual (label "reviewer" toolName "decision") (Just "deny") (permissionDecisionOf output)
    assertBool (label "reviewer" toolName "message names reviewer policy") (messageContains "Reviewers do not edit code" output)
    assertBool (label "reviewer" toolName "message relays to worker") (messageContains "request_changes" output)

assertRuntimeImplementationPolicy :: IO ()
assertRuntimeImplementationPolicy = do
  assertAllowsRuntimeTool "tl allows Claude apply_patch" TLRole.config Claude "apply_patch"
  assertDeniesRuntimeTool "tl denies Codex apply_patch" TLRole.config Codex "apply_patch" "apply_patch"
  assertAllowsRuntimeTool "tl allows Codex Edit passthrough" TLRole.config Codex "Edit"
  assertDeniesRuntimeTool "root denies OpenCode edit" RootRole.config OpenCode "edit" "edit"
  assertAllowsRuntimeTool "root allows Gemini apply_patch passthrough" RootRole.config Gemini "apply_patch"
  assertDeniesRuntimeTool "reviewer denies Codex str_replace_editor" ReviewerRole.config Codex "str_replace_editor" "str_replace_editor"
  assertDeniesRuntimeCommand "tl denies Codex shell redirection" TLRole.config Codex "shell" "cat > src/lib.rs" "shell"
  assertDeniesRuntimeCommand "tl denies Codex python write_text" TLRole.config Codex "shell" "python -c 'from pathlib import Path; Path(\"x\").write_text(\"y\")'" "shell"
  assertAllowsRuntimeCommand "tl allows Codex shell read" TLRole.config Codex "shell" "cat src/lib.rs"
  assertAllowsRuntimeCommand "tl allows Claude Bash redirection passthrough" TLRole.config Claude "Bash" "cat > src/lib.rs"

assertDeniesRuntimeTool :: String -> RoleConfig tools -> Runtime -> Text -> Text -> IO ()
assertDeniesRuntimeTool label_ cfg runtime toolName expectedMessage = do
  output <- runPreToolUseInput cfg (hookInputRuntime runtime toolName)
  assertBool label_ (not (continue_ output))
  assertEqual (label_ <> " decision") (Just "deny") (permissionDecisionOf output)
  assertBool (label_ <> " message") (messageContains expectedMessage output)

assertAllowsRuntimeTool :: String -> RoleConfig tools -> Runtime -> Text -> IO ()
assertAllowsRuntimeTool label_ cfg runtime toolName = do
  output <- runPreToolUseInput cfg (hookInputRuntime runtime toolName)
  assertBool label_ (continue_ output)
  assertEqual (label_ <> " decision") (Just "allow") (permissionDecisionOf output)

assertDeniesRuntimeCommand :: String -> RoleConfig tools -> Runtime -> Text -> Text -> Text -> IO ()
assertDeniesRuntimeCommand label_ cfg runtime toolName command expectedMessage = do
  output <- runPreToolUseInput cfg (commandHookInputRuntime runtime toolName command)
  assertBool label_ (not (continue_ output))
  assertEqual (label_ <> " decision") (Just "deny") (permissionDecisionOf output)
  assertBool (label_ <> " message") (messageContains expectedMessage output)

assertAllowsRuntimeCommand :: String -> RoleConfig tools -> Runtime -> Text -> Text -> IO ()
assertAllowsRuntimeCommand label_ cfg runtime toolName command = do
  output <- runPreToolUseInput cfg (commandHookInputRuntime runtime toolName command)
  assertBool label_ (continue_ output)
  assertEqual (label_ <> " decision") (Just "allow") (permissionDecisionOf output)

assertChainlinkCLIBlockPolicy :: IO ()
assertChainlinkCLIBlockPolicy = do
  let deniedCommands =
        [ "chainlink issue close 1",
          "chainlink issue create title",
          "chainlink issue update 1",
          "chainlink issue block 2 1",
          "chainlink issue relate 2 1",
          "chainlink issue comment 1 note",
          "chainlink subissue create 1 child",
          "chainlink subissue close 2",
          "chainlink session work 310",
          "chainlink session end",
          "chainlink timer start 1",
          "chainlink timer stop 1",
          "chainlink milestone create M1",
          "chainlink close 1",
          "chainlink quick title"
        ]
  forM_ deniedCommands $ \command -> do
    output <- runPreToolUseInput TLRole.config (bashHookInput command)
    assertBool ("tl denies " <> T.unpack command) (not (continue_ output))
    assertEqual ("chainlink deny decision " <> T.unpack command) (Just "deny") (permissionDecisionOf output)
    assertBool ("chainlink deny message " <> T.unpack command) (messageContains "chainlink CLI mutating verbs" output)

  let allowedCommands =
        [ "chainlink issue show 1",
          "chainlink issue list",
          "chainlink issue search lifecycle",
          "chainlink session status",
          "chainlink timer show 1",
          "chainlink timer list"
        ]
  forM_ allowedCommands $ \command -> do
    output <- runPreToolUseInput WorkerRole.config (bashHookInput command)
    assertBool ("worker allows " <> T.unpack command) (continue_ output)
    assertEqual ("chainlink allow decision " <> T.unpack command) (Just "allow") (permissionDecisionOf output)

assertReviewerGitAuthorMutationPolicy :: IO ()
assertReviewerGitAuthorMutationPolicy = do
  let deniedCommands =
        [ "git commit -m x",
          "git commit --amend --no-edit",
          "git rebase main",
          "git cherry-pick abc123",
          "git merge feature",
          "git status && git commit -m sneak"
        ]
  forM_ deniedCommands $ \command -> do
    output <- runPreToolUseInput ReviewerRole.config (bashHookInput command)
    assertBool ("reviewer denies " <> T.unpack command) (not (continue_ output))
    assertEqual ("reviewer git deny decision " <> T.unpack command) (Just "deny") (permissionDecisionOf output)
    assertBool ("reviewer git deny message " <> T.unpack command) (messageContains "Reviewer cannot author or rewrite commits" output)

  let allowedCommands =
        [ "git status",
          "git rev-parse HEAD",
          "git log --oneline",
          "gitk"
        ]
  forM_ allowedCommands $ \command -> do
    output <- runPreToolUseInput ReviewerRole.config (bashHookInput command)
    assertBool ("reviewer allows " <> T.unpack command) (continue_ output)
    assertEqual ("reviewer git allow decision " <> T.unpack command) (Just "allow") (permissionDecisionOf output)

runPreToolUse :: RoleConfig tools -> Text -> IO HookOutput
runPreToolUse cfg toolName = runPreToolUseInput cfg (hookInput toolName)

runPreToolUseInput :: RoleConfig tools -> HookInput -> IO HookOutput
runPreToolUseInput cfg input = do
  status <- runM $ runC $ runFileSystemSuspend $ runAgentControlSuspend (preToolUse (hooks cfg) input)
  case status of
    C.Done output -> pure output
    C.Continue {} -> fail "PreToolUse hook unexpectedly suspended"

runPostToolUse :: RoleConfig tools -> IO HookOutput
runPostToolUse cfg = runPostToolUseFor cfg "Bash"

runPostToolUseFor :: RoleConfig tools -> Text -> IO HookOutput
runPostToolUseFor cfg toolName = do
  status <- runM $ runC $ runFileSystemSuspend $ runAgentControlSuspend (postToolUse (hooks cfg) (hookInputFor PostToolUse toolName))
  case status of
    C.Done output -> pure output
    C.Continue {} -> fail "PostToolUse hook unexpectedly suspended"

hookInput :: Text -> HookInput
hookInput = hookInputFor PreToolUse

bashHookInput :: Text -> HookInput
bashHookInput command = commandHookInputRuntime Claude "Bash" command

hookInputRuntime :: Runtime -> Text -> HookInput
hookInputRuntime runtime toolName =
  (hookInput toolName)
    { hiRuntime = Just runtime
    }

commandHookInputRuntime :: Runtime -> Text -> Text -> HookInput
commandHookInputRuntime runtime toolName command =
  (hookInputRuntime runtime toolName)
    { hiToolInput = Just (Aeson.object ["command" Aeson..= command])
    }

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
      hiChainlinkDb = Nothing,
      hiLlmRequest = Nothing,
      hiLlmResponse = Nothing
    }

assertReviewerPostToolUseEventName :: IO ()
assertReviewerPostToolUseEventName = do
  output <- runPostToolUse ReviewerRole.config
  case hookSpecificOutput output of
    Just (PostToolUseOutput Nothing) -> pure ()
    other -> fail $ "reviewer Bash PostToolUse should emit empty PostToolUseOutput, got " <> show other

  forM_ ["approve_pr", "request_changes"] $ \toolName -> do
    verdictOutput <- runPostToolUseFor ReviewerRole.config toolName
    case hookSpecificOutput verdictOutput of
      Just (PostToolUseOutput (Just ctx)) -> do
        assertBool (T.unpack toolName <> " nudge says exit") (T.isInfixOf "Exit now" ctx)
        assertBool (T.unpack toolName <> " nudge forbids code edits") (T.isInfixOf "do not continue reviewing or edit code" ctx)
      other -> fail $ "reviewer " <> T.unpack toolName <> " PostToolUse should nudge exit, got " <> show other

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
      assertBool "reviewer must not expose send_tmux_message" ("send_tmux_message" `notElem` names)
      assertBool "reviewer must not expose send_mailbox_message" ("send_mailbox_message" `notElem` names)
      assertBool "reviewer must not expose notify_parent" ("notify_parent" `notElem` names)

assertNoRoleExposesShutdown :: IO ()
assertNoRoleExposesShutdown =
  forM_ ["root", "tl", "dev", "worker", "testrunner", "reviewer"] $ \roleName ->
    case lookupRole roleName of
      Nothing -> fail $ "role missing from registry: " <> T.unpack roleName
      Just roleCfg -> do
        let names = map tdName (roleListTools roleCfg)
        assertBool ("role must not expose shutdown: " <> T.unpack roleName) ("shutdown" `notElem` names)

assertReviewerCanExitDecisions :: IO ()
assertReviewerCanExitDecisions = do
  assertBlocks "reviewing" (canExit @ReviewerPhase @ReviewerEvent (ReviewerReviewing 7))
  assertClean "done exits cleanly" (canExit @ReviewerPhase @ReviewerEvent ReviewerDone)
  assertClean "spawned exits cleanly" (canExit @ReviewerPhase @ReviewerEvent ReviewerSpawned)
  assertClean "posted exits cleanly" (canExit @ReviewerPhase @ReviewerEvent (ReviewerPosted 7))

assertReviewerVerdictsAreTerminal :: IO ()
assertReviewerVerdictsAreTerminal = do
  case transition ReviewerSpawned (ReviewerApprovedEv 7) of
    Transitioned ReviewerDone -> pure ()
    _ -> fail "expected ReviewerDone after approval verdict"
  case transition ReviewerSpawned (ReviewerRequestedChangesEv 7 "needs fix") of
    Transitioned ReviewerDone -> pure ()
    _ -> fail "expected ReviewerDone after requested-changes verdict"


assertAppendVerdictLocksPerHeadSha :: IO ()
assertAppendVerdictLocksPerHeadSha = do
  first <- either (fail . T.unpack) pure $ ReviewerRole.appendVerdict 7 "abc123" "approved" "ok" (Just "main.review-pr-7-codex") [] ReviewerRole.emptyReviewFile
  case ReviewerRole.appendVerdict 7 "abc123" "changes_requested" "late finding" (Just "main.review-pr-7-claude") [] first of
    Left msg -> assertBool "duplicate verdict mentions existing SHA" ("already exists" `T.isInfixOf` msg && "abc123" `T.isInfixOf` msg)
    Right _ -> fail "expected duplicate verdict at same SHA to be refused"

assertAppendVerdictAllowsNewHeadSha :: IO ()
assertAppendVerdictAllowsNewHeadSha = do
  first <- either (fail . T.unpack) pure $ ReviewerRole.appendVerdict 7 "abc123" "approved" "ok" (Just "main.review-pr-7-codex") [] ReviewerRole.emptyReviewFile
  second <- either (fail . T.unpack) pure $ ReviewerRole.appendVerdict 7 "def456" "changes_requested" "new round" (Just "main.review-pr-7-codex") [] first
  assertEqual "new SHA verdict count" 2 (length (ReviewerRole.reviewVerdicts second))

assertAppendVerdictRecordsAuthorAndHeadSha :: IO ()
assertAppendVerdictRecordsAuthorAndHeadSha = do
  reviewFile <- either (fail . T.unpack) pure $ ReviewerRole.appendVerdict 7 "abc123" "approved" "ok" (Just "main.review-pr-7-codex") [] ReviewerRole.emptyReviewFile
  case ReviewerRole.reviewVerdicts reviewFile of
    [verdict] -> do
      assertEqual "verdict author branch" (Just "main.review-pr-7-codex") (ReviewerRole.verdictAuthorBranch verdict)
      assertEqual "verdict head sha" (Just "abc123") (ReviewerRole.verdictHeadSha verdict)
    other -> fail $ "expected one verdict, got " <> show (length other)

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

-- The watcher owns CI gating. Once it emits MergeReadyEv, a previously
-- approved dev leaf may exit even if the CI status was already green before
-- the approval verdict was observed.
assertApprovedCanExitOnWatcherMergeReady :: IO ()
assertApprovedCanExitOnWatcherMergeReady = do
  case transition (DevApproved 9) (MergeReadyEv 9 "success" "main.feature") of
    Transitioned DevDone -> pure ()
    other -> fail $ "expected DevDone after watcher merge-ready, got " <> showDevTransition other
  assertBlocks "approved waiting for watcher merge-ready" (canExit @DevPhase @DevEvent (DevApproved 9))

assertCITriggeredMergeReadyTransitionsToDoneAndExits :: IO ()
assertCITriggeredMergeReadyTransitionsToDoneAndExits = do
  case transition (DevApproved 9) (CITriggeredEv 9 "main.feature" "abc123") of
    Transitioned (DevCITriggered 9 "main.feature") -> pure ()
    other -> fail $ "expected DevCITriggered after approval, got " <> showDevTransition other
  case transition (DevCITriggered 9 "main.feature") (MergeReadyEv 9 "success" "main.feature") of
    Transitioned DevDone -> pure ()
    other -> fail $ "expected DevDone after MergeReadyEv from CITriggered, got " <> showDevTransition other
  assertBlocks "ci triggered waiting for result" (canExit @DevPhase @DevEvent (DevCITriggered 9 "main.feature"))
  assertClean "done exits cleanly" (canExit @DevPhase @DevEvent DevDone)

assertCIFailureBlocksAfterTrigger :: IO ()
assertCIFailureBlocksAfterTrigger = do
  case transition (DevCITriggered 9 "main.feature") (CIBlockedEv 9 "failure" "main.feature") of
    Transitioned (DevCIBlocked 9 "failure") -> pure ()
    other -> fail $ "expected DevCIBlocked after failed CI, got " <> showDevTransition other
  assertBlocks "ci blocked terminal" (canExit @DevPhase @DevEvent (DevCIBlocked 9 "failure"))

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
