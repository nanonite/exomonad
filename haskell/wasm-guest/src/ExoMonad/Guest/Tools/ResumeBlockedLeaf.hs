{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

-- | Resume a parked, externally blocked leaf without creating a new owner.
module ExoMonad.Guest.Tools.ResumeBlockedLeaf
  ( ResumeBlockedLeaf (..),
    ResumeBlockedLeafArgs (..),
    resumeBlockedLeafDescription,
    resumeBlockedLeafSchema,
    resumeBlockedLeafCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), object, withObject, (.:), (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text qualified as T
import ExoMonad.Guest.Effects.AgentControl qualified as AC
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tools.Spawn (spawnErrorMessage)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

data ResumeBlockedLeaf

data ResumeBlockedLeafArgs = ResumeBlockedLeafArgs
  { rblChainlinkIssueId :: Int,
    rblExpectedInvocationId :: Text,
    rblExpectedBranch :: Text,
    rblExpectedWorktreeFingerprint :: Text,
    rblTask :: Text,
    rblHumanApproved :: Bool
  }
  deriving (Show, Eq, Generic)

instance FromJSON ResumeBlockedLeafArgs where
  parseJSON = withObject "ResumeBlockedLeafArgs" $ \value ->
    ResumeBlockedLeafArgs
      <$> value .: "chainlink_issue_id"
      <*> value .: "expected_invocation_id"
      <*> value .: "expected_branch"
      <*> value .: "expected_worktree_fingerprint"
      <*> value .: "task"
      <*> value .: "human_approved"

resumeBlockedLeafDescription :: Text
resumeBlockedLeafDescription =
  "Resume one parked externally blocked leaf in its existing owner worktree and branch. The host resolves the exact dormant owner, verifies the authoritative parked event, dead tmux target, invocation, branch, and dirty-worktree fingerprint, then starts a fresh same-harness invocation. Human approval is mandatory; this never creates a sibling owner, new worktree, or PR."

resumeBlockedLeafSchema :: Aeson.Object
resumeBlockedLeafSchema =
  genericToolSchemaWith @ResumeBlockedLeafArgs
    [ ("chainlink_issue_id", "Open Chainlink issue parked for the blocked leaf"),
      ("expected_invocation_id", "Exact dormant invocation identity from the blocked handoff"),
      ("expected_branch", "Exact owner branch identity from the blocked handoff"),
      ("expected_worktree_fingerprint", "SHA-256 fingerprint of the preserved worktree status"),
      ("task", "Complete continuation task and human guidance"),
      ("human_approved", "Explicit operator approval for this same-owner resume")
    ]

resumeBlockedLeafCore :: ResumeBlockedLeafArgs -> Eff Effects (Either Text Aeson.Value)
resumeBlockedLeafCore args
  | rblChainlinkIssueId args <= 0 = pure $ Left "chainlink_issue_id must be positive"
  | T.null (T.strip (rblExpectedInvocationId args)) = pure $ Left "expected_invocation_id is required"
  | T.null (T.strip (rblExpectedBranch args)) = pure $ Left "expected_branch is required"
  | T.null (T.strip (rblExpectedWorktreeFingerprint args)) = pure $ Left "expected_worktree_fingerprint is required"
  | T.null (T.strip (rblTask args)) = pure $ Left "task is required"
  | not (rblHumanApproved args) = pure $ Left "human_approved must be true"
  | otherwise = do
      result <-
        AC.spawnLeafSubtree
          AC.SpawnLeafSubtreeConfig
            { AC.slcTask = T.strip (rblTask args),
              AC.slcBranchName = "",
              AC.slcIntentId = Nothing,
              AC.slcRole = Just "dev",
              AC.slcAgentType = Nothing,
              AC.slcModel = Nothing,
              AC.slcPerms = AC.defaultPermFlags,
              AC.slcStandaloneRepo = False,
              AC.slcAllowedDirs = [],
              AC.slcBlockedIssueId = Just (fromIntegral (rblChainlinkIssueId args)),
              AC.slcExpectedInvocationId = Just (T.strip (rblExpectedInvocationId args)),
              AC.slcExpectedBranch = Just (T.strip (rblExpectedBranch args)),
              AC.slcExpectedWorktreeFingerprint = Just (T.strip (rblExpectedWorktreeFingerprint args)),
              AC.slcHumanApproved = True
            }
      pure $ case result of
        Left err -> Left (spawnErrorMessage err)
        Right agent ->
          Right $
            object
              [ "success" .= True,
                "chainlink_issue_id" .= rblChainlinkIssueId args,
                "agent" .= agent,
                "invocation" .= object
                  [ "invocation_id" .= AC.invocationId agent,
                    "trigger" .= AC.invocationTrigger agent,
                    "runtime" .= AC.invocationRuntime agent,
                    "target_type" .= AC.routingTargetType agent,
                    "target_id" .= AC.routingTargetId agent,
                    "fresh" .= AC.invocationFresh agent,
                    "ready" .= AC.invocationReady agent,
                    "outcome" .= AC.invocationOutcome agent
                  ]
              ]

instance MCPTool ResumeBlockedLeaf where
  type ToolArgs ResumeBlockedLeaf = ResumeBlockedLeafArgs
  toolName = "resume_blocked_leaf"
  toolDescription = resumeBlockedLeafDescription
  toolSchema = resumeBlockedLeafSchema
  toolHandlerEff args = do
    result <- resumeBlockedLeafCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value
