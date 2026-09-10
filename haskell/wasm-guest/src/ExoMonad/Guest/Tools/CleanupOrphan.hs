{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.CleanupOrphan
  ( CleanupOrphan (..),
    CleanupOrphanArgs (..),
    cleanupOrphanDescription,
    cleanupOrphanSchema,
    cleanupOrphanCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), ToJSON (..), object, withObject, (.!=), (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Agent qualified as PA
import ExoMonad.Effects.Agent qualified as Agent
import ExoMonad.Guest.Proto (fromText)
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Tools.Spawn (spawnErrorMessage)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

newtype CleanupOrphan = CleanupOrphan ()

data CleanupOrphanArgs = CleanupOrphanArgs
  { coaName :: Text,
    coaReason :: Maybe Text,
    coaDryRun :: Bool,
    coaAllowNoPr :: Bool,
    coaDiscardDirty :: Bool,
    coaPreserveUniqueCommits :: Bool,
    coaDeleteRemoteBranch :: Maybe Bool
  }
  deriving (Generic, Show)

instance FromJSON CleanupOrphanArgs where
  parseJSON = withObject "CleanupOrphanArgs" $ \v ->
    CleanupOrphanArgs
      <$> v .: "name"
      <*> v .:? "reason"
      <*> v .:? "dry_run" .!= False
      <*> v .:? "allow_no_pr" .!= False
      <*> v .:? "discard_dirty" .!= False
      <*> v .:? "preserve_unique_commits" .!= False
      <*> v .:? "delete_remote_branch"

instance ToJSON CleanupOrphanArgs where
  toJSON args =
    object
      [ "name" .= coaName args,
        "reason" .= coaReason args,
        "dry_run" .= coaDryRun args,
        "allow_no_pr" .= coaAllowNoPr args,
        "discard_dirty" .= coaDiscardDirty args,
        "preserve_unique_commits" .= coaPreserveUniqueCommits args,
        "delete_remote_branch" .= maybe False id (coaDeleteRemoteBranch args)
      ]

cleanupOrphanDescription :: Text
cleanupOrphanDescription = "Safely dispose an orphan agent after verifying its tmux window is dead and managed identity is coherent. Use allow_no_pr=true for abandoned work without a PR and discard_dirty=true to explicitly discard a named dirty worktree; these overrides require an exact target, and dirty discard requires apply. Set delete_remote_branch=true separately for irreversible lease-protected remote deletion. Supply reason for auditable operator context."

cleanupOrphanSchema :: Aeson.Object
cleanupOrphanSchema =
  genericToolSchemaWith @CleanupOrphanArgs
    [ ("name", "Agent slug to clean up, as shown by session_status"),
      ("reason", "Optional operator context recorded in the cleanup receipt."),
      ("dry_run", "When true, report what would be removed without removing it. Defaults to false."),
      ("allow_no_pr", "Explicitly authorize cleanup when no pull request owns the managed branch."),
      ("discard_dirty", "Explicitly authorize discarding dirty changes for this named agent; requires apply."),
      ("preserve_unique_commits", "Keep unique abandoned commits reachable by preserving the local branch. Without this option they may later be garbage-collected."),
      ("delete_remote_branch", "Independently authorize irreversible lease-protected deletion of the exact managed remote branch; never implied by other options.")
    ]

cleanupOrphanCore :: CleanupOrphanArgs -> Eff Effects (Either Text Aeson.Value)
cleanupOrphanCore args
  | T.null (T.strip (coaName args)) = pure $ Left "name is required"
  | otherwise = do
      let req =
            PA.DisposeOrphanRequest
              { PA.disposeOrphanRequestAgentSlug = fromText (coaName args),
                PA.disposeOrphanRequestReason = fromText (maybe "" id (coaReason args)),
                PA.disposeOrphanRequestVerifyPrState = True,
                PA.disposeOrphanRequestDryRun = coaDryRun args,
                PA.disposeOrphanRequestSweep = False,
                PA.disposeOrphanRequestAllowNoPr = coaAllowNoPr args,
                PA.disposeOrphanRequestDiscardDirty = coaDiscardDirty args,
                PA.disposeOrphanRequestPreserveUniqueCommits = coaPreserveUniqueCommits args,
                PA.disposeOrphanRequestDeleteRemoteBranch = maybe False id (coaDeleteRemoteBranch args)
              }
      result <- suspendEffect @Agent.AgentDisposeOrphan req
      pure $ case result of
        Left err -> Left (spawnErrorMessage err)
        Right resp -> Right (cleanupOrphanOutput args resp)

cleanupOrphanOutput :: CleanupOrphanArgs -> PA.DisposeOrphanResponse -> Aeson.Value
cleanupOrphanOutput args resp =
  object
    [ "success" .= True,
      "agent" .= coaName args,
      "dry_run" .= coaDryRun args,
      "delete_remote_branch" .= maybe False id (coaDeleteRemoteBranch args),
      "operator_reason" .= lazyText (PA.disposeOrphanResponseOperatorReason resp),
      "preserved_unique_commits" .= PA.disposeOrphanResponsePreservedUniqueCommits resp,
      "verified" .= PA.disposeOrphanResponseVerified resp,
      "pr_state" .= lazyText (PA.disposeOrphanResponsePrState resp),
      "pr_number" .= PA.disposeOrphanResponsePrNumber resp,
      "removed_worktree" .= PA.disposeOrphanResponseRemovedWorktree resp,
      "removed_agent_dir" .= PA.disposeOrphanResponseRemovedAgentDir resp,
      "message" .= lazyText (PA.disposeOrphanResponseMessage resp),
      "cleaned_agents" .= map lazyText (V.toList (PA.disposeOrphanResponseCleanedAgents resp)),
      "skipped_agents" .= map lazyText (V.toList (PA.disposeOrphanResponseSkippedAgents resp)),
      "errors" .= map lazyText (V.toList (PA.disposeOrphanResponseErrors resp)),
      "discarded_porcelain" .= map lazyText (V.toList (PA.disposeOrphanResponseDiscardedPorcelain resp)),
      "discarded_tracked_paths" .= map lazyText (V.toList (PA.disposeOrphanResponseDiscardedTrackedPaths resp)),
      "discarded_untracked_paths" .= map lazyText (V.toList (PA.disposeOrphanResponseDiscardedUntrackedPaths resp)),
      "discarded_changes_truncated" .= PA.disposeOrphanResponseDiscardedChangesTruncated resp,
      "verified_remote_name" .= lazyText (PA.disposeOrphanResponseVerifiedRemoteName resp),
      "verified_remote_ref" .= lazyText (PA.disposeOrphanResponseVerifiedRemoteRef resp),
      "verified_remote_head_sha" .= lazyText (PA.disposeOrphanResponseVerifiedRemoteHeadSha resp),
      "remote_deletion_outcome" .= lazyText (PA.disposeOrphanResponseRemoteDeletionOutcome resp)
    ]

lazyText :: TL.Text -> Text
lazyText = TL.toStrict

instance MCPTool CleanupOrphan where
  type ToolArgs CleanupOrphan = CleanupOrphanArgs
  toolName = "cleanup_orphan"
  toolDescription = cleanupOrphanDescription
  toolSchema = cleanupOrphanSchema
  toolHandlerEff args = do
    result <- cleanupOrphanCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value
