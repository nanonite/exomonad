{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.CleanupLeaf
  ( CleanupLeaf (..),
    CleanupLeafArgs (..),
    cleanupLeafDescription,
    cleanupLeafSchema,
    cleanupLeafCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), ToJSON (..), object, withObject, (.!=), (.:?), (.=))
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

data CleanupLeaf = CleanupLeaf ()

data CleanupLeafArgs = CleanupLeafArgs
  { claName :: Maybe Text,
    claDryRun :: Bool,
    claSweep :: Bool
  }
  deriving (Generic, Show)

instance FromJSON CleanupLeafArgs where
  parseJSON = withObject "CleanupLeafArgs" $ \v ->
    CleanupLeafArgs
      <$> v .:? "name"
      <*> v .:? "dry_run" .!= False
      <*> v .:? "sweep" .!= False

instance ToJSON CleanupLeafArgs where
  toJSON args =
    object
      [ "name" .= claName args,
        "dry_run" .= claDryRun args,
        "sweep" .= claSweep args
      ]

cleanupLeafDescription :: Text
cleanupLeafDescription =
  "Safely dispose an orphan leaf after verifying its tmux window is dead, its worktree is clean, and its PR is merged or closed-unmerged. Use sweep=true for all orphan worktrees or dry_run=true to inspect without disposal."

cleanupLeafSchema :: Aeson.Object
cleanupLeafSchema =
  genericToolSchemaWith @CleanupLeafArgs
    [ ("name", "Optional agent slug to verify and clean; required unless sweep=true."),
      ("dry_run", "Verify and report without disposing resources. Defaults to false."),
      ("sweep", "Verify and clean every orphan worktree. Defaults to false.")
    ]

cleanupLeafCore :: CleanupLeafArgs -> Eff Effects (Either Text Aeson.Value)
cleanupLeafCore args
  | not (claSweep args) && maybe True (T.null . T.strip) (claName args) = pure $ Left "name is required unless sweep=true"
  | otherwise = do
      let req =
            PA.DisposeOrphanRequest
              { PA.disposeOrphanRequestAgentSlug = fromText (maybe "" id (claName args)),
                PA.disposeOrphanRequestVerifyPrState = True,
                PA.disposeOrphanRequestDryRun = claDryRun args,
                PA.disposeOrphanRequestSweep = claSweep args
              }
      result <- suspendEffect @Agent.AgentDisposeOrphan req
      pure $ case result of
        Left err -> Left (spawnErrorMessage err)
        Right resp -> Right (cleanupLeafOutput args resp)

cleanupLeafOutput :: CleanupLeafArgs -> PA.DisposeOrphanResponse -> Aeson.Value
cleanupLeafOutput args resp =
  object
    [ "success" .= True,
      "agent" .= claName args,
      "dry_run" .= claDryRun args,
      "sweep" .= claSweep args,
      "verified" .= PA.disposeOrphanResponseVerified resp,
      "pr_state" .= lazyText (PA.disposeOrphanResponsePrState resp),
      "pr_number" .= PA.disposeOrphanResponsePrNumber resp,
      "removed_worktree" .= PA.disposeOrphanResponseRemovedWorktree resp,
      "removed_agent_dir" .= PA.disposeOrphanResponseRemovedAgentDir resp,
      "message" .= lazyText (PA.disposeOrphanResponseMessage resp),
      "cleaned_agents" .= map lazyText (V.toList (PA.disposeOrphanResponseCleanedAgents resp)),
      "skipped_agents" .= map lazyText (V.toList (PA.disposeOrphanResponseSkippedAgents resp)),
      "errors" .= map lazyText (V.toList (PA.disposeOrphanResponseErrors resp))
    ]

lazyText :: TL.Text -> Text
lazyText = TL.toStrict

instance MCPTool CleanupLeaf where
  type ToolArgs CleanupLeaf = CleanupLeafArgs
  toolName = "cleanup_leaf"
  toolDescription = cleanupLeafDescription
  toolSchema = cleanupLeafSchema
  toolHandlerEff args = do
    result <- cleanupLeafCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value
