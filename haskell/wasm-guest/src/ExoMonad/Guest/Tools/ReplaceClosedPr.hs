{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.ReplaceClosedPr
  ( ReplaceClosedPr (..),
    ReplaceClosedPrArgs (..),
    replaceClosedPrDescription,
    replaceClosedPrSchema,
    replaceClosedPrCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), object, withObject, (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Agent qualified as PA
import ExoMonad.Effects.Agent (AgentReplaceClosedPr)
import ExoMonad.Guest.Effects.AgentControl qualified as AC
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Tools.Spawn (spawnErrorMessage)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)
import Proto3.Suite.Types (Enumerated (..))

data ReplaceClosedPr

data ReplaceClosedPrArgs = ReplaceClosedPrArgs
  { rcpChainlinkIssueId :: Int,
    rcpClosedPrNumber :: Int,
    rcpOldLeafName :: Text,
    rcpNewLeafName :: Text,
    rcpReplacementTask :: Text,
    rcpAgentType :: Maybe AC.AgentType,
    rcpOperatorContext :: Maybe Text,
    rcpHumanApproved :: Bool
  }
  deriving (Generic, Show)

instance FromJSON ReplaceClosedPrArgs where
  parseJSON = withObject "ReplaceClosedPrArgs" $ \v ->
    ReplaceClosedPrArgs
      <$> v .: "chainlink_issue_id"
      <*> v .: "closed_pr_number"
      <*> v .: "old_leaf_name"
      <*> v .: "new_leaf_name"
      <*> v .: "replacement_task"
      <*> v .:? "agent_type"
      <*> v .:? "operator_context"
      <*> v .: "human_approved"

replaceClosedPrDescription :: Text
replaceClosedPrDescription =
  "With human approval, replace an open or closed and unmerged Forgejo PR while continuing the existing open Chainlink issue. Retires the old reviewer and author resources, preserves the old PR head SHA, and starts a fresh leaf and branch targeting the old PR base. This tool does not close the old PR; after verifying the replacement, explicitly reconcile or close it. Never use this for a merged PR."

replaceClosedPrSchema :: Aeson.Object
replaceClosedPrSchema =
  genericToolSchemaWith @ReplaceClosedPrArgs
    [ ("chainlink_issue_id", "Positive open Chainlink issue id to continue"),
      ("closed_pr_number", "Target open or closed, unmerged Forgejo PR number to replace"),
      ("old_leaf_name", "Explicit old author leaf identity to retire"),
      ("new_leaf_name", "Required fresh bare leaf slug; do not reuse the old slug"),
      ("replacement_task", "Complete task for the fresh leaf, including acceptance criteria"),
      ("agent_type", "Optional fresh leaf runtime: claude, retired, shoal, opencode, or codex"),
      ("operator_context", "Optional human-approved context for the replacement"),
      ("human_approved", "Required true value confirming the human approved this replacement")
    ]

replaceClosedPrCore :: ReplaceClosedPrArgs -> Eff Effects (Either Text Aeson.Value)
replaceClosedPrCore args
  | rcpChainlinkIssueId args <= 0 = pure $ Left "chainlink_issue_id must be positive"
  | rcpClosedPrNumber args <= 0 = pure $ Left "closed_pr_number must be positive"
  | otherwise = do
      result <-
        suspendEffect @AgentReplaceClosedPr
          PA.ReplaceClosedPrRequest
            { PA.replaceClosedPrRequestChainlinkIssueId = fromIntegral (rcpChainlinkIssueId args),
              PA.replaceClosedPrRequestClosedPrNumber = fromIntegral (rcpClosedPrNumber args),
              PA.replaceClosedPrRequestOldLeafName = TL.fromStrict (rcpOldLeafName args),
              PA.replaceClosedPrRequestNewLeafName = TL.fromStrict (rcpNewLeafName args),
              PA.replaceClosedPrRequestReplacementTask = TL.fromStrict (rcpReplacementTask args),
              PA.replaceClosedPrRequestAgentType = Enumerated (Right (maybe PA.AgentTypeAGENT_TYPE_UNSPECIFIED toProtoAgentType (rcpAgentType args))),
              PA.replaceClosedPrRequestOperatorContext = TL.fromStrict (maybe "" id (rcpOperatorContext args)),
              PA.replaceClosedPrRequestHumanApproved = rcpHumanApproved args
            }
      pure $ case result of
        Left err -> Left (spawnErrorMessage err)
        Right response ->
          Right $
            object
              [ "success" .= PA.replaceClosedPrResponseSuccess response,
                "error" .= lazyText (PA.replaceClosedPrResponseError response),
                "chainlink_issue_id" .= PA.replaceClosedPrResponseChainlinkIssueId response,
                "old_pr_number" .= PA.replaceClosedPrResponseOldPrNumber response,
                "old_pr_state" .= lazyText (PA.replaceClosedPrResponseOldPrState response),
                "old_pr_merged" .= PA.replaceClosedPrResponseOldPrMerged response,
                "old_head_branch" .= lazyText (PA.replaceClosedPrResponseOldHeadBranch response),
                "source_head_sha" .= lazyText (PA.replaceClosedPrResponseSourceHeadSha response),
                "original_base_branch" .= lazyText (PA.replaceClosedPrResponseOriginalBaseBranch response),
                "old_leaf_name" .= lazyText (PA.replaceClosedPrResponseOldLeafName response),
                "retired_resources" .= map lazyText (V.toList (PA.replaceClosedPrResponseRetiredResources response)),
                "new_leaf_name" .= lazyText (PA.replaceClosedPrResponseNewLeafName response),
                "new_branch" .= lazyText (PA.replaceClosedPrResponseNewBranch response),
                "worktree_path" .= lazyText (PA.replaceClosedPrResponseWorktreePath response),
                "spawn_status" .= lazyText (PA.replaceClosedPrResponseSpawnStatus response),
                "next_action" .= lazyText (PA.replaceClosedPrResponseNextAction response),
                "replacement_already_exists" .= PA.replaceClosedPrResponseReplacementAlreadyExists response
              ]

toProtoAgentType :: AC.AgentType -> PA.AgentType
toProtoAgentType AC.Claude = PA.AgentTypeAGENT_TYPE_CLAUDE
toProtoAgentType AC.Retired = PA.AgentTypeAGENT_TYPE_RETIRED
toProtoAgentType AC.Shoal = PA.AgentTypeAGENT_TYPE_SHOAL
toProtoAgentType AC.OpenCode = PA.AgentTypeAGENT_TYPE_OPENCODE
toProtoAgentType AC.Codex = PA.AgentTypeAGENT_TYPE_CODEX

instance MCPTool ReplaceClosedPr where
  type ToolArgs ReplaceClosedPr = ReplaceClosedPrArgs
  toolName = "replace_close_pr"
  toolDescription = replaceClosedPrDescription
  toolSchema = replaceClosedPrSchema
  toolHandlerEff args = do
    result <- replaceClosedPrCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value

lazyText :: TL.Text -> Text
lazyText = TL.toStrict
