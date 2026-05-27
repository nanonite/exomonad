{-# LANGUAGE OverloadedStrings #-}

module ExoMonad.Guest.Effects.StopHook
  ( checkUncommittedWork,
    checkPRNotFiled,
    getCurrentBranch,
    getAgentId,
  )
where

import Control.Monad.Freer (Eff)
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Effects.FilePr qualified as FPR
import Effects.Git qualified as Git
import ExoMonad.Effects.FilePR (FilePRLocalPrGetForBranch)
import ExoMonad.Effects.Git (GitGetBranch, GitGetStatus, GitHasUnpushedCommits)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Types (Effects)
import System.Environment (lookupEnv)

-- | Check for uncommitted/unpushed work and nudge if found.
checkUncommittedWork :: Text -> Eff Effects (Maybe Text)
checkUncommittedWork branch = do
  statusResult <- suspendEffect @GitGetStatus (Git.GetStatusRequest {Git.getStatusRequestWorkingDir = "."})
  let hasUncommitted = case statusResult of
        Right resp ->
          not (null (Git.getStatusResponseDirtyFiles resp))
            || not (null (Git.getStatusResponseStagedFiles resp))
        _ -> False

  unpushedResult <- suspendEffect @GitHasUnpushedCommits (Git.HasUnpushedCommitsRequest {Git.hasUnpushedCommitsRequestWorkingDir = ".", Git.hasUnpushedCommitsRequestRemote = "origin"})
  let hasUnpushed = case unpushedResult of
        Right resp -> Git.hasUnpushedCommitsResponseHasUnpushed resp
        _ -> False

  if hasUncommitted
    then pure $ Just $ "You have uncommitted changes on " <> branch <> " but no PR filed. Commit and file a PR before stopping."
    else
      if hasUnpushed
        then pure $ Just $ "Commits on " <> branch <> " aren't in a PR yet. File a PR before stopping."
        else pure Nothing

-- | Check Forgejo-backed PR lookup for an already-filed PR.
-- Hosted PR lookup is intentionally not performed here; file_pr owns remote
-- creation and stop hooks must not depend on hosted API credentials.
checkPRNotFiled :: Text -> Eff Effects (Maybe Text)
checkPRNotFiled branch = do
  localPrResult <-
    suspendEffect @FilePRLocalPrGetForBranch
      ( FPR.LocalPrGetForBranchRequest
          { FPR.localPrGetForBranchRequestBranch = TL.fromStrict branch
          }
      )
  case localPrResult of
    Right resp
      | FPR.localPrResponseFound resp ->
          pure Nothing
    _ -> pure Nothing

-- ============================================================================
-- Agent Identity Helpers
-- ============================================================================

-- | Read the agent's identity from EXOMONAD_AGENT_ID env var.
getAgentId :: IO (Maybe Text)
getAgentId = fmap (fmap T.pack) (lookupEnv "EXOMONAD_AGENT_ID")

-- | Get the current branch identity, defaulting to "unknown" on error.
-- Detached reviewer worktrees receive their agent birth branch from the host
-- git effect so verdict provenance does not collapse to "unknown".
getCurrentBranch :: Eff Effects Text
getCurrentBranch = do
  result <- suspendEffect @GitGetBranch (Git.GetBranchRequest {Git.getBranchRequestWorkingDir = "."})
  case result of
    Right resp
      | not (TL.null (Git.getBranchResponseBranch resp)) -> pure $ TL.toStrict (Git.getBranchResponseBranch resp)
      | otherwise -> pure "unknown"
    Left _ -> pure "unknown"
