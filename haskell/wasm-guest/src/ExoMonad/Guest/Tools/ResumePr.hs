{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

-- | Resume an existing PR through the host-resolved owner identity.
module ExoMonad.Guest.Tools.ResumePr
  ( ResumePr (..),
    ResumePrArgs (..),
    renderResumePrTask,
    resumePrDescription,
    resumePrSchema,
    resumePrCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), object, withObject, (.:), (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Effects.Agent qualified as PA
import ExoMonad.Effects.Agent qualified as Agent
import ExoMonad.Guest.Effects.AgentControl qualified as AC
import ExoMonad.Guest.Tool.Class (MCPTool (..), successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Tools.Spawn
  ( SpawnToolFailure,
    spawnErrorOutput,
    spawnFailureFromEffectError,
    spawnFailureFromText,
  )
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

data ResumePr

data ResumePrArgs = ResumePrArgs
  { rpaPrNumber :: Int,
    rpaExpectedHeadSha :: Text,
    rpaTask :: Text
  }
  deriving (Show, Eq, Generic)

instance FromJSON ResumePrArgs where
  parseJSON = withObject "ResumePrArgs" $ \v ->
    ResumePrArgs
      <$> v .: "pr_number"
      <*> v .: "expected_head_sha"
      <*> v .: "task"

resumePrDescription :: Text
resumePrDescription =
  "Resume an existing open, unmerged PR using its number, the exact expected head SHA, and a complete task handoff. The host re-fetches the PR, verifies that head SHA, resolves the exact owning agent and branch, starts a fresh invocation, and verifies its tmux target is ready before reporting success. An already-running owner is an explicit failure, not a fresh resume. Put all repair instructions, read-first paths, steps, verification commands, boundaries, and done criteria in task. On the resumed owner's next `file_pr` call, preserve or update the PR body with the literal heading `## Acceptance Criteria` and copy every issue Definition-of-Done bullet verbatim beneath it. Never provide a leaf name, agent type, or model. For closed or unrecoverable PRs, use the human-approved replace_close_pr workflow."

resumePrSchema :: Aeson.Object
resumePrSchema =
  genericToolSchemaWith @ResumePrArgs
    [ ("pr_number", "Existing open, unmerged PR number to resume"),
      ("expected_head_sha", "Exact PR head SHA observed by the caller; the host rechecks it before resuming"),
      ("task", "Complete repair handoff for the owning agent on the existing PR")
    ]

resumePrCore :: ResumePrArgs -> Eff Effects (Either SpawnToolFailure Aeson.Value)
resumePrCore args
  | rpaPrNumber args <= 0 = pure $ Left (spawnFailureFromText "pr_number must be positive")
  | T.null (T.strip (rpaExpectedHeadSha args)) = pure $ Left (spawnFailureFromText "expected_head_sha is required")
  | T.null (T.strip (rpaTask args)) = pure $ Left (spawnFailureFromText "task must be complete and non-empty")
  | otherwise = do
      let renderedTask = renderResumePrTask args
      stateResult <-
        suspendEffect @Agent.AgentWatcherPrState
          PA.WatcherPrStateRequest
            { PA.watcherPrStateRequestPrNumber = fromIntegral (rpaPrNumber args)
            }
      case stateResult of
        Left err -> pure $ Left (spawnFailureFromEffectError err)
        Right state
          | not (PA.watcherPrStateResponseSuccess state) ->
              pure $ Left (spawnFailureFromText (lazyText (PA.watcherPrStateResponseError state)))
          | not (PA.watcherPrStateResponseFound state) ->
              pure $ Left (spawnFailureFromText "PR was not found; refusing to create a replacement branch")
          | PA.watcherPrStateResponseMerged state ->
              pure $ Left (spawnFailureFromText "PR is merged; use a separate follow-up task")
          | T.toLower (lazyText (PA.watcherPrStateResponsePrState state)) /= "open" ->
              pure $ Left (spawnFailureFromText "PR is not open and unmerged; use replace_close_pr only with human approval")
          | T.null (T.strip (lazyText (PA.watcherPrStateResponseHeadBranch state))) ->
              pure $ Left (spawnFailureFromText "PR has no head branch; refusing to resume it")
          | T.null (T.strip (lazyText (PA.watcherPrStateResponseHeadSha state))) ->
              pure $ Left (spawnFailureFromText "PR has no head SHA; refusing to resume it")
          | T.strip (lazyText (PA.watcherPrStateResponseHeadSha state)) /= T.strip (rpaExpectedHeadSha args) ->
              pure $ Left (spawnFailureFromText "PR head changed since the caller's observation; refusing to resume stale work")
          | otherwise -> do
              spawnResult <-
                AC.resumePr
                  AC.ResumePrConfig
                    { AC.rpcTask = renderedTask,
                      AC.rpcPrNumber = fromIntegral (rpaPrNumber args),
                      AC.rpcExpectedHeadSha = T.strip (rpaExpectedHeadSha args),
                      AC.rpcModel = Nothing
                    }
              pure $ case spawnResult of
                Left err -> Left (spawnFailureFromEffectError err)
                Right agent ->
                  Right $
                    object
                      [ "success" .= True,
                        "pr_number" .= rpaPrNumber args,
                        "head_branch" .= lazyText (PA.watcherPrStateResponseHeadBranch state),
                        "head_sha" .= lazyText (PA.watcherPrStateResponseHeadSha state),
                        "agent" .= agent,
                        "invocation" .= resumeInvocationValue agent
                      ]

instance MCPTool ResumePr where
  type ToolArgs ResumePr = ResumePrArgs
  toolName = "resume_pr"
  toolDescription = resumePrDescription
  toolSchema = resumePrSchema
  toolHandlerEff args = do
    result <- resumePrCore args
    pure $ case result of
      Left err -> spawnErrorOutput err
      Right value -> successResult value

renderResumePrTask :: ResumePrArgs -> Text
renderResumePrTask = rpaTask

lazyText :: TL.Text -> Text
lazyText = TL.toStrict

resumeInvocationValue :: AC.SpawnResult -> Aeson.Value
resumeInvocationValue agent =
  object
    [ "invocation_id" .= AC.invocationId agent,
      "trigger" .= AC.invocationTrigger agent,
      "runtime" .= AC.invocationRuntime agent,
      "target_type" .= AC.routingTargetType agent,
      "target_id" .= AC.routingTargetId agent,
      "fresh" .= AC.invocationFresh agent,
      "ready" .= AC.invocationReady agent,
      "outcome" .= AC.invocationOutcome agent
    ]
