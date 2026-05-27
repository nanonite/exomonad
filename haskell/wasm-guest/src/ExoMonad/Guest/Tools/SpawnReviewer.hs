{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.SpawnReviewer
  ( SpawnReviewer (..),
    SpawnReviewerArgs (..),
    spawnReviewerDescription,
    spawnReviewerSchema,
    spawnReviewerCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), object, withObject, (.!=), (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text.Lazy qualified as TL
import Effects.Agent qualified as PA
import ExoMonad.Effects.Agent qualified as Agent
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Tools.Spawn (spawnErrorMessage)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

data SpawnReviewer

data SpawnReviewerArgs = SpawnReviewerArgs
  { srPrNumber :: Int,
    srForce :: Bool
  }
  deriving (Show, Eq, Generic)

instance FromJSON SpawnReviewerArgs where
  parseJSON = withObject "SpawnReviewerArgs" $ \v ->
    SpawnReviewerArgs
      <$> v .: "pr_number"
      <*> v .:? "force" .!= False

spawnReviewerDescription :: Text
spawnReviewerDescription = "Spawn only a reviewer for an existing open Forgejo PR. Returns already_active=true without spawning when a reviewer is still running, unless force=true."

spawnReviewerSchema :: Aeson.Object
spawnReviewerSchema =
  genericToolSchemaWith @SpawnReviewerArgs
    [ ("pr_number", "Existing open PR number to review"),
      ("force", "Spawn a fresh reviewer even if a reviewer for this PR is already active. Defaults to false.")
    ]

spawnReviewerCore :: SpawnReviewerArgs -> Eff Effects (Either Text Aeson.Value)
spawnReviewerCore args
  | srPrNumber args <= 0 = pure $ Left "pr_number must be positive"
  | otherwise = do
      let req =
            PA.SpawnReviewerRequest
              { PA.spawnReviewerRequestPrNumber = fromIntegral (srPrNumber args),
                PA.spawnReviewerRequestForce = srForce args
              }
      result <- suspendEffect @Agent.AgentSpawnReviewer req
      pure $ case result of
        Left err -> Left (spawnErrorMessage err)
        Right resp ->
          Right $
            object
              [ "reviewer_name" .= lazyText (PA.spawnReviewerResponseReviewerName resp),
                "already_active" .= PA.spawnReviewerResponseAlreadyActive resp
              ]

instance MCPTool SpawnReviewer where
  type ToolArgs SpawnReviewer = SpawnReviewerArgs
  toolName = "spawn_reviewer"
  toolDescription = spawnReviewerDescription
  toolSchema = spawnReviewerSchema
  toolHandlerEff args = do
    result <- spawnReviewerCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value

lazyText :: TL.Text -> Text
lazyText = TL.toStrict
