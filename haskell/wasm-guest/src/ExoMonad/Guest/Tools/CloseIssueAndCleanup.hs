{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.CloseIssueAndCleanup
  ( CloseIssueAndCleanup (..),
    CloseIssueAndCleanupArgs (..),
    closeIssueAndCleanupDescription,
    closeIssueAndCleanupSchema,
    closeIssueAndCleanupCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), ToJSON (..), object, withObject, (.:), (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Agent qualified as Agent
import ExoMonad.Effects.Agent (AgentCloseIssueAndCleanup)
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Tools.Spawn (spawnErrorMessage)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

data CloseIssueAndCleanupArgs = CloseIssueAndCleanupArgs
  { ciacIssueId :: Int,
    ciacLeafName :: Text
  }
  deriving (Generic, Show)

instance FromJSON CloseIssueAndCleanupArgs where
  parseJSON = withObject "CloseIssueAndCleanupArgs" $ \v ->
    CloseIssueAndCleanupArgs
      <$> v .: "issue_id"
      <*> v .: "leaf_name"

instance ToJSON CloseIssueAndCleanupArgs where
  toJSON args =
    object
      [ "issue_id" .= ciacIssueId args,
        "leaf_name" .= ciacLeafName args
      ]

closeIssueAndCleanupDescription :: Text
closeIssueAndCleanupDescription =
  "Close a completed Chainlink issue and clean up the merged leaf. Refuses to run when the leaf has any filed PR that is not merged. Disposes the leaf worktree and reviewer worktrees for merged PRs owned by the leaf."

closeIssueAndCleanupSchema :: Aeson.Object
closeIssueAndCleanupSchema =
  genericToolSchemaWith @CloseIssueAndCleanupArgs
    [ ("issue_id", "The Chainlink issue id to close"),
      ("leaf_name", "The leaf agent name / worktree slug to dispose, e.g. issue-335-close-cleanup-codex")
    ]

closeIssueAndCleanupCore :: CloseIssueAndCleanupArgs -> Eff Effects (Either Text Aeson.Value)
closeIssueAndCleanupCore args = do
  result <-
    suspendEffect @AgentCloseIssueAndCleanup
      Agent.CloseIssueAndCleanupRequest
        { Agent.closeIssueAndCleanupRequestIssueId = fromIntegral (ciacIssueId args),
          Agent.closeIssueAndCleanupRequestLeafName = TL.fromStrict (ciacLeafName args)
        }
  case result of
    Left err -> pure $ Left (spawnErrorMessage err)
    Right response
      | Agent.closeIssueAndCleanupResponseSuccess response ->
          pure $
            Right $
              object
                [ "success" .= True,
                  "leaf_name" .= TL.toStrict (Agent.closeIssueAndCleanupResponseLeafName response),
                  "cleaned_pr_numbers" .= V.toList (Agent.closeIssueAndCleanupResponseCleanedPrNumbers response)
                ]
      | otherwise -> pure $ Left (TL.toStrict (Agent.closeIssueAndCleanupResponseError response))

data CloseIssueAndCleanup

instance MCPTool CloseIssueAndCleanup where
  type ToolArgs CloseIssueAndCleanup = CloseIssueAndCleanupArgs
  toolName = "close_issue_and_cleanup"
  toolDescription = closeIssueAndCleanupDescription
  toolSchema = closeIssueAndCleanupSchema
  toolHandlerEff args = do
    result <- closeIssueAndCleanupCore args
    case result of
      Left err -> pure $ errorResult err
      Right value -> pure $ successResult value
