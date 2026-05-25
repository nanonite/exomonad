{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.WatcherPrState
  ( WatcherPrState (..),
    WatcherPrStateArgs (..),
    watcherPrStateDescription,
    watcherPrStateSchema,
    watcherPrStateCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), object, withObject, (.:), (.=))
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

data WatcherPrState

newtype WatcherPrStateArgs = WatcherPrStateArgs
  { wpsPrNumber :: Int
  }
  deriving (Show, Eq, Generic)

instance FromJSON WatcherPrStateArgs where
  parseJSON = withObject "WatcherPrStateArgs" $ \v ->
    WatcherPrStateArgs
      <$> v .: "pr_number"

watcherPrStateDescription :: Text
watcherPrStateDescription = "Query live Forgejo PR review and CI state. Returns merge_ready and a blocker string so TL/root agents can diagnose stuck watcher notification paths before deciding whether to call merge_pr."

watcherPrStateSchema :: Aeson.Object
watcherPrStateSchema =
  genericToolSchemaWith @WatcherPrStateArgs
    [("pr_number", "Existing PR number whose live Forgejo review and CI state should be inspected")]

watcherPrStateCore :: WatcherPrStateArgs -> Eff Effects (Either Text Aeson.Value)
watcherPrStateCore args
  | wpsPrNumber args <= 0 = pure $ Left "pr_number must be positive"
  | otherwise = do
      let req =
            PA.WatcherPrStateRequest
              { PA.watcherPrStateRequestPrNumber = fromIntegral (wpsPrNumber args)
              }
      result <- suspendEffect @Agent.AgentWatcherPrState req
      pure $ case result of
        Left err -> Left (spawnErrorMessage err)
        Right resp
          | not (PA.watcherPrStateResponseSuccess resp) ->
              Left (lazyText (PA.watcherPrStateResponseError resp))
          | otherwise ->
              Right $
                object
                  [ "success" .= True,
                    "pr_number" .= PA.watcherPrStateResponsePrNumber resp,
                    "found" .= PA.watcherPrStateResponseFound resp,
                    "merge_ready" .= PA.watcherPrStateResponseMergeReady resp,
                    "blocker" .= lazyText (PA.watcherPrStateResponseBlocker resp),
                    "review_state" .= lazyText (PA.watcherPrStateResponseReviewState resp),
                    "ci_status" .= lazyText (PA.watcherPrStateResponseCiStatus resp),
                    "head_sha" .= lazyText (PA.watcherPrStateResponseHeadSha resp),
                    "head_branch" .= lazyText (PA.watcherPrStateResponseHeadBranch resp),
                    "base_branch" .= lazyText (PA.watcherPrStateResponseBaseBranch resp),
                    "pr_state" .= lazyText (PA.watcherPrStateResponsePrState resp),
                    "merged" .= PA.watcherPrStateResponseMerged resp,
                    "review_count" .= PA.watcherPrStateResponseReviewCount resp
                  ]

instance MCPTool WatcherPrState where
  type ToolArgs WatcherPrState = WatcherPrStateArgs
  toolName = "watcher_pr_state"
  toolDescription = watcherPrStateDescription
  toolSchema = watcherPrStateSchema
  toolHandlerEff args = do
    result <- watcherPrStateCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value

lazyText :: TL.Text -> Text
lazyText = TL.toStrict
