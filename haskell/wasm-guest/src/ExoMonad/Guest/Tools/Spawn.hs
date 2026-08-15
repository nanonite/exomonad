-- | Hylo spawn primitives: spawn_leaf_subtree, spawn_workers.
--
-- Core I/O functions are role-agnostic. Role-specific MCP wrappers
-- apply their own state transitions.
module ExoMonad.Guest.Tools.Spawn
  ( -- * Marker types
    SpawnLeafSubtree,
    SpawnWorkers,
    SpawnLeaf,
    SpawnWorkerTool,
    CloseWorkerPaneTool,

    -- * Args types
    SpawnLeafSubtreeArgs (..),
    SpawnWorkersArgs (..),
    SpawnLeafArgs (..),
    SpawnWorkerToolArgs (..),
    CloseWorkerPaneArgs (..),
    WorkerSpec (..),
    WorkerType (..),

    -- * Core functions (role wrappers call these)
    spawnLeafSubtreeCore,
    spawnWorkersCore,
    spawnLeafCore,
    spawnWorkerToolCore,
    closeWorkerPaneCore,

    -- * Result types

    -- * Render functions
    spawnLeafRender,

    -- * Shared descriptions/schemas (role wrappers reuse these)
    spawnLeafSubtreeDescription,
    spawnLeafSubtreeSchema,
    spawnWorkersDescription,
    spawnWorkersSchema,
    spawnLeafDescription,
    spawnLeafSchema,
    spawnWorkerToolDescription,
    spawnWorkerToolSchema,
    closeWorkerPaneDescription,
    closeWorkerPaneSchema,

    -- * Helpers (re-exported for role code)
    spawnErrorMessage,
    hasCustomCode,
  )
where

import Control.Monad (forM, void)
import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON, object, withObject, withText, (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.ByteString.Lazy qualified as BSL
import Data.Either (partitionEithers)
import Data.Maybe (fromMaybe)
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Effects.EffectError (Custom (..), EffectError (..), EffectErrorKind (..), InvalidInput (..), NetworkError (..), NotFound (..), PermissionDenied (..), Timeout (..))
import Effects.Git qualified as Git
import Effects.Log qualified as Log
import ExoMonad.Effects.Log (LogEmitEvent)
import ExoMonad.Guest.Effects.AgentControl qualified as AC
import ExoMonad.Guest.Tool.Class (MCPCallOutput (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (JsonSchema (..), genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect_)
import ExoMonad.Guest.Tools.Chainlink.Pure (chainlinkWorkerProtocolText)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

-- ============================================================================
-- Helpers
-- ============================================================================

-- | Helper to convert EffectError to a human-readable message.
spawnErrorMessage :: EffectError -> Text
spawnErrorMessage (EffectError kind) = case kind of
  Just (EffectErrorKindCustom c) -> case customCode c of
    "worktree.branch_exists" -> "Branch already exists. Try a different slug."
    "worktree.push_rejected" -> "Push rejected (non-fast-forward). Remote branch has diverged."
    "worktree.lock_conflict" -> "Git lock file conflict - another git operation may be in progress. Retry in a few seconds."
    _ -> TL.toStrict (customMessage c)
  Just (EffectErrorKindNotFound n) -> "Not found: " <> TL.toStrict (notFoundResource n)
  Just (EffectErrorKindInvalidInput i) -> "Invalid input: " <> TL.toStrict (invalidInputMessage i)
  Just (EffectErrorKindNetworkError n) -> "Network error: " <> TL.toStrict (networkErrorMessage n)
  Just (EffectErrorKindPermissionDenied p) -> "Permission denied: " <> TL.toStrict (permissionDeniedMessage p)
  Just (EffectErrorKindTimeout t) -> "Timeout: " <> TL.toStrict (timeoutMessage t)
  Nothing -> "Unknown effect error"

-- | Helper to check if an EffectError has a specific custom code.
hasCustomCode :: Text -> EffectError -> Bool
hasCustomCode code (EffectError (Just (EffectErrorKindCustom c))) = customCode c == TL.fromStrict code
hasCustomCode _ _ = False

-- ============================================================================
-- SpawnLeafSubtree
-- ============================================================================

data SpawnLeafSubtree

data SpawnLeafSubtreeArgs = SpawnLeafSubtreeArgs
  { slsTask :: Text,
    slsBranchName :: Text,
    slsIntentId :: Maybe Text,
    slsAgentType :: Maybe AC.AgentType,
    slsPermissionMode :: Maybe Text,
    slsAllowedTools :: Maybe [Text],
    slsDisallowedTools :: Maybe [Text],
    slsStandaloneRepo :: Maybe Bool,
    slsAllowedDirs :: Maybe [Text]
  }
  deriving (Show, Eq, Generic)

instance FromJSON SpawnLeafSubtreeArgs where
  parseJSON = withObject "SpawnLeafSubtreeArgs" $ \v ->
    SpawnLeafSubtreeArgs
      <$> v .: "task"
      <*> v .: "branch_name"
      <*> v .:? "intent_id"
      <*> v .:? "agent_type"
      <*> v .:? "permission_mode"
      <*> v .:? "allowed_tools"
      <*> v .:? "disallowed_tools"
      <*> v .:? "standalone_repo"
      <*> v .:? "allowed_dirs"

-- | Shared tool description for spawn_leaf_subtree.
spawnLeafSubtreeDescription :: Text
spawnLeafSubtreeDescription = "Fork a leaf agent into its own worktree and tmux window. Gets dev role (files PR, cannot spawn children). Leaf agents are capable implementers — give them acceptance criteria and file paths, not line-by-line instructions. Claude Code parents should create a team using TeamCreate before spawning Claude Code children. After spawning, return immediately."

-- | Shared tool schema for spawn_leaf_subtree.
spawnLeafSubtreeSchema :: Aeson.Object
spawnLeafSubtreeSchema =
  genericToolSchemaWith @SpawnLeafSubtreeArgs
    [ ("task", "Description of the sub-problem to solve"),
      ("branch_name", "Branch name suffix (will be prefixed with current branch)"),
      ("intent_id", "Controller dispatch intent identifier used to correlate agent.spawned confirmation."),
      ("agent_type", "Agent type for the leaf: 'claude', 'opencode', or 'codex'. Omit to use the server default."),
      ("permission_mode", "Permission mode for the agent. Omit for --dangerously-skip-permissions."),
      ("allowed_tools", "Tool patterns to allow. Omit for no restriction."),
      ("disallowed_tools", "Tool patterns to disallow. Omit for no restriction."),
      ("standalone_repo", "When true, creates a standalone git repo instead of a worktree for information isolation."),
      ("allowed_dirs", "Directories from the parent project to be copied into the agent's context (only for standalone_repo).")
    ]

-- | Core spawn_leaf_subtree I/O.
-- Returns (actualSlug, spawnResult) on success.
spawnLeafSubtreeCore :: SpawnLeafSubtreeArgs -> Eff Effects (Either Text (Text, AC.SpawnResult))
spawnLeafSubtreeCore args = do
  let renderedTask = slsTask args <> "\n\n" <> leafProfileText <> "\n\n" <> leafAcceptanceCriteriaText
      standaloneRepo = fromMaybe False (slsStandaloneRepo args)
      perms =
        AC.PermissionFlags
          { AC.permMode = slsPermissionMode args,
            AC.allowedTools = fromMaybe [] (slsAllowedTools args),
            AC.disallowedTools = fromMaybe [] (slsDisallowedTools args)
          }
      cfg =
        AC.SpawnLeafSubtreeConfig
          { AC.slcTask = renderedTask,
            AC.slcBranchName = slsBranchName args,
            AC.slcIntentId = slsIntentId args,
            AC.slcRole = Nothing,
            AC.slcAgentType = slsAgentType args,
            AC.slcPerms = perms,
            AC.slcStandaloneRepo = standaloneRepo,
            AC.slcAllowedDirs = fromMaybe [] (slsAllowedDirs args)
          }
  result <- AC.spawnLeafSubtree cfg
  case result of
    Left err -> pure $ Left (spawnErrorMessage err)
    Right spawnResult -> do
      emitSpawnEvent (slsIntentId args) (slsBranchName args) "auto" (slsTask args)
      pure $ Right (slsBranchName args, spawnResult)

-- | Render a spawn leaf result to MCPCallOutput.
spawnLeafRender :: Either Text (Text, AC.SpawnResult) -> MCPCallOutput
spawnLeafRender (Left err) = errorResult err
spawnLeafRender (Right (_, sr)) = successResult $ Aeson.toJSON sr

-- ============================================================================
-- SpawnWorkers (batch)
-- ============================================================================

data SpawnWorkers

-- | Worker type determines the completion protocol and allowed operations.
data WorkerType = Implementation | Research
  deriving (Show, Eq, Generic)

instance JsonSchema WorkerType

instance FromJSON WorkerType where
  parseJSON = withText "WorkerType" $ \case
    "implementation" -> pure Implementation
    "research" -> pure Research
    t -> fail $ "Unknown worker type: " <> T.unpack t

data WorkerSpec = WorkerSpec
  { wsName :: Text,
    wsTask :: Text,
    wsIntentId :: Maybe Text,
    wsReadFirst :: Maybe [Text],
    wsSteps :: Maybe [Text],
    wsVerify :: Maybe [Text],
    wsDoneCriteria :: Maybe [Text],
    wsBoundary :: Maybe [Text],
    wsContext :: Maybe Text,
    wsPrompt :: Maybe Text,
    wsProfiles :: Maybe [Text],
    wsContextFiles :: Maybe [Text],
    wsVerifyTemplates :: Maybe [Text],
    wsType :: Maybe WorkerType,
    wsAgentType :: Maybe AC.AgentType,
    wsPermissionMode :: Maybe Text,
    wsAllowedTools :: Maybe [Text],
    wsDisallowedTools :: Maybe [Text]
  }
  deriving (Show, Eq, Generic)

instance JsonSchema WorkerSpec where
  toSchema =
    Aeson.Object $
      genericToolSchemaWith @WorkerSpec
        [ ("name", "Human-readable name for the leaf agent"),
          ("task", "Short description of the task"),
          ("intent_id", "Controller dispatch intent identifier used to correlate agent.spawned confirmation."),
          ("read_first", "Files the agent should read before starting"),
          ("steps", "Numbered implementation steps"),
          ("verify", "Commands to verify the work"),
          ("done_criteria", "Acceptance criteria for completion"),
          ("boundary", "Things the agent must NOT do"),
          ("context", "Freeform context: code snippets, examples, detailed specs"),
          ("prompt", "Raw prompt (escape hatch). If provided, all other fields except name are ignored."),
          ("profiles", "Template profiles to include (e.g., 'general', 'haskell', 'rust')"),
          ("context_files", "Paths to files to include in context"),
          ("verify_templates", "Verification script templates"),
          ("type", "Worker type: 'implementation' (default) or 'research'. Research workers are read-only — they explore, search, and report findings via notify_parent."),
          ("agent_type", "Agent type for the worker: 'claude', 'opencode', or 'codex'. Omit to use the server default."),
          ("permission_mode", "Permission mode for the agent. Omit for --dangerously-skip-permissions."),
          ("allowed_tools", "Tool patterns to allow. Omit for no restriction."),
          ("disallowed_tools", "Tool patterns to disallow. Omit for no restriction.")
        ]

instance FromJSON WorkerSpec where
  parseJSON = withObject "WorkerSpec" $ \v ->
    WorkerSpec
      <$> v .: "name"
      <*> v .: "task"
      <*> v .:? "intent_id"
      <*> v .:? "read_first"
      <*> v .:? "steps"
      <*> v .:? "verify"
      <*> v .:? "done_criteria"
      <*> v .:? "boundary"
      <*> v .:? "context"
      <*> v .:? "prompt"
      <*> v .:? "profiles"
      <*> v .:? "context_files"
      <*> v .:? "verify_templates"
      <*> v .:? "type"
      <*> v .:? "agent_type"
      <*> v .:? "permission_mode"
      <*> v .:? "allowed_tools"
      <*> v .:? "disallowed_tools"

data SpawnWorkersArgs = SpawnWorkersArgs
  { swsSpecs :: [WorkerSpec]
  }
  deriving (Show, Eq, Generic)

instance FromJSON SpawnWorkersArgs where
  parseJSON = withObject "SpawnWorkersArgs" $ \v ->
    SpawnWorkersArgs <$> v .: "specs"

-- | Shared tool description for spawn_workers.
spawnWorkersDescription :: Text
spawnWorkersDescription = "Spawn multiple worker agents in one call. PREFER WORKERS OVER DOING WORK YOURSELF — worker tokens cost far less than TL tokens. Any task you can specify clearly (implementation, research, file edits, test writing) should be a worker. If it touches 2+ files or takes more than 5 tool calls, spawn a worker. Give them acceptance criteria, key file paths, and anti-patterns, not step-by-step code. Each gets a tmux pane in YOUR window, working in YOUR directory on YOUR branch (ephemeral, no isolation, no PR), so the TL worktree must be clean before spawning. Commit the scaffold or discard throwaway output before retrying. Workers are sequential per TL tab; wait for the active worker handoff before spawning another worker, or use spawn_leaf for parallel PR work. Workers send messages via notify_parent. Set type to 'research' for read-only exploration workers that search, read, and report findings without modifying anything. Claude Code parents should create a team using TeamCreate before spawning Claude Code workers. After spawning, return immediately — do not poll or wait."

-- | Shared tool schema for spawn_workers.
spawnWorkersSchema :: Aeson.Object
spawnWorkersSchema =
  genericToolSchemaWith @SpawnWorkersArgs
    [ ("specs", "Array of worker specifications")
    ]

-- | Core spawn_workers I/O. No state transitions (workers are ephemeral).
spawnWorkersCore :: SpawnWorkersArgs -> Eff Effects MCPCallOutput
spawnWorkersCore args = do
  results <- forM (swsSpecs args) $ \spec -> do
    let protocol = case wsType spec of
          Just Research -> researchProfileText
          _ -> workerProfileText
        prompt = case wsPrompt spec of
          Just p -> p
          Nothing -> renderSpec spec <> "\n\n" <> protocol
        perms =
          AC.PermissionFlags
            { AC.permMode = wsPermissionMode spec,
              AC.allowedTools = fromMaybe [] (wsAllowedTools spec),
              AC.disallowedTools = fromMaybe [] (wsDisallowedTools spec)
            }
        cfg =
          AC.SpawnWorkerConfig
            { AC.swcName = wsName spec,
              AC.swcPrompt = prompt,
              AC.swcIntentId = wsIntentId spec,
              AC.swcAgentType = wsAgentType spec,
              AC.swcPerms = perms
            }
    r <- AC.spawnWorker cfg
    case r of
      Right _ -> emitSpawnEvent (wsIntentId spec) (wsName spec) "worker" (wsTask spec)
      Left _ -> pure ()
    pure r
  let (errs, successes) = partitionEithers results
  pure $
    successResult $
      object
        [ "spawned" .= map Aeson.toJSON successes,
          "errors" .= map (Aeson.String . spawnErrorMessage) errs
        ]

-- ============================================================================
-- SpawnLeaf (worktree — branch + PR)
-- ============================================================================

data SpawnLeaf

data SpawnLeafArgs = SpawnLeafArgs
  { slName :: Text,
    slTask :: Text,
    slIntentId :: Maybe Text,
    slAgentType :: Maybe AC.AgentType,
    slReadFirst :: Maybe [Text],
    slSteps :: Maybe [Text],
    slVerify :: Maybe [Text],
    slBoundary :: Maybe [Text],
    slContext :: Maybe Text
  }
  deriving (Show, Eq, Generic)

instance FromJSON SpawnLeafArgs where
  parseJSON = withObject "SpawnLeafArgs" $ \v ->
    SpawnLeafArgs
      <$> v .: "name"
      <*> v .: "task"
      <*> v .:? "intent_id"
      <*> v .:? "agent_type"
      <*> v .:? "read_first"
      <*> v .:? "steps"
      <*> v .:? "verify"
      <*> v .:? "boundary"
      <*> v .:? "context"

spawnLeafDescription :: Text
spawnLeafDescription = "Spawn a leaf agent in its own worktree and branch. The agent gets dev role (files PR, cannot spawn children). The TL worktree must be clean before spawning because dev-leaves fork from branch HEAD and cannot see uncommitted scaffold. Agent type defaults to the server config; pass agent_type only when this leaf needs a specific supported runtime. Use structured fields (steps, verify, boundary) for precise specs, or put everything in task for simple cases. Claude Code parents should create a team using TeamCreate before spawning Claude Code leaves. After spawning, return immediately."

spawnLeafSchema :: Aeson.Object
spawnLeafSchema =
  genericToolSchemaWith @SpawnLeafArgs
    [ ("name", "Branch name suffix (e.g., 'fix-clippy' \x2192 'main.fix-clippy')"),
      ("task", "What to build. Combined with steps/verify/boundary into structured spec"),
      ("intent_id", "Controller dispatch intent identifier used to correlate agent.spawned confirmation."),
      ("agent_type", "Agent type for the leaf: 'claude', 'opencode', or 'codex'. Omit to use the server default."),
      ("steps", "Numbered implementation steps with code snippets and exact file paths"),
      ("verify", "Exact verification commands (e.g., 'cargo test --workspace')"),
      ("boundary", "DO NOT rules for known failure modes"),
      ("context", "Freeform context: code snippets, examples, patterns to follow"),
      ("read_first", "File paths to read before starting (CLAUDE.md, source patterns)")
    ]

spawnLeafCore :: SpawnLeafArgs -> Eff Effects (Either Text (Text, AC.SpawnResult))
spawnLeafCore args = do
  let leafArgs =
        SpawnLeafSubtreeArgs
          { slsTask = buildLeafTask args,
            slsBranchName = slName args,
            slsIntentId = slIntentId args,
            slsAgentType = slAgentType args,
            slsPermissionMode = Nothing,
            slsAllowedTools = Nothing,
            slsDisallowedTools = Nothing,
            slsStandaloneRepo = Just False,
            slsAllowedDirs = Nothing
          }
  spawnLeafSubtreeCore leafArgs

buildLeafTask :: SpawnLeafArgs -> Text
buildLeafTask args =
  let spec =
        WorkerSpec
          { wsName = slName args,
            wsTask = slTask args,
            wsIntentId = Nothing,
            wsReadFirst = slReadFirst args,
            wsSteps = slSteps args,
            wsVerify = slVerify args,
            wsDoneCriteria = Nothing,
            wsBoundary = slBoundary args,
            wsContext = slContext args,
            wsPrompt = Nothing,
            wsProfiles = Nothing,
            wsContextFiles = Nothing,
            wsVerifyTemplates = Nothing,
            wsType = Nothing,
            wsAgentType = Nothing,
            wsPermissionMode = Nothing,
            wsAllowedTools = Nothing,
            wsDisallowedTools = Nothing
          }
   in renderSpec spec

-- ============================================================================
-- SpawnWorkerTool (inline — ephemeral pane, no branch)
-- ============================================================================

data SpawnWorkerTool

data SpawnWorkerToolArgs = SpawnWorkerToolArgs
  { swtName :: Text,
    swtTask :: Text,
    swtIntentId :: Maybe Text,
    swtAgentType :: Maybe AC.AgentType
  }
  deriving (Show, Eq, Generic)

instance FromJSON SpawnWorkerToolArgs where
  parseJSON = withObject "SpawnWorkerToolArgs" $ \v ->
    SpawnWorkerToolArgs
      <$> v .: "name"
      <*> v .: "task"
      <*> v .:? "intent_id"
      <*> v .:? "agent_type"

spawnWorkerToolDescription :: Text
spawnWorkerToolDescription = "Spawn an ephemeral worker in a tmux pane. The worker runs in YOUR directory on YOUR branch \x2014 no isolation, no PR, so the TL worktree must be clean before spawning. Commit the scaffold or discard throwaway output before retrying. Workers are sequential per TL tab; wait for the active worker handoff before spawning another worker, or use spawn_leaf for parallel PR work. Agent type defaults to the server config; pass agent_type only when this worker needs a specific supported runtime. PREFER WORKERS OVER DOING WORK YOURSELF \x2014 worker tokens cost far less than TL tokens. Put everything in the task string: context, instructions, file paths, anti-patterns. Workers send results via notify_parent. Claude Code parents should create a team using TeamCreate before spawning Claude Code workers. After spawning, return immediately."

spawnWorkerToolSchema :: Aeson.Object
spawnWorkerToolSchema =
  genericToolSchemaWith @SpawnWorkerToolArgs
    [ ("name", "Worker name (pane title, messaging identity)"),
      ("task", "The full prompt. Everything the worker needs in one string"),
      ("intent_id", "Controller dispatch intent identifier used to correlate agent.spawned confirmation."),
      ("agent_type", "Agent type for the worker: 'claude', 'opencode', or 'codex'. Omit to use the server default.")
    ]

spawnWorkerToolCore :: SpawnWorkerToolArgs -> Eff Effects MCPCallOutput
spawnWorkerToolCore args = do
  let spec =
        WorkerSpec
          { wsName = swtName args,
            wsTask = swtTask args,
            wsIntentId = swtIntentId args,
            wsReadFirst = Nothing,
            wsSteps = Nothing,
            wsVerify = Nothing,
            wsDoneCriteria = Nothing,
            wsBoundary = Nothing,
            wsContext = Nothing,
            wsPrompt = Nothing,
            wsProfiles = Nothing,
            wsContextFiles = Nothing,
            wsVerifyTemplates = Nothing,
            wsType = Nothing,
            wsAgentType = swtAgentType args,
            wsPermissionMode = Nothing,
            wsAllowedTools = Nothing,
            wsDisallowedTools = Nothing
          }
  spawnWorkersCore (SpawnWorkersArgs [spec])

data CloseWorkerPaneTool

data CloseWorkerPaneArgs = CloseWorkerPaneArgs
  { cwpPaneId :: Text
  }
  deriving (Show, Eq, Generic)

instance FromJSON CloseWorkerPaneArgs where
  parseJSON = withObject "CloseWorkerPaneArgs" $ \v ->
    CloseWorkerPaneArgs <$> v .: "pane_id"

closeWorkerPaneDescription :: Text
closeWorkerPaneDescription = "Close an ephemeral worker tmux pane by pane_id. Use the pane_id returned by spawn_worker after you have received the worker's final result or no longer need the worker pane."

closeWorkerPaneSchema :: Aeson.Object
closeWorkerPaneSchema =
  genericToolSchemaWith @CloseWorkerPaneArgs
    [("pane_id", "Stable tmux pane id returned by spawn_worker, such as '%42'")]

closeWorkerPaneCore :: CloseWorkerPaneArgs -> Eff Effects MCPCallOutput
closeWorkerPaneCore args = do
  result <- AC.closeWorkerPane (cwpPaneId args)
  pure $ case result of
    Left err -> errorResult (spawnErrorMessage err)
    Right resp -> successResult (Aeson.toJSON resp)

-- | Helper to emit 'agent.spawned' event to the host.
emitSpawnEvent :: Maybe Text -> Text -> Text -> Text -> Eff Effects ()
emitSpawnEvent intentId slug agentType taskSummary = do
  let eventPayload =
        BSL.toStrict $
          Aeson.encode $
            object
              ( [ "slug" .= slug,
                  "agent_type" .= agentType,
                  "task_summary" .= taskSummary
                ]
                  <> maybe [] (\value -> ["intent_id" .= value]) intentId
              )
  void $
    suspendEffect_ @LogEmitEvent
      ( Log.EmitEventRequest
          { Log.emitEventRequestEventType = "agent.spawned",
            Log.emitEventRequestPayload = eventPayload,
            Log.emitEventRequestTimestamp = 0
          }
      )

-- ============================================================================
-- Raw Text prompt builders (WASM32 workaround)
-- ============================================================================

-- | Render a structured spec as raw Text (body only, no completion protocol).
-- Callers append the appropriate protocol for the agent's identity.
renderSpec :: WorkerSpec -> Text
renderSpec spec =
  T.intercalate "\n\n" $
    filter (not . T.null) $
      ["## TASK\n" <> wsTask spec]
        <> maybe [] (\items -> ["## BOUNDARY\n" <> T.intercalate "\n" (map ("- " <>) items)]) (wsBoundary spec)
        <> maybe [] (\items -> ["## READ FIRST\n" <> T.intercalate "\n" (map ("- " <>) items)]) (wsReadFirst spec)
        <> maybe [] (\items -> ["## STEPS\n" <> T.intercalate "\n" (zipWith (\i s -> T.pack (show (i :: Int)) <> ". " <> s) [1 ..] items)]) (wsSteps spec)
        <> maybe [] (\t -> if T.null t then [] else ["## CONTEXT\n" <> t]) (wsContext spec)
        <> maybe [] (\items -> ["## VERIFY\n" <> T.intercalate "\n" (map (\c -> "- `" <> c <> "`") items)]) (wsVerify spec)
        <> maybe [] (\items -> ["## DONE CRITERIA\n" <> T.intercalate "\n" (map ("- " <>) items)]) (wsDoneCriteria spec)

-- | Pre-rendered leaf profile text.
leafProfileText :: Text
leafProfileText = "## Completion Protocol (Leaf Subtree)\nYou are a **leaf agent** in your own git worktree and branch. Your branch name follows the pattern `{parent}.{slug}`.\nOne invocation handles one assignment: receive the task, implement it, publish the authoritative result, and exit cleanly.\nOne-shot does not mean non-interactive. While this invocation is alive, continue consuming durable inbox guidance delivered through the validated tmux pane for this exact invocation; never redirect a stale target to the root pane.\n\nWhen you are done:\n\n1. **Commit your changes** with a descriptive message.\n   - `git add <specific files>` \x2014 NEVER `git add .` or `git add -A`\n   - `git commit -m \"feat: <description>\"`\n2. **File a PR** using `file_pr` as the authoritative publication. The base branch is auto-detected from your branch name.\n3. **Use `notify_parent` to send status updates** \x2014 e.g., \"PR filed\" or \"hit a blocker, need guidance.\" Call with `failure` status to escalate problems. Never use `send_message` with recipient `parent`; `parent` is a reserved alias resolved only by `notify_parent`.\n4. **Exit after publishing the assignment result.** Do not idle for reviewer approval, CI, merge-ready, or merge; the watcher owns those state-machine inputs and notifies the TL.\n5. If later review changes require work after this process exits, the TL uses `resume_pr`: that starts a fresh invocation in this same owner worktree, branch, and PR, with pending inbox guidance visible at startup. Do not create a new owner or stacked PR.\n\n**DO NOT:**\n- Merge your own PR (the parent TL merges)\n- Push to main or any branch other than your own\n- Create additional branches\n- Wait for merge-ready after the assignment is published"

leafAcceptanceCriteriaText :: Text
leafAcceptanceCriteriaText =
  "## PR BODY CONTRACT\nBefore the next `file_pr` call, make the PR body contain the literal heading `## Acceptance Criteria`. Copy every issue Definition-of-Done bullet verbatim beneath it, and preserve or update that heading on resumed work."

-- | Pre-rendered worker profile text.
workerProfileText :: Text
workerProfileText =
  "## Completion Protocol (Worker)\nYou are an **ephemeral worker** \x2014 you run in the parent's directory on the parent's branch. You do NOT have your own worktree or branch.\n\nWhen you are done:\n\n1. **Call `notify_parent`** with status `success` and a DETAILED message containing your complete findings. Never use `send_message` with recipient `parent`; `parent` is a reserved alias resolved only by `notify_parent`.\n   - Include FULL code snippets, exact file paths with line numbers, and concrete data.\n   - Your parent CANNOT see your terminal output. `notify_parent` is your ONLY communication channel.\n   - A terse summary like \"Task complete\" is useless \x2014 include everything the parent needs to act on your findings.\n   - For research tasks: include the actual code/data you found, not just \"I found it.\"\n   - For implementation tasks: describe exactly what you changed and how to verify it.\n2. If you failed after multiple attempts, call `notify_parent` with status `failure` and explain what went wrong.\n\n**DO NOT:**\n- Commit, push, or file PRs (you are ephemeral \x2014 the parent owns the branch)\n- Create new branches\n- Run `git checkout` or `git switch`\n- Print findings to stdout instead of sending them via `notify_parent`"
    <> "\n\n"
    <> chainlinkWorkerProtocolText

-- | Pre-rendered research worker profile text.
researchProfileText :: Text
researchProfileText = "## Completion Protocol (Research Worker)\nYou are a **research worker** \x2014 your job is to explore, read, search, and synthesize. You do NOT modify anything.\n\nYour workflow:\n\n1. **Read files** (`Read`, `Glob`, `Grep`) to understand code structure and patterns.\n2. **Search broadly** \x2014 check multiple files, grep for patterns, follow imports and references.\n3. **Synthesize findings** into a clear, structured report.\n4. **Call `notify_parent`** with status `success` and your findings. Never use `send_message` with recipient `parent`; `parent` is a reserved alias resolved only by `notify_parent`.\n   - Structure your report with headings, bullet points, and code references (file:line).\n   - Lead with the answer, then supporting evidence.\n   - If you cannot find what was asked, call `notify_parent` with status `failure` explaining what you searched and what was missing.\n\n**DO NOT:**\n- Edit, write, or create any files\n- Run git commands (commit, push, checkout, branch)\n- File PRs or run build commands\n- Make changes to the codebase in any way"
