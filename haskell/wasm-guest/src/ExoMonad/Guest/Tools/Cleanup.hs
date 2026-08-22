{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.Cleanup
  ( Cleanup (..),
    CleanupArgs (..),
    cleanupDescription,
    cleanupSchema,
    cleanupCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), ToJSON (..), object, withObject, (.!=), (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text qualified as T
import Effects.Agent qualified as PA
import ExoMonad.Effects.Agent qualified as Agent
import ExoMonad.Guest.Proto (fromText, toText)
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Tools.Spawn (spawnErrorMessage)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

data Cleanup = Cleanup ()

data CleanupArgs = CleanupArgs
  { caIssue :: Text,
    caForce :: Bool,
    caSubrepo :: Text
  }
  deriving (Generic, Show)

instance FromJSON CleanupArgs where
  parseJSON = withObject "CleanupArgs" $ \v ->
    CleanupArgs
      <$> v .: "issue"
      <*> v .:? "force" .!= False
      <*> v .:? "subrepo" .!= ""

instance ToJSON CleanupArgs where
  toJSON args =
    object
      [ "issue" .= caIssue args,
        "force" .= caForce args,
        "subrepo" .= caSubrepo args
      ]

cleanupDescription :: Text
cleanupDescription =
  "Dispose one explicitly identified agent's tmux target, configuration, and worktree through the host cleanup path."

cleanupSchema :: Aeson.Object
cleanupSchema =
  genericToolSchemaWith @CleanupArgs
    [ ("issue", "Agent runtime identity to clean up."),
      ("force", "Force cleanup of uncommitted resources. Defaults to false."),
      ("subrepo", "Optional sub-repository path.")
    ]

cleanupCore :: CleanupArgs -> Eff Effects (Either Text Aeson.Value)
cleanupCore args
  | T.null (T.strip (caIssue args)) = pure $ Left "issue is required"
  | otherwise = do
      result <-
        suspendEffect @Agent.AgentCleanup
          ( Agent.CleanupRequest
              { Agent.cleanupRequestIssue = fromText (caIssue args),
                Agent.cleanupRequestForce = caForce args,
                Agent.cleanupRequestSubrepo = fromText (caSubrepo args)
              }
          )
      pure $ case result of
        Left err -> Left (spawnErrorMessage err)
        Right response ->
          if PA.cleanupResponseSuccess response
            then Right (object ["success" .= True, "issue" .= caIssue args])
            else Left (toText (PA.cleanupResponseError response))

instance MCPTool Cleanup where
  type ToolArgs Cleanup = CleanupArgs
  toolName = "cleanup"
  toolDescription = cleanupDescription
  toolSchema = cleanupSchema
  toolHandlerEff args = do
    result <- cleanupCore args
    pure $ either errorResult successResult result
