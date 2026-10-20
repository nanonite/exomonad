{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE FlexibleContexts #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

-- | Commit-all preparation for an explicitly identified parent-to-child TL handoff.
module ExoMonad.Guest.Tools.PrepareScopeHandoff
  ( PrepareScopeHandoff,
    PrepareScopeHandoffArgs (..),
    prepareScopeHandoffCore,
    prepareScopeHandoffDescription,
    prepareScopeHandoffSchema,
    prepareScopeHandoffResponseValue,
  )
where

import Control.Monad.Freer (Eff, Member)
import Data.Aeson (FromJSON (..), Value, object, withObject, (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Agent qualified as PA
import ExoMonad.Effects.Agent qualified as Agent
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.Suspend.Types (SuspendYield)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Tools.Spawn (spawnErrorMessage)
import GHC.Generics (Generic)

data PrepareScopeHandoff

data PrepareScopeHandoffArgs = PrepareScopeHandoffArgs
  { pshApply :: Bool,
    pshHandoffId :: Text,
    pshControllerId :: Text,
    pshParentRunId :: Text,
    pshParentScopeId :: Text,
    pshChildScopeId :: Text,
    pshExpectedBranch :: Text,
    pshExpectedHeadSha :: Maybe Text,
    pshExpectedSourceFingerprint :: Maybe Text
  }
  deriving (Show, Eq, Generic)

instance FromJSON PrepareScopeHandoffArgs where
  parseJSON = withObject "PrepareScopeHandoffArgs" $ \value ->
    PrepareScopeHandoffArgs
      <$> value .: "apply"
      <*> value .: "handoff_id"
      <*> value .: "controller_id"
      <*> value .: "parent_run_id"
      <*> value .: "parent_scope_id"
      <*> value .: "child_scope_id"
      <*> value .: "expected_branch"
      <*> value .:? "expected_head_sha"
      <*> value .:? "expected_source_fingerprint"

prepareScopeHandoffDescription :: Text
prepareScopeHandoffDescription =
  "Inspect or commit all normal Git changes in this TL's authenticated source worktree before handing control to one declared child. Standard git add -A ignore behavior, hooks, and configured identity apply. First call with apply=false; pass its branch, HEAD, and source fingerprint unchanged to apply=true. Changed paths are hexadecimal encoded. A false ready field blocks handoff."

prepareScopeHandoffSchema :: Aeson.Object
prepareScopeHandoffSchema =
  genericToolSchemaWith @PrepareScopeHandoffArgs
    [ ("apply", "False inspects; true stages and commits the inspected snapshot."),
      ("handoff_id", "Stable logical identity for this parent-run/scope/child handoff."),
      ("controller_id", "Authenticated TL controller identity."),
      ("parent_run_id", "Accepted parent RunStore identity."),
      ("parent_scope_id", "Accepted parent manifest scope identity."),
      ("child_scope_id", "The intended accepted child declaration."),
      ("expected_branch", "Exact source branch selected for this handoff."),
      ("expected_head_sha", "HEAD returned by the preceding inspection; required for apply."),
      ("expected_source_fingerprint", "Fingerprint returned by the preceding inspection; required for apply.")
    ]

prepareScopeHandoffResponseValue :: PA.PrepareScopeHandoffResponse -> Value
prepareScopeHandoffResponseValue response =
  object
    [ "success" .= PA.prepareScopeHandoffResponseSuccess response,
      "error" .= strictText (PA.prepareScopeHandoffResponseError response),
      "outcome" .= strictText (PA.prepareScopeHandoffResponseOutcome response),
      "handoff_id" .= strictText (PA.prepareScopeHandoffResponseHandoffId response),
      "controller_id" .= strictText (PA.prepareScopeHandoffResponseControllerId response),
      "parent_run_id" .= strictText (PA.prepareScopeHandoffResponseParentRunId response),
      "parent_scope_id" .= strictText (PA.prepareScopeHandoffResponseParentScopeId response),
      "child_scope_id" .= strictText (PA.prepareScopeHandoffResponseChildScopeId response),
      "source_worktree" .= strictText (PA.prepareScopeHandoffResponseSourceWorktree response),
      "branch" .= strictText (PA.prepareScopeHandoffResponseBranch response),
      "head_before" .= strictText (PA.prepareScopeHandoffResponseHeadBefore response),
      "head_after" .= strictText (PA.prepareScopeHandoffResponseHeadAfter response),
      "commit_sha" .= strictText (PA.prepareScopeHandoffResponseCommitSha response),
      "source_fingerprint" .= strictText (PA.prepareScopeHandoffResponseSourceFingerprint response),
      "changed_paths" .= map strictText (V.toList (PA.prepareScopeHandoffResponseChangedPaths response)),
      "ready" .= PA.prepareScopeHandoffResponseReady response
    ]

prepareScopeHandoffCore :: (Member SuspendYield effs) => PrepareScopeHandoffArgs -> Eff effs (Either Text Value)
prepareScopeHandoffCore args = do
  result <-
    suspendEffect @Agent.AgentPrepareScopeHandoff
      PA.PrepareScopeHandoffRequest
        { PA.prepareScopeHandoffRequestApply = pshApply args,
          PA.prepareScopeHandoffRequestHandoffId = lazyText (pshHandoffId args),
          PA.prepareScopeHandoffRequestControllerId = lazyText (pshControllerId args),
          PA.prepareScopeHandoffRequestParentRunId = lazyText (pshParentRunId args),
          PA.prepareScopeHandoffRequestParentScopeId = lazyText (pshParentScopeId args),
          PA.prepareScopeHandoffRequestChildScopeId = lazyText (pshChildScopeId args),
          PA.prepareScopeHandoffRequestExpectedBranch = lazyText (pshExpectedBranch args),
          PA.prepareScopeHandoffRequestExpectedHeadSha = maybe "" lazyText (pshExpectedHeadSha args),
          PA.prepareScopeHandoffRequestExpectedSourceFingerprint = maybe "" lazyText (pshExpectedSourceFingerprint args)
        }
  pure $ case result of
    Left err -> Left (spawnErrorMessage err)
    Right response -> Right (prepareScopeHandoffResponseValue response)

instance MCPTool PrepareScopeHandoff where
  type ToolArgs PrepareScopeHandoff = PrepareScopeHandoffArgs
  toolName = "prepare_scope_handoff"
  toolDescription = prepareScopeHandoffDescription
  toolSchema = prepareScopeHandoffSchema
  toolHandlerEff args = do
    result <- prepareScopeHandoffCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value

lazyText :: Text -> TL.Text
lazyText = TL.fromStrict

strictText :: TL.Text -> Text
strictText = TL.toStrict
