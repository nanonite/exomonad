{-# LANGUAGE DataKinds #-}
{-# LANGUAGE FlexibleContexts #-}
{-# LANGUAGE NamedFieldPuns #-}
{-# LANGUAGE OverloadedStrings #-}

module Main where

import AllRoles (lookupRole, roleListTools)
import Control.Monad (forM_, unless)
import Control.Monad.Freer (Eff, runM)
import Control.Monad.Freer.Coroutine (runC)
import Control.Monad.Freer.Coroutine qualified as C
import Data.Aeson (Value)
import Data.Aeson qualified as Aeson
import Data.ByteString qualified as BS
import Data.ByteString.Lazy qualified as BL
import Data.ByteString.Lazy.Char8 qualified as BSL
import Data.Map.Strict qualified as Map
import Data.Text (Text)
import Data.Text qualified as T
import Data.Word (Word8)
import DevPhase (DevEvent (..), DevPhase (..))
import DevRole qualified
import Effects.Envelope qualified as Envelope
import Effects.Git qualified as Git
import Effects.Kv qualified as KV
import Effects.Log qualified as Log
import ExoMonad.Guest.Effects.AgentControl (runAgentControlSuspend)
import ExoMonad.Guest.Effects.AgentControl qualified as AgentControl
import ExoMonad.Guest.Effects.FileSystem (runFileSystemSuspend)
import ExoMonad.Guest.Events (CIStatusEvent (..), EventAction (..), EventHandlerConfig (..), PRReviewEvent (..))
import ExoMonad.Guest.Events.Templates qualified as Tpl
import ExoMonad.Guest.Prompt qualified as Prompt
import ExoMonad.Guest.ReviewHandoff (ReviewFixTask (..), renderReviewFixTask, reviewHandoffInstructions)
import ExoMonad.Guest.StateMachine (StateMachine (..), StopCheckResult (..), TransitionResult (..))
import ExoMonad.Guest.StateMachine qualified as StateMachine
import ExoMonad.Guest.Tool.Class (ToolDefinition (tdDescription, tdName))
import ExoMonad.Guest.Tool.Suspend.Types (EffectRequest (..))
import ExoMonad.Guest.Tools.FilePR (filePRDescription, filePRSchema)
import ExoMonad.Guest.Tools.MergePR (MergePRArgs (..), mergePRDescription, mergePRSchema)
import ExoMonad.Guest.Tools.ResumePr (ResumePrArgs (..), renderResumePrTask, resumePrDescription, resumePrSchema)
import ExoMonad.Guest.Tools.Spawn (forkWaveSchema, spawnLeafSchema, spawnWorkersSchema)
import ExoMonad.Guest.Types (Effects, HookEventType (..), HookInput (..), HookOutput (..), HookSpecificOutput (..), Runtime (..))
import ExoMonad.Types (ChainlinkDbPathState (..), HookConfig (..), RoleConfig (..), validateChainlinkDbEnv)
import Proto3.Suite.Class (Message, toLazyByteString)
import ReviewerPhase (ReviewerEvent (..), ReviewerPhase (..))
import ReviewerRole qualified
import RootRole qualified
import System.Environment (getArgs)
import TLPhase (ChildHandle (..), TLEvent (..), TLPhase (..))
import TLRole qualified
import WorkerRole qualified

denyTools :: [Text]
denyTools = ["Edit", "Write", "MultiEdit", "NotebookEdit"]

allowTools :: [Text]
allowTools = ["Read", "Grep", "Bash", "spawn_leaf", "spawn_worker", "send_tmux_message", "send_mailbox_message"]

main :: IO ()
main = do
  args <- getArgs
  case args of
    ["--tl-phase-golden", sourceHash] -> emitTLPhaseGolden (T.pack sourceHash)
    _ -> runRoleHookTests

runRoleHookTests :: IO ()
runRoleHookTests = do
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
  assertPublishedDevPhasesExit
  assertReviewApprovedAfterFixRoundTransitionsToApproved
  assertReviewApprovedFromUnderReviewRoundZero
  assertFixesPushedFromChangesRequestedYieldsRoundOne
  assertFixesPushedIncrementsUnderReviewRound
  assertApprovedCanExitOnWatcherMergeReady
  assertCITriggeredMergeReadyTransitionsToDoneAndExits
  assertCIFailureBlocksAfterTrigger
  assertMergeReadyReviewLeavesParentToWatcher
  assertMergeReadyCIStatusLeavesParentToWatcher
  assertReviewCommentedJSONAndHandler
  assertRequestedChangesDeliverOwnerReviewMessage
  assertTLReviewHandlerPreservesReviewMetadata
  assertReviewerFacingTextDoesNotMentionCopilot
  assertAcceptanceCriteriaContract
  assertReviewerAcceptanceCriteriaGuidance
  assertSpawnSchemasPreserveRetiredBoundary

emitTLPhaseGolden :: Text -> IO ()
emitTLPhaseGolden sourceHash =
  BL.putStr . Aeson.encode $
    Aeson.object
      [ "source_blob_hash" Aeson..= sourceHash,
        "rows" Aeson..= map goldenRow (phaseSamples `cross` eventSamples)
      ]

phaseSamples :: [TLPhase]
phaseSamples =
  [ TLPlanning,
    TLDispatching,
    TLWaiting sampleChildren,
    TLMerging 7 sampleChildren,
    TLAllMerged,
    TLPRFiled 12 "https://forgejo.example/pulls/12",
    TLDone,
    TLFailed "child failed"
  ]

eventSamples :: [TLEvent]
eventSamples =
  [ ChildSpawned (ChildHandle "c" "main.c" "codex"),
    ChildCompleted "a",
    ChildFailed "a" "timed out",
    PRMerged 7 "a",
    AllChildrenDone,
    OwnPRFiled 12 "https://forgejo.example/pulls/12" "main.tl"
  ]

sampleChildren :: Map.Map Text ChildHandle
sampleChildren =
  Map.fromList
    [ ("a", ChildHandle "a" "main.a" "codex"),
      ("b", ChildHandle "b" "main.b" "claude")
    ]

goldenRow :: (TLPhase, TLEvent) -> Value
goldenRow (phase, event) =
  Aeson.object
    [ "phase" Aeson..= Aeson.toJSON phase,
      "event" Aeson..= eventJSON event,
      "result" Aeson..= transitionJSON phase event
    ]

transitionJSON :: TLPhase -> TLEvent -> Value
transitionJSON phase event
  | not (legalTransition phase event) = Aeson.String "illegal"
  | otherwise =
      case StateMachine.transition phase event of
        Transitioned result -> Aeson.toJSON result
        InvalidTransition _ -> Aeson.String "illegal"

legalTransition :: TLPhase -> TLEvent -> Bool
legalTransition _ (ChildSpawned _) = True
legalTransition (TLWaiting _) (ChildCompleted _) = True
legalTransition _ (ChildCompleted _) = False
legalTransition _ (ChildFailed _ _) = True
legalTransition (TLWaiting _) (PRMerged _ _) = True
legalTransition (TLMerging _ _) (PRMerged _ _) = True
legalTransition _ (PRMerged _ _) = False
legalTransition _ AllChildrenDone = True
legalTransition _ (OwnPRFiled _ _ _) = True

cross :: [a] -> [b] -> [(a, b)]
cross left right = [(x, y) | x <- left, y <- right]

eventJSON :: TLEvent -> Value
eventJSON (ChildSpawned handle) = Aeson.object ["event" Aeson..= ("child_spawned" :: Text), "handle" Aeson..= handle]
eventJSON (ChildCompleted slug) = Aeson.object ["event" Aeson..= ("child_completed" :: Text), "slug" Aeson..= slug]
eventJSON (ChildFailed slug reason) = Aeson.object ["event" Aeson..= ("child_failed" :: Text), "slug" Aeson..= slug, "reason" Aeson..= reason]
eventJSON (PRMerged prNumber slug) = Aeson.object ["event" Aeson..= ("pr_merged" :: Text), "pr_number" Aeson..= prNumber, "slug" Aeson..= slug]
eventJSON AllChildrenDone = Aeson.object ["event" Aeson..= ("all_children_done" :: Text)]
eventJSON (OwnPRFiled prNumber url branch) = Aeson.object ["event" Aeson..= ("own_pr_filed" :: Text), "pr_number" Aeson..= prNumber, "url" Aeson..= url, "branch" Aeson..= branch]

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
  assertDeniesRuntimeTool "root denies Codex apply_patch" RootRole.config Codex "apply_patch" "apply_patch"
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
        ["approve_pr", "request_changes", "post_review_comment", "check_inbox", "list_agents"]
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
  assertClean "needs human direction exits for TL resume" (canExit @DevPhase @DevEvent (DevNeedsHumanDirection 9 "still wrong"))

assertPublishedDevPhasesExit :: IO ()
assertPublishedDevPhasesExit = do
  assertClean "PR-filed invocation exits after handoff" (canExit @DevPhase @DevEvent (DevPRFiled 9 "https://forgejo/pr/9"))
  assertClean "under-review invocation exits after handoff" (canExit @DevPhase @DevEvent (DevUnderReview 9 1))
  assertClean "approved invocation exits while watcher owns CI" (canExit @DevPhase @DevEvent (DevApproved 9))
  assertClean "CI-triggered invocation exits while watcher owns CI" (canExit @DevPhase @DevEvent (DevCITriggered 9 "main.feature"))
  assertClean "CI-blocked invocation exits for TL decision" (canExit @DevPhase @DevEvent (DevCIBlocked 9 "failure"))

-- Intended semantics: after the dev has pushed a fix (round_ >= 1), an
-- approval verdict must transition to DevApproved, NOT DevNeedsHumanDirection.
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
  assertClean "approved invocation exits without merge-ready" (canExit @DevPhase @DevEvent (DevApproved 9))

assertCITriggeredMergeReadyTransitionsToDoneAndExits :: IO ()
assertCITriggeredMergeReadyTransitionsToDoneAndExits = do
  case transition (DevApproved 9) (CITriggeredEv 9 "main.feature" "abc123") of
    Transitioned (DevCITriggered 9 "main.feature") -> pure ()
    other -> fail $ "expected DevCITriggered after approval, got " <> showDevTransition other
  case transition (DevCITriggered 9 "main.feature") (MergeReadyEv 9 "success" "main.feature") of
    Transitioned DevDone -> pure ()
    other -> fail $ "expected DevDone after MergeReadyEv from CITriggered, got " <> showDevTransition other
  assertClean "ci-triggered invocation exits without waiting" (canExit @DevPhase @DevEvent (DevCITriggered 9 "main.feature"))
  assertClean "done exits cleanly" (canExit @DevPhase @DevEvent DevDone)

assertCIFailureBlocksAfterTrigger :: IO ()
assertCIFailureBlocksAfterTrigger = do
  case transition (DevCITriggered 9 "main.feature") (CIBlockedEv 9 "failure" "main.feature") of
    Transitioned (DevCIBlocked 9 "failure") -> pure ()
    other -> fail $ "expected DevCIBlocked after failed CI, got " <> showDevTransition other
  assertClean "ci-blocked invocation exits for TL decision" (canExit @DevPhase @DevEvent (DevCIBlocked 9 "failure"))

assertMergeReadyReviewLeavesParentToWatcher :: IO ()
assertMergeReadyReviewLeavesParentToWatcher = do
  action <- runPRReviewEvent DevRole.config (MergeReady 9 "success" "main.feature")
  assertNoAction "merge-ready pr_review dev handler" action

assertMergeReadyCIStatusLeavesParentToWatcher :: IO ()
assertMergeReadyCIStatusLeavesParentToWatcher = do
  action <-
    runCIStatusEvent
      DevRole.config
      (CIStatusEvent 9 "success" "main.feature" False True True)
  assertNoAction "merge-ready ci_status dev handler" action

assertReviewCommentedJSONAndHandler :: IO ()
assertReviewCommentedJSONAndHandler = do
  let event = ReviewCommented 9 "Looks good, with one suggestion." "main.feature-codex" (Just "main.review-pr-9-codex")
  case Aeson.fromJSON (Aeson.toJSON event) of
    Aeson.Success (ReviewCommented n comments_ branch authorBranch) -> do
      assertEqual "comment-only review PR number" 9 n
      assertEqual "comment-only review body" "Looks good, with one suggestion." comments_
      assertEqual "comment-only review head branch" "main.feature-codex" branch
      assertEqual "comment-only review author branch" (Just "main.review-pr-9-codex") authorBranch
    other -> fail $ "comment-only review JSON roundtrip failed: " <> show other
  action <- runPRReviewEvent DevRole.config event
  case action of
    InjectMessage message -> do
      assertBool "comment-only review dev message includes PR" ("[REVIEW COMMENT] PR #9" `T.isInfixOf` message)
      assertBool "comment-only review dev message includes body" ("Looks good, with one suggestion." `T.isInfixOf` message)
    other -> fail $ "comment-only review dev handler should inject the review, got " <> show other

assertRequestedChangesDeliverOwnerReviewMessage :: IO ()
assertRequestedChangesDeliverOwnerReviewMessage = do
  reviewAction <- runPRReviewEvent DevRole.config (ReviewReceived 12 "Please update the timeout path." "main.feature-codex" (Just "main.review-pr-12-codex"))
  assertReviewMessage "review received dev handler" "## Review on PR #12" "Please update the timeout path." reviewAction

  let event = ReviewerRequestedChanges 10 "Please fix the error path." "main.feature-codex" (Just "main.review-pr-10-codex")
  action <- runPRReviewEvent DevRole.config event
  assertReviewMessage "requested-changes dev handler" "## Review on PR #10" "Please fix the error path." action

assertReviewMessage :: String -> Text -> Text -> EventAction -> IO ()
assertReviewMessage label_ heading body action =
  case action of
    InjectMessage message -> do
      assertBool (label_ <> " includes heading") (heading `T.isInfixOf` message)
      assertBool (label_ <> " includes body") (body `T.isInfixOf` message)
    other -> fail $ label_ <> " should inject the review, got " <> show other

assertTLReviewHandlerPreservesReviewMetadata :: IO ()
assertTLReviewHandlerPreservesReviewMetadata = do
  let event = ReviewCommented 11 "Consider this edge case." "main.subtl.feature-codex" (Just "main.review-pr-11-codex")
  action <- runPRReviewEvent TLRole.config event
  case action of
    InjectMessage message -> do
      assertBool "TL review handler includes PR" ("PR #11" `T.isInfixOf` message)
      assertBool "TL review handler includes head branch" ("main.subtl.feature-codex" `T.isInfixOf` message)
      assertBool "TL review handler includes reviewer branch" ("main.review-pr-11-codex" `T.isInfixOf` message)
      assertBool "TL review handler includes body" ("Consider this edge case." `T.isInfixOf` message)
    other -> fail $ "TL review handler should inject a parent-formatted message, got " <> show other

runPRReviewEvent :: RoleConfig tools -> PRReviewEvent -> IO EventAction
runPRReviewEvent cfg event =
  runEventHandler "PR review" (onPRReview (eventHandlers cfg) event)

runCIStatusEvent :: RoleConfig tools -> CIStatusEvent -> IO EventAction
runCIStatusEvent cfg event =
  runEventHandler "CI status" (onCIStatus (eventHandlers cfg) event)

runEventHandler :: String -> Eff Effects EventAction -> IO EventAction
runEventHandler label_ action = do
  status <- runM $ runC $ runFileSystemSuspend $ runAgentControlSuspend action
  resumeEventHandler label_ status

resumeEventHandler :: String -> C.Status '[IO] EffectRequest Value EventAction -> IO EventAction
resumeEventHandler _ (C.Done output) = pure output
resumeEventHandler label_ (C.Continue request resume) = do
  response <- eventEffectResponse label_ request
  next <- runM (resume response)
  resumeEventHandler label_ next

eventEffectResponse :: String -> EffectRequest -> IO Value
eventEffectResponse label_ request =
  case erType request of
    "git.get_branch" -> pure $ responseValue (Git.GetBranchResponse "main.feature" False)
    "kv.get" -> pure $ responseValue (KV.GetResponse False "")
    "kv.set" -> pure $ responseValue (KV.SetResponse True)
    "log.info" -> pure $ responseValue (Log.LogResponse True)
    "log.warn" -> pure $ responseValue (Log.LogResponse True)
    other -> fail $ label_ <> " event unexpectedly suspended on " <> T.unpack other

responseValue :: (Message payload) => payload -> Value
responseValue payload =
  let payloadBytes = BL.toStrict (toLazyByteString payload)
      response = Envelope.EffectResponse (Just (Envelope.EffectResponseResultPayload payloadBytes))
   in Aeson.toJSON (BS.unpack (BL.toStrict (toLazyByteString response)) :: [Word8])

assertNoAction :: String -> EventAction -> IO ()
assertNoAction label_ action =
  case action of
    NoAction -> pure ()
    other -> fail $ label_ <> ": expected NoAction, got " <> show other

assertReviewerFacingTextDoesNotMentionCopilot :: IO ()
assertReviewerFacingTextDoesNotMentionCopilot = do
  assertNoCopilot "merge_pr description" mergePRDescription
  let mergePrSchemaText = T.pack (BSL.unpack (Aeson.encode mergePRSchema))
  assertNoCopilot "merge_pr schema" mergePrSchemaText
  assertContains "merge_pr schema" "chainlink_issue_id" mergePrSchemaText
  assertNoCopilot "prReady" (Tpl.prReady 42)
  assertNoCopilot "reviewTimeout" (Tpl.reviewTimeout 42 15)
  assertContains "merge_pr description" "Forgejo reviewer" mergePRDescription
  assertContains "merge_pr description issue id" "chainlink_issue_id" mergePRDescription
  assertContains "prReady" "Forgejo reviewer" (Tpl.prReady 42)
  assertContains "reviewTimeout" "Forgejo reviewer" (Tpl.reviewTimeout 42 15)
  case Aeson.fromJSON (Aeson.object ["pr_number" Aeson..= (7 :: Int), "chainlink_issue_id" Aeson..= (42 :: Int)]) of
    Aeson.Success args -> assertEqual "merge_pr parses chainlink_issue_id" (Just 42) (mprChainlinkIssueId args)
    Aeson.Error err -> fail $ "merge_pr args parse failed: " <> err

assertAcceptanceCriteriaContract :: IO ()
assertAcceptanceCriteriaContract = do
  let heading = "## Acceptance Criteria"
      filePrSchemaText = T.pack (BSL.unpack (Aeson.encode filePRSchema))
      resumePrSchemaText = T.pack (BSL.unpack (Aeson.encode resumePrSchema))
      resumeArgs =
        ResumePrArgs
          { rpaPrNumber = 104,
            rpaTask = "Repair the reviewed PR",
            rpaReadFirst = Nothing,
            rpaSteps = Nothing,
            rpaVerify = Nothing,
            rpaBoundary = Nothing,
            rpaContext = Nothing,
            rpaDoneCriteria = Just ["Preserve this issue bullet verbatim"]
          }
      renderedResume = renderResumePrTask resumeArgs
      renderedReviewHandoff =
        renderReviewFixTask
          ReviewFixTask
            { reviewFixTask = "Repair the reviewed PR",
              reviewFixBoundary = Nothing,
              reviewFixReadFirst = Nothing,
              reviewFixSteps = Nothing,
              reviewFixContext = Nothing,
              reviewFixVerify = Nothing,
              reviewFixDoneCriteria = Just ["Preserve this issue bullet verbatim"]
            }
  assertContains "file_pr description heading" heading filePRDescription
  assertContains "file_pr description verbatim" "copied verbatim" filePRDescription
  assertContains "file_pr schema heading" heading filePrSchemaText
  assertContains "file_pr schema Definition of Done" "Definition-of-Done" filePrSchemaText
  assertContains "resume_pr description heading" heading resumePrDescription
  assertContains "resume_pr description done criteria" "done_criteria" resumePrDescription
  assertContains "resume_pr description preserve" "do not silently drop" resumePrDescription
  assertContains "resume_pr schema heading" heading resumePrSchemaText
  assertContains "resume_pr schema verbatim" "copy them verbatim" resumePrSchemaText
  assertContains "resume task heading" heading renderedResume
  assertContains "resume task done criteria" "Preserve this issue bullet verbatim" renderedResume
  assertContains "review handoff heading" heading reviewHandoffInstructions
  assertContains "review handoff file_pr" "next `file_pr` call" reviewHandoffInstructions
  assertContains "review handoff done criteria" "DONE CRITERIA" reviewHandoffInstructions
  assertContains "rendered handoff heading" heading renderedReviewHandoff
  assertContains "rendered handoff done criteria" "Preserve this issue bullet verbatim" renderedReviewHandoff
  assertContains "leaf prompt heading" heading (Prompt.render Prompt.leafProfile)
  assertContains "leaf prompt Definition of Done" "Definition-of-Done" (Prompt.render Prompt.leafProfile)
  assertContains "leaf prompt one assignment" "one assignment" (Prompt.render Prompt.leafProfile)
  assertContains "leaf prompt exact live pane" "validated tmux pane" (Prompt.render Prompt.leafProfile)
  assertContains "leaf prompt resume invocation" "resume_pr" (Prompt.render Prompt.leafProfile)
  assertBool "leaf prompt does not wait for merge-ready" (not ("Stop only after merge-ready" `T.isInfixOf` Prompt.render Prompt.leafProfile))

assertSpawnSchemasPreserveRetiredBoundary :: IO ()
assertSpawnSchemasPreserveRetiredBoundary = do
  let retiredProvider = T.concat ["ge", "mini"]
      schemaText schema = T.pack (BSL.unpack (Aeson.encode schema))
      schemas =
        [ ("fork_wave", schemaText forkWaveSchema),
          ("spawn_leaf", schemaText spawnLeafSchema),
          ("spawn_workers", schemaText spawnWorkersSchema)
        ]
  forM_ schemas $ \(toolName, schema) ->
    assertBool (toolName <> " schema omits retired provider") (not (retiredProvider `T.isInfixOf` schema))
  let parsed = Aeson.fromJSON (Aeson.String retiredProvider) :: Aeson.Result AgentControl.AgentType
  case parsed of
    Aeson.Success AgentControl.Retired -> pure ()
    Aeson.Success value -> fail ("unexpected compatibility agent type: " <> show value)
    Aeson.Error message -> fail ("retired provider did not reach the effect boundary: " <> message)

assertReviewerAcceptanceCriteriaGuidance :: IO ()
assertReviewerAcceptanceCriteriaGuidance = do
  let guidance = ReviewerRole.reviewerAcceptanceCriteriaGuidance
  assertContains "reviewer guidance heading" "## Acceptance Criteria" guidance
  assertContains "reviewer guidance authoritative" "authoritative" guidance
  assertContains "reviewer guidance diff" "diff and tests" guidance
  assertContains "reviewer guidance missing" "heading is missing" guidance
  assertContains "reviewer guidance no guessing" "Do not invent or guess" guidance
  case lookupRole "reviewer" of
    Nothing -> fail "reviewer role missing from registry"
    Just roleCfg ->
      forM_ ["approve_pr", "request_changes", "post_review_comment"] $ \toolName ->
        case [tdDescription definition | definition <- roleListTools roleCfg, tdName definition == toolName] of
          [description] -> assertContains (T.unpack toolName <> " reviewer guidance") "## Acceptance Criteria" description
          other -> fail $ "expected one reviewer tool definition for " <> T.unpack toolName <> ", got " <> show other

assertNoCopilot :: String -> Text -> IO ()
assertNoCopilot label_ value =
  assertBool (label_ <> " should not mention Copilot") (not ("Copilot" `T.isInfixOf` value))

assertContains :: String -> Text -> Text -> IO ()
assertContains label_ expected value =
  assertBool (label_ <> " should mention " <> T.unpack expected) (expected `T.isInfixOf` value)

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
