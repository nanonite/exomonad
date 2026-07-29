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
import Data.Aeson (FromJSON (..), object, withObject, (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Effects.Agent qualified as PA
import ExoMonad.Effects.Agent qualified as Agent
import ExoMonad.Guest.Effects.AgentControl qualified as AC
import ExoMonad.Guest.ReviewHandoff (ReviewFixTask (..), renderReviewFixTask)
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Tools.Spawn (spawnErrorMessage)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

data ResumePr

data ResumePrArgs = ResumePrArgs
  { rpaPrNumber :: Int,
    rpaTask :: Text,
    rpaReadFirst :: Maybe [Text],
    rpaSteps :: Maybe [Text],
    rpaVerify :: Maybe [Text],
    rpaBoundary :: Maybe [Text],
    rpaContext :: Maybe Text,
    rpaDoneCriteria :: Maybe [Text]
  }
  deriving (Show, Eq, Generic)

instance FromJSON ResumePrArgs where
  parseJSON = withObject "ResumePrArgs" $ \v ->
    ResumePrArgs
      <$> v .: "pr_number"
      <*> v .: "task"
      <*> v .:? "read_first"
      <*> v .:? "steps"
      <*> v .:? "verify"
      <*> v .:? "boundary"
      <*> v .:? "context"
      <*> v .:? "done_criteria"

resumePrDescription :: Text
resumePrDescription =
  "Resume an existing open, unmerged PR by number. The host re-fetches its head SHA and resolves the exact owning agent, branch, and runtime. Provide the task summary plus optional read_first, steps, verify, boundary, context, and done_criteria fields for a complete review-fix handoff. Never provide a leaf name or agent type. For closed or unrecoverable PRs, use the human-approved replace_close_pr workflow."

resumePrSchema :: Aeson.Object
resumePrSchema =
  genericToolSchemaWith @ResumePrArgs
    [ ("pr_number", "Existing open, unmerged PR number to resume"),
      ("task", "Task summary for the owning agent to continue on the existing PR"),
      ("read_first", "Exact files or documents the leaf must read before editing"),
      ("steps", "Concrete implementation steps for the repair"),
      ("verify", "Exact commands the leaf must run before reporting completion"),
      ("boundary", "Constraints and anti-patterns for the repair"),
      ("context", "Reviewer analysis, root cause, proposed solution, and relevant snippets"),
      ("done_criteria", "Acceptance criteria for the repair")
    ]

resumePrCore :: ResumePrArgs -> Eff Effects (Either Text Aeson.Value)
resumePrCore args
  | rpaPrNumber args <= 0 = pure $ Left "pr_number must be positive"
  | T.null (T.strip (rpaTask args)) = pure $ Left "task must be complete and non-empty"
  | otherwise = do
      let renderedTask = renderResumePrTask args
      stateResult <-
        suspendEffect @Agent.AgentWatcherPrState
          PA.WatcherPrStateRequest
            { PA.watcherPrStateRequestPrNumber = fromIntegral (rpaPrNumber args)
            }
      case stateResult of
        Left err -> pure $ Left (spawnErrorMessage err)
        Right state
          | not (PA.watcherPrStateResponseSuccess state) ->
              pure $ Left (lazyText (PA.watcherPrStateResponseError state))
          | not (PA.watcherPrStateResponseFound state) ->
              pure $ Left "PR was not found; refusing to create a replacement branch"
          | PA.watcherPrStateResponseMerged state ->
              pure $ Left "PR is merged; use a separate follow-up task"
          | T.toLower (lazyText (PA.watcherPrStateResponsePrState state)) /= "open" ->
              pure $ Left "PR is not open and unmerged; use replace_close_pr only with human approval"
          | T.null (T.strip (lazyText (PA.watcherPrStateResponseHeadBranch state))) ->
              pure $ Left "PR has no head branch; refusing to resume it"
          | T.null (T.strip (lazyText (PA.watcherPrStateResponseHeadSha state))) ->
              pure $ Left "PR has no head SHA; refusing to resume it"
          | otherwise -> do
              spawnResult <-
                AC.resumePr
                  AC.ResumePrConfig
                    { AC.rpcTask = renderedTask,
                      AC.rpcPrNumber = fromIntegral (rpaPrNumber args),
                      AC.rpcExpectedHeadSha = lazyText (PA.watcherPrStateResponseHeadSha state)
                    }
              pure $ case spawnResult of
                Left err -> Left (spawnErrorMessage err)
                Right agent ->
                  Right $
                    object
                      [ "success" .= True,
                        "pr_number" .= rpaPrNumber args,
                        "head_branch" .= lazyText (PA.watcherPrStateResponseHeadBranch state),
                        "head_sha" .= lazyText (PA.watcherPrStateResponseHeadSha state),
                        "agent" .= agent
                      ]

instance MCPTool ResumePr where
  type ToolArgs ResumePr = ResumePrArgs
  toolName = "resume_pr"
  toolDescription = resumePrDescription
  toolSchema = resumePrSchema
  toolHandlerEff args = do
    result <- resumePrCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value

renderResumePrTask :: ResumePrArgs -> Text
renderResumePrTask args =
  renderReviewFixTask
    ReviewFixTask
      { reviewFixTask = rpaTask args,
        reviewFixBoundary = rpaBoundary args,
        reviewFixReadFirst = rpaReadFirst args,
        reviewFixSteps = rpaSteps args,
        reviewFixContext = rpaContext args,
        reviewFixVerify = rpaVerify args,
        reviewFixDoneCriteria = rpaDoneCriteria args
      }

lazyText :: TL.Text -> Text
lazyText = TL.toStrict
