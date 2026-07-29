{-# LANGUAGE DeriveGeneric #-}

-- | Shared contract for turning reviewer feedback into an actionable leaf task.
module ExoMonad.Guest.ReviewHandoff
  ( ReviewFixTask (..),
    renderReviewFixTask,
    reviewHandoffInstructions,
  )
where

import Data.Text (Text)
import Data.Text qualified as T
import ExoMonad.Guest.Prompt qualified as Prompt
import GHC.Generics (Generic)

-- | Structured task fields used when resuming an existing PR owner.
data ReviewFixTask = ReviewFixTask
  { reviewFixTask :: Text,
    reviewFixBoundary :: Maybe [Text],
    reviewFixReadFirst :: Maybe [Text],
    reviewFixSteps :: Maybe [Text],
    reviewFixContext :: Maybe Text,
    reviewFixVerify :: Maybe [Text],
    reviewFixDoneCriteria :: Maybe [Text]
  }
  deriving (Eq, Generic, Show)

-- | Render a review-fix task using the same sections as a new leaf scaffold.
renderReviewFixTask :: ReviewFixTask -> Text
renderReviewFixTask spec =
  T.intercalate "\n\n" $
    filter
      (not . T.null)
      [ Prompt.render (Prompt.task (reviewFixTask spec)),
        maybe "" (Prompt.render . Prompt.boundary) (reviewFixBoundary spec),
        maybe "" (Prompt.render . Prompt.readFirst) (reviewFixReadFirst spec),
        maybe "" (Prompt.render . Prompt.steps) (reviewFixSteps spec),
        maybe "" (Prompt.render . Prompt.context) (reviewFixContext spec),
        maybe "" (Prompt.render . Prompt.verify) (reviewFixVerify spec),
        maybe "" (Prompt.render . Prompt.doneCriteria) (reviewFixDoneCriteria spec)
      ]

-- | Instructions injected into the TL's review notification before it composes
-- and sends the repair task to the existing PR owner.
reviewHandoffInstructions :: Text
reviewHandoffInstructions =
  "TL review-fix handoff (required):\n"
    <> "1. Read the PR diff, reviewer comments, and affected source/tests before deciding.\n"
    <> "2. State the root cause of each requested change.\n"
    <> "3. Propose the concrete solution, naming exact files/lines and expected behavior.\n"
    <> "4. Build a complete repair task with ROOT CAUSE, PROPOSED SOLUTION, READ FIRST, STEPS, VERIFY, BOUNDARY, and DONE CRITERIA sections.\n"
    <> "5. For this existing open PR, call `resume_pr` with the PR number and complete task. Do not call `spawn_leaf`, create a sibling branch, create a new Chainlink issue, or close the owning issue.\n"
    <> "The resumed leaf must commit/push the fix, end its Chainlink session, and report the verification results to its parent."
