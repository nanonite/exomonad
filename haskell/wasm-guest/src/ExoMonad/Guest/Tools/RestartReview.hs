{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.RestartReview
  ( RestartReview (..),
    RestartReviewArgs (..),
    restartReviewDescription,
    restartReviewSchema,
    restartReviewCore,
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

data RestartReview

newtype RestartReviewArgs = RestartReviewArgs
  { rrPrNumber :: Int
  }
  deriving (Show, Eq, Generic)

instance FromJSON RestartReviewArgs where
  parseJSON = withObject "RestartReviewArgs" $ \v ->
    RestartReviewArgs
      <$> v .: "pr_number"

restartReviewDescription :: Text
restartReviewDescription = "Reset a stuck PR review cycle: clear watcher flags, dispose reviewer resources, and let the next watcher poll spawn a fresh reviewer."

restartReviewSchema :: Aeson.Object
restartReviewSchema =
  genericToolSchemaWith @RestartReviewArgs
    [("pr_number", "Existing PR number whose review cycle should be restarted")]

restartReviewCore :: RestartReviewArgs -> Eff Effects (Either Text Aeson.Value)
restartReviewCore args
  | rrPrNumber args <= 0 = pure $ Left "pr_number must be positive"
  | otherwise = do
      let req =
            PA.RestartReviewRequest
              { PA.restartReviewRequestPrNumber = fromIntegral (rrPrNumber args)
              }
      result <- suspendEffect @Agent.AgentRestartReview req
      pure $ case result of
        Left err -> Left (spawnErrorMessage err)
        Right resp
          | not (PA.restartReviewResponseSuccess resp) ->
              Left (lazyText (PA.restartReviewResponseError resp))
          | otherwise ->
              Right $
                object
                  [ "success" .= True,
                    "pr_number" .= PA.restartReviewResponsePrNumber resp,
                    "cleaned_reviewers" .= map lazyText (V.toList (PA.restartReviewResponseCleanedReviewers resp)),
                    "runtime_state_found" .= PA.restartReviewResponseRuntimeStateFound resp,
                    "watcher_state_found" .= PA.restartReviewResponseWatcherStateFound resp,
                    "legacy_review_file_removed" .= PA.restartReviewResponseLegacyReviewFileRemoved resp
                  ]

instance MCPTool RestartReview where
  type ToolArgs RestartReview = RestartReviewArgs
  toolName = "restart_review"
  toolDescription = restartReviewDescription
  toolSchema = restartReviewSchema
  toolHandlerEff args = do
    result <- restartReviewCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value

lazyText :: TL.Text -> Text
lazyText = TL.toStrict
