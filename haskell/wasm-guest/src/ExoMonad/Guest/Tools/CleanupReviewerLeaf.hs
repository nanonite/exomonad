{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.CleanupReviewerLeaf
  ( CleanupReviewerLeaf (..),
    CleanupReviewerLeafArgs (..),
    cleanupReviewerLeafDescription,
    cleanupReviewerLeafSchema,
    cleanupReviewerLeafCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), object, withObject, (.:), (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Agent qualified as PA
import ExoMonad.Effects.Agent qualified as Agent
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Tools.Spawn (spawnErrorMessage)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

data CleanupReviewerLeaf

newtype CleanupReviewerLeafArgs = CleanupReviewerLeafArgs
  { crlPrNumber :: Int
  }
  deriving (Show, Eq, Generic)

instance FromJSON CleanupReviewerLeafArgs where
  parseJSON = withObject "CleanupReviewerLeafArgs" $ \v ->
    CleanupReviewerLeafArgs
      <$> v .: "pr_number"

cleanupReviewerLeafDescription :: Text
cleanupReviewerLeafDescription = "Clean all reviewer resources for an existing PR: kill reviewer windows, remove reviewer worktrees/config, delete legacy review files, and reset watcher state."

cleanupReviewerLeafSchema :: Aeson.Object
cleanupReviewerLeafSchema =
  genericToolSchemaWith @CleanupReviewerLeafArgs
    [("pr_number", "Existing PR number whose reviewer resources should be cleaned")]

cleanupReviewerLeafCore :: CleanupReviewerLeafArgs -> Eff Effects (Either Text Aeson.Value)
cleanupReviewerLeafCore args
  | crlPrNumber args <= 0 = pure $ Left "pr_number must be positive"
  | otherwise = do
      let req =
            PA.CleanupReviewerLeafRequest
              { PA.cleanupReviewerLeafRequestPrNumber = fromIntegral (crlPrNumber args)
              }
      result <- suspendEffect @Agent.AgentCleanupReviewerLeaf req
      pure $ case result of
        Left err -> Left (spawnErrorMessage err)
        Right resp
          | not (PA.cleanupReviewerLeafResponseSuccess resp) ->
              Left (lazyText (PA.cleanupReviewerLeafResponseError resp))
          | otherwise ->
              Right $
                object
                  [ "success" .= True,
                    "pr_number" .= PA.cleanupReviewerLeafResponsePrNumber resp,
                    "cleaned_reviewers" .= map lazyText (V.toList (PA.cleanupReviewerLeafResponseCleanedReviewers resp))
                  ]

instance MCPTool CleanupReviewerLeaf where
  type ToolArgs CleanupReviewerLeaf = CleanupReviewerLeafArgs
  toolName = "cleanup_reviewer_leaf"
  toolDescription = cleanupReviewerLeafDescription
  toolSchema = cleanupReviewerLeafSchema
  toolHandlerEff args = do
    result <- cleanupReviewerLeafCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value

lazyText :: TL.Text -> Text
lazyText = TL.toStrict
