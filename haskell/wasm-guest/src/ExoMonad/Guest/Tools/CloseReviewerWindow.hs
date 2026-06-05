{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.CloseReviewerWindow
  ( CloseReviewerWindow (..),
    CloseReviewerWindowArgs (..),
    closeReviewerWindowDescription,
    closeReviewerWindowSchema,
    closeReviewerWindowCore,
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

data CloseReviewerWindow

newtype CloseReviewerWindowArgs = CloseReviewerWindowArgs
  { crwPrNumber :: Int
  }
  deriving (Show, Eq, Generic)

instance FromJSON CloseReviewerWindowArgs where
  parseJSON = withObject "CloseReviewerWindowArgs" $ \v ->
    CloseReviewerWindowArgs
      <$> v .: "pr_number"

closeReviewerWindowDescription :: Text
closeReviewerWindowDescription = "Close reviewer tmux windows for a PR by matching window names containing review-pr-{pr_number}-. Use this as a manual fallback when reviewer cleanup leaves a stale reviewer window."

closeReviewerWindowSchema :: Aeson.Object
closeReviewerWindowSchema =
  genericToolSchemaWith @CloseReviewerWindowArgs
    [("pr_number", "Existing PR number whose reviewer tmux window(s) should be closed")]

closeReviewerWindowCore :: CloseReviewerWindowArgs -> Eff Effects (Either Text Aeson.Value)
closeReviewerWindowCore args
  | crwPrNumber args <= 0 = pure $ Left "pr_number must be positive"
  | otherwise = do
      let req =
            PA.CloseReviewerWindowRequest
              { PA.closeReviewerWindowRequestPrNumber = fromIntegral (crwPrNumber args)
              }
      result <- suspendEffect @Agent.AgentCloseReviewerWindow req
      pure $ case result of
        Left err -> Left (spawnErrorMessage err)
        Right resp
          | not (PA.closeReviewerWindowResponseSuccess resp) ->
              Left (lazyText (PA.closeReviewerWindowResponseError resp))
          | otherwise ->
              Right $
                object
                  [ "success" .= True,
                    "pr_number" .= PA.closeReviewerWindowResponsePrNumber resp,
                    "closed_windows" .= map lazyText (V.toList (PA.closeReviewerWindowResponseClosedWindows resp))
                  ]

instance MCPTool CloseReviewerWindow where
  type ToolArgs CloseReviewerWindow = CloseReviewerWindowArgs
  toolName = "close_reviewer_window"
  toolDescription = closeReviewerWindowDescription
  toolSchema = closeReviewerWindowSchema
  toolHandlerEff args = do
    result <- closeReviewerWindowCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value

lazyText :: TL.Text -> Text
lazyText = TL.toStrict
