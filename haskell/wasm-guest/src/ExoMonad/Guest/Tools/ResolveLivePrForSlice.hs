{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.ResolveLivePrForSlice
  ( ResolveLivePrForSlice (..),
    ResolveLivePrForSliceArgs (..),
    resolveLivePrForSliceDescription,
    resolveLivePrForSliceSchema,
    resolveLivePrForSliceResponseValue,
    resolveLivePrForSliceCore,
  )
where

import Control.Monad.Freer (Eff, Member)
import Data.Aeson (FromJSON (..), object, withObject, (.:), (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Effects.Agent qualified as PA
import ExoMonad.Effects.Agent qualified as Agent
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.Suspend.Types (SuspendYield)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Tools.Spawn (spawnErrorMessage)
import GHC.Generics (Generic)
import Proto3.Suite.Types qualified as Protobuf

data ResolveLivePrForSlice

data ResolveLivePrForSliceArgs = ResolveLivePrForSliceArgs
  { rlpfssSliceId :: Text
  }
  deriving (Show, Eq, Generic)

instance FromJSON ResolveLivePrForSliceArgs where
  parseJSON = withObject "ResolveLivePrForSliceArgs" $ \v ->
    ResolveLivePrForSliceArgs <$> v .: "slice_id"

resolveLivePrForSliceDescription :: Text
resolveLivePrForSliceDescription =
  "Resolve the current live Forgejo PR for a TL slice through the publication effect. The typed result distinguishes never_published, all_attempts_abandoned, and live; absence is a successful diagnosis, not an error."

resolveLivePrForSliceSchema :: Aeson.Object
resolveLivePrForSliceSchema =
  genericToolSchemaWith @ResolveLivePrForSliceArgs
    [("slice_id", "Stable TL slice identifier whose live PR should be resolved.")]

resolveLivePrForSliceResponseValue :: PA.ResolveLivePrForSliceResponse -> Aeson.Value
resolveLivePrForSliceResponseValue response =
  object
    [ "success" .= PA.resolveLivePrForSliceResponseSuccess response,
      "slice_id" .= TL.toStrict (PA.resolveLivePrForSliceResponseSliceId response),
      "resolution" .= resolutionText (PA.resolveLivePrForSliceResponseResolution response),
      "pr_number" .= PA.resolveLivePrForSliceResponsePrNumber response,
      "publication" .= PA.resolveLivePrForSliceResponsePublication response
    ]

resolveLivePrForSliceCore :: (Member SuspendYield effs) => ResolveLivePrForSliceArgs -> Eff effs (Either Text Aeson.Value)
resolveLivePrForSliceCore args
  | T.null (T.strip (rlpfssSliceId args)) = pure $ Left "slice_id is required"
  | otherwise = do
      result <-
        suspendEffect @Agent.AgentResolveLivePrForSlice
          PA.ResolveLivePrForSliceRequest
            { PA.resolveLivePrForSliceRequestSliceId = TL.fromStrict (rlpfssSliceId args)
            }
      pure $ case result of
        Left err -> Left (spawnErrorMessage err)
        Right response
          | not (PA.resolveLivePrForSliceResponseSuccess response) ->
              Left (TL.toStrict (PA.resolveLivePrForSliceResponseError response))
          | otherwise -> Right (resolveLivePrForSliceResponseValue response)

resolutionText :: Protobuf.Enumerated PA.LivePrResolutionKind -> Text
resolutionText value =
  case Protobuf.enumerated value of
    Right PA.LivePrResolutionKindLIVE_PR_RESOLUTION_KIND_NEVER_PUBLISHED -> "never_published"
    Right PA.LivePrResolutionKindLIVE_PR_RESOLUTION_KIND_ALL_ATTEMPTS_ABANDONED -> "all_attempts_abandoned"
    Right PA.LivePrResolutionKindLIVE_PR_RESOLUTION_KIND_LIVE -> "live"
    _ -> "unspecified"

instance MCPTool ResolveLivePrForSlice where
  type ToolArgs ResolveLivePrForSlice = ResolveLivePrForSliceArgs
  toolName = "resolve_live_pr_for_slice"
  toolDescription = resolveLivePrForSliceDescription
  toolSchema = resolveLivePrForSliceSchema
  toolHandlerEff args = do
    result <- resolveLivePrForSliceCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value
