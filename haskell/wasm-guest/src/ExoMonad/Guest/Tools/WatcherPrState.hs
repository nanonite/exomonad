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
import Data.Aeson (FromJSON (..), object, withObject, (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Maybe (fromMaybe, isNothing)
import Data.Text (Text)
import Data.Text qualified as T
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

data WatcherPrStateArgs = WatcherPrStateArgs
  { wpsPrNumber :: Maybe Int,
    wpsSliceId :: Maybe Text
  }
  deriving (Show, Eq, Generic)

instance FromJSON WatcherPrStateArgs where
  parseJSON = withObject "WatcherPrStateArgs" $ \v ->
    WatcherPrStateArgs
      <$> v .:? "pr_number"
      <*> v .:? "slice_id"

watcherPrStateDescription :: Text
watcherPrStateDescription = "Query live Forgejo PR review and CI observations for an existing PR, including its current head SHA, review state, and CI status. Pass pr_number when known. When pr_number is not yet persisted (e.g. after a crash between pr.filed and identity association), pass slice_id instead to recover it from the durable published-heads registry. Use the returned evidence for diagnostics; this tool does not authorize merging."

watcherPrStateSchema :: Aeson.Object
watcherPrStateSchema =
  genericToolSchemaWith @WatcherPrStateArgs
    [ ("pr_number", "Existing PR number whose live Forgejo review and CI state should be inspected. Omit when recovering identity via slice_id."),
      ("slice_id", "TL slice identifier used to recover a PR number that was never persisted onto checkpoint state. Ignored when pr_number is supplied.")
    ]

watcherPrStateCore :: WatcherPrStateArgs -> Eff Effects (Either Text Aeson.Value)
watcherPrStateCore args
  | maybe False (<= 0) (wpsPrNumber args) = pure $ Left "pr_number must be positive"
  | isNothing (wpsPrNumber args) && maybe True T.null (wpsSliceId args) =
      pure $ Left "either pr_number or slice_id is required"
  | otherwise = do
      let req =
            PA.WatcherPrStateRequest
              { PA.watcherPrStateRequestPrNumber = maybe 0 fromIntegral (wpsPrNumber args),
                PA.watcherPrStateRequestSliceId = TL.fromStrict (fromMaybe "" (wpsSliceId args))
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
                    "review_state" .= lazyText (PA.watcherPrStateResponseReviewState resp),
                    "ci_status" .= lazyText (PA.watcherPrStateResponseCiStatus resp),
                    "head_sha" .= lazyText (PA.watcherPrStateResponseHeadSha resp),
                    "head_branch" .= lazyText (PA.watcherPrStateResponseHeadBranch resp),
                    "base_branch" .= lazyText (PA.watcherPrStateResponseBaseBranch resp),
                    "base_sha" .= lazyText (PA.watcherPrStateResponseBaseSha resp),
                    "patch_digest" .= lazyText (PA.watcherPrStateResponsePatchDigest resp),
                    "merge_tree_sha" .= lazyText (PA.watcherPrStateResponseMergeTreeSha resp),
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
