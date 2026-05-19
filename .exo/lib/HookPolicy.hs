{-# LANGUAGE OverloadedStrings #-}

-- | Shared hook policy guards for agent shell commands.
module HookPolicy
  ( blockGhCommand,
    blockChainlinkSqliteCommand,
    blockGitAuthorMutation,
    blockImplementationMutation,
    implementerToolsFor,
    preToolUseWithGhBlock,
    preToolUseWithGitAuthorBlock,
    preToolUseWithImplementationBlock,
    preToolUseWithGitAuthorAndImplementationBlock,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (Value (..))
import Data.Aeson.KeyMap qualified as KM
import Data.List (tails)
import Data.Text (Text)
import Data.Text qualified as T
import ExoMonad.Guest.Types (HookInput (..), HookOutput, Runtime (..), denyResponse)
import ExoMonad.Types (Effects)

blockGhCommand :: HookInput -> Maybe Text
blockGhCommand hookInput =
  commandFromHookInput hookInput >>= \cmd ->
    if containsGhToken cmd
      then
        Just $
          "BLOCKED: Do not run gh commands from agents. "
            <> "Use ExoMonad MCP tools such as file_pr, merge_pr, and local review flow instead."
      else Nothing

blockChainlinkSqliteCommand :: HookInput -> Maybe Text
blockChainlinkSqliteCommand hookInput =
  commandFromHookInput hookInput >>= \cmd ->
    if accessesChainlinkSqlite cmd
      then
        Just $
          "BLOCKED: Do not access Chainlink sqlite databases directly. "
            <> "Use the scoped Chainlink MCP tools instead."
      else Nothing

blockGitAuthorMutation :: HookInput -> Maybe Text
blockGitAuthorMutation hookInput =
  commandFromHookInput hookInput >>= \cmd ->
    if hasCommitMutatingGitCommand cmd
      then Just reviewerGitAuthorMutationMessage
      else Nothing

blockImplementationMutation :: (Text -> Text) -> HookInput -> Maybe Text
blockImplementationMutation renderMessage hookInput =
  case hiToolName hookInput of
    Just toolName
      | toolName `elem` implementerToolsFor runtime ->
          Just (renderMessage toolName)
      | toolName `elem` shellToolsFor runtime,
        Just cmd <- commandFromHookInput hookInput,
        commandWritesFiles cmd ->
          Just (renderMessage toolName)
    _ -> Nothing
  where
    runtime = runtimeFromHookInput hookInput

implementerToolsFor :: Runtime -> [Text]
implementerToolsFor Claude = ["Edit", "Write", "MultiEdit", "NotebookEdit"]
implementerToolsFor Codex = ["apply_patch", "str_replace_editor", "edit_file"]
implementerToolsFor OpenCode = ["edit", "write", "patch"]
implementerToolsFor Gemini = []

reviewerGitAuthorMutationMessage :: Text
reviewerGitAuthorMutationMessage =
  "Reviewer cannot author or rewrite commits -- provenance must remain with the worktree owner. Use `request_changes`/`post_review_comment` to send the fix back to the worker. Read-only git commands (status, diff, log, show, fetch, rev-parse, symbolic-ref) remain available for inspecting the PR's HEAD."

commandFromHookInput :: HookInput -> Maybe Text
commandFromHookInput hookInput =
  case hiToolInput hookInput of
    Just (Object obj)
      | Just (String cmd) <- KM.lookup "command" obj ->
          Just cmd
      | Just (String cmd) <- KM.lookup "cmd" obj ->
          Just cmd
      | Just (String cmd) <- KM.lookup "script" obj ->
          Just cmd
    _ -> Nothing

preToolUseWithGhBlock ::
  (HookInput -> Eff Effects HookOutput) ->
  HookInput ->
  Eff Effects HookOutput
preToolUseWithGhBlock next hookInput =
  case blockGhCommand hookInput of
    Just reason -> pure (denyResponse reason)
    Nothing ->
      case blockChainlinkSqliteCommand hookInput of
        Just reason -> pure (denyResponse reason)
        Nothing -> next hookInput

preToolUseWithGitAuthorBlock ::
  (HookInput -> Eff Effects HookOutput) ->
  HookInput ->
  Eff Effects HookOutput
preToolUseWithGitAuthorBlock next hookInput =
  case blockGitAuthorMutation hookInput of
    Just reason -> pure (denyResponse reason)
    Nothing -> preToolUseWithGhBlock next hookInput

preToolUseWithImplementationBlock ::
  (Text -> Text) ->
  (HookInput -> Eff Effects HookOutput) ->
  HookInput ->
  Eff Effects HookOutput
preToolUseWithImplementationBlock renderMessage next hookInput =
  case blockImplementationMutation renderMessage hookInput of
    Just reason -> pure (denyResponse reason)
    Nothing -> preToolUseWithGhBlock next hookInput

preToolUseWithGitAuthorAndImplementationBlock ::
  (Text -> Text) ->
  (HookInput -> Eff Effects HookOutput) ->
  HookInput ->
  Eff Effects HookOutput
preToolUseWithGitAuthorAndImplementationBlock renderMessage next hookInput =
  case blockGitAuthorMutation hookInput of
    Just reason -> pure (denyResponse reason)
    Nothing -> preToolUseWithImplementationBlock renderMessage next hookInput

runtimeFromHookInput :: HookInput -> Runtime
runtimeFromHookInput hookInput =
  case hiRuntime hookInput of
    Just runtime -> runtime
    Nothing -> Claude

shellToolsFor :: Runtime -> [Text]
shellToolsFor Claude = []
shellToolsFor Codex = ["shell", "Bash"]
shellToolsFor OpenCode = ["shell", "bash", "run_command"]
shellToolsFor Gemini = []

commandWritesFiles :: Text -> Bool
commandWritesFiles cmd =
  let normalized = T.toCaseFold cmd
      tokens = normalizedShellTokens cmd
   in ">" `T.isInfixOf` normalized
        || "tee" `elem` tokens
        || "write_text" `T.isInfixOf` normalized
        || "write_bytes" `T.isInfixOf` normalized
        || ".write(" `T.isInfixOf` normalized
        || pythonOpenWriteMode normalized

pythonOpenWriteMode :: Text -> Bool
pythonOpenWriteMode normalized =
  "open(" `T.isInfixOf` normalized
    && any
      (`T.isInfixOf` normalized)
      ["'w", "\"w", "'a", "\"a", "'x", "\"x"]

containsGhToken :: Text -> Bool
containsGhToken cmd =
  "gh" `elem` T.words (T.map shellTokenSeparator (T.toCaseFold cmd))

accessesChainlinkSqlite :: Text -> Bool
accessesChainlinkSqlite cmd =
  let normalized = T.toCaseFold cmd
   in ".chainlink/issues.db" `T.isInfixOf` normalized
        || ("sqlite3" `elem` T.words (T.map shellTokenSeparator normalized)
              && ".chainlink" `T.isInfixOf` normalized
              && "issues.db" `T.isInfixOf` normalized)

hasCommitMutatingGitCommand :: Text -> Bool
hasCommitMutatingGitCommand cmd =
  any deniesGitInvocation (gitInvocationTokens cmd)

normalizedShellTokens :: Text -> [Text]
normalizedShellTokens = T.words . T.map shellTokenSeparator . T.toCaseFold

gitInvocationTokens :: Text -> [[Text]]
gitInvocationTokens cmd =
  [suffix | suffix@(token : _) <- tails (normalizedShellTokens cmd), token == "git"]

deniesGitInvocation :: [Text] -> Bool
deniesGitInvocation (_git : args) =
  case gitVerb args of
    Just (verb, verbArgs) -> deniedGitVerb verb verbArgs
    Nothing -> False
deniesGitInvocation _ = False

gitVerb :: [Text] -> Maybe (Text, [Text])
gitVerb [] = Nothing
gitVerb (token : rest)
  | "-" `T.isPrefixOf` token = gitVerb (dropGitGlobalFlagValue token rest)
  | otherwise = Just (token, rest)

dropGitGlobalFlagValue :: Text -> [Text] -> [Text]
dropGitGlobalFlagValue token rest
  | token `elem` ["-c", "--git-dir", "--work-tree", "--namespace"] = drop 1 rest
  | any (`T.isPrefixOf` token) ["--git-dir=", "--work-tree=", "--namespace="] = rest
  | otherwise = rest

deniedGitVerb :: Text -> [Text] -> Bool
deniedGitVerb "commit" _ = True
deniedGitVerb "rebase" _ = True
deniedGitVerb "cherry-pick" _ = True
deniedGitVerb "merge" _ = True
deniedGitVerb "filter-branch" _ = True
deniedGitVerb "replace" _ = True
deniedGitVerb "revert" _ = True
deniedGitVerb "am" _ = True
deniedGitVerb "update-ref" _ = True
deniedGitVerb "reset" args = any (`elem` ["--hard", "--soft"]) args
deniedGitVerb "apply" args = "--index" `elem` args
deniedGitVerb "push" args = any (`elem` ["--force", "--force-with-lease"]) args
deniedGitVerb "notes" args =
  case firstNonFlag args of
    Just action -> action `elem` ["add", "edit", "remove"]
    Nothing -> False
deniedGitVerb _ _ = False

firstNonFlag :: [Text] -> Maybe Text
firstNonFlag [] = Nothing
firstNonFlag (token : rest)
  | "-" `T.isPrefixOf` token = firstNonFlag rest
  | otherwise = Just token

shellTokenSeparator :: Char -> Char
shellTokenSeparator c
  | c `elem` (" \t\n\r;&|(){}[]<>`'\"/" :: String) = ' '
  | otherwise = c
