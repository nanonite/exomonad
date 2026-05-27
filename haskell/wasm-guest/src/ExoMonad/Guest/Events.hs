{-# LANGUAGE OverloadedStrings #-}

module ExoMonad.Guest.Events
  ( EventHandlerConfig (..),
    EventAction (..),
    PRReviewEvent (..),
    CIStatusEvent (..),
    TimeoutEvent (..),
    SiblingMergedEvent (..),
    IssueClosedEvent (..),
    EventInput (..),
    defaultEventHandlers,
    dispatchEvent,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON, ToJSON, Value, object, withObject, (.!=), (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

-- | PR review event types
data PRReviewEvent
  = ReviewReceived
      { prNumber :: Int,
        comments :: Text
      }
  | ReviewApproved
      { prNumber :: Int
      }
  | ReviewTimeout
      { prNumber :: Int,
        minutesElapsed :: Int
      }
  | FixesPushed
      { prNumber :: Int,
        fpCiStatus :: Text,
        fpHeadSha :: Text
      }
  | CommitsPushed
      { prNumber :: Int,
        cpCiStatus :: Text
      }
  | ReviewerApproved
      { prNumber :: Int
      }
  | ReviewerRequestedChanges
      { prNumber :: Int,
        rcComments :: Text
      }
  | RateLimited
      { rlRetriesRemaining :: Int,
        rlSecondsUntilReset :: Int
      }
  | Stuck
      { prNumber :: Int,
        stuckRounds :: Int
      }
  | CITriggered
      { prNumber :: Int,
        ctBranch :: Text,
        ctHeadSha :: Text
      }
  | CIBlocked
      { prNumber :: Int,
        cbCiStatus :: Text,
        cbBranch :: Text
      }
  | MergeReady
      { prNumber :: Int,
        mrCiStatus :: Text,
        mrBranch :: Text
      }
  | DevNotPushing
      { prNumber :: Int
      }
  | ReviewerNotResponding
      { prNumber :: Int
      }
  | ReviewerNeverStarted
      { prNumber :: Int
      }
  | ReviewDevFailed
      { prNumber :: Int
      }
  deriving (Show, Generic)

instance FromJSON PRReviewEvent where
  parseJSON = withObject "PRReviewEvent" $ \v -> do
    kind <- v .: "kind"
    case kind of
      "review_received" -> ReviewReceived <$> v .: "pr_number" <*> v .: "comments"
      "approved" -> ReviewApproved <$> v .: "pr_number"
      "timeout" -> ReviewTimeout <$> v .: "pr_number" <*> v .: "minutes_elapsed"
      "fixes_pushed" -> FixesPushed <$> v .: "pr_number" <*> v .: "ci_status" <*> v .: "head_sha"
      "commits_pushed" -> CommitsPushed <$> v .: "pr_number" <*> v .: "ci_status"
      "reviewer_approved" -> ReviewerApproved <$> v .: "pr_number"
      "reviewer_requested_changes" -> ReviewerRequestedChanges <$> v .: "pr_number" <*> v .: "comments"
      "rate_limited" -> RateLimited <$> v .: "retries_remaining" <*> v .: "seconds_until_reset"
      "stuck" -> Stuck <$> v .: "pr_number" <*> v .: "rounds"
      "ci_triggered" -> CITriggered <$> v .: "pr_number" <*> v .: "branch" <*> v .: "head_sha"
      "ci_blocked" -> CIBlocked <$> v .: "pr_number" <*> v .: "ci_status" <*> v .: "branch"
      "merge_ready" -> MergeReady <$> v .: "pr_number" <*> v .: "ci_status" <*> v .: "branch"
      "dev_not_pushing" -> DevNotPushing <$> v .: "pr_number"
      "reviewer_not_responding" -> ReviewerNotResponding <$> v .: "pr_number"
      "reviewer_never_started" -> ReviewerNeverStarted <$> v .: "pr_number"
      "dev_failed" -> ReviewDevFailed <$> v .: "pr_number"
      other -> fail $ "Unknown PRReviewEvent kind: " <> show (other :: Text)

instance ToJSON PRReviewEvent where
  toJSON (ReviewReceived n c) = object ["kind" .= ("review_received" :: Text), "pr_number" .= n, "comments" .= c]
  toJSON (ReviewApproved n) = object ["kind" .= ("approved" :: Text), "pr_number" .= n]
  toJSON (ReviewTimeout n m) = object ["kind" .= ("timeout" :: Text), "pr_number" .= n, "minutes_elapsed" .= m]
  toJSON (FixesPushed n ci sha) = object ["kind" .= ("fixes_pushed" :: Text), "pr_number" .= n, "ci_status" .= ci, "head_sha" .= sha]
  toJSON (CommitsPushed n ci) = object ["kind" .= ("commits_pushed" :: Text), "pr_number" .= n, "ci_status" .= ci]
  toJSON (ReviewerApproved n) = object ["kind" .= ("reviewer_approved" :: Text), "pr_number" .= n]
  toJSON (ReviewerRequestedChanges n c) = object ["kind" .= ("reviewer_requested_changes" :: Text), "pr_number" .= n, "comments" .= c]
  toJSON (RateLimited r s) = object ["kind" .= ("rate_limited" :: Text), "retries_remaining" .= r, "seconds_until_reset" .= s]
  toJSON (Stuck n r) = object ["kind" .= ("stuck" :: Text), "pr_number" .= n, "rounds" .= r]
  toJSON (CITriggered n branch sha) = object ["kind" .= ("ci_triggered" :: Text), "pr_number" .= n, "branch" .= branch, "head_sha" .= sha]
  toJSON (CIBlocked n ci branch) = object ["kind" .= ("ci_blocked" :: Text), "pr_number" .= n, "ci_status" .= ci, "branch" .= branch]
  toJSON (MergeReady n ci branch) = object ["kind" .= ("merge_ready" :: Text), "pr_number" .= n, "ci_status" .= ci, "branch" .= branch]
  toJSON (DevNotPushing n) = object ["kind" .= ("dev_not_pushing" :: Text), "pr_number" .= n]
  toJSON (ReviewerNotResponding n) = object ["kind" .= ("reviewer_not_responding" :: Text), "pr_number" .= n]
  toJSON (ReviewerNeverStarted n) = object ["kind" .= ("reviewer_never_started" :: Text), "pr_number" .= n]
  toJSON (ReviewDevFailed n) = object ["kind" .= ("dev_failed" :: Text), "pr_number" .= n]

-- | CI status event
data CIStatusEvent = CIStatusEvent
  { ciPrNumber :: Int,
    ciStatus :: Text,
    ciBranch :: Text,
    ciMergeBlockedOnCI :: Bool,
    ciReviewerApproved :: Bool,
    ciMergeReady :: Bool
  }
  deriving (Show, Generic)

instance FromJSON CIStatusEvent where
  parseJSON = withObject "CIStatusEvent" $ \v ->
    CIStatusEvent
      <$> v .: "pr_number"
      <*> v .: "status"
      <*> v .: "branch"
      <*> v .:? "merge_blocked_on_ci" .!= False
      <*> v .:? "reviewer_approved" .!= False
      <*> v .:? "merge_ready" .!= False

instance ToJSON CIStatusEvent where
  toJSON (CIStatusEvent n s b blocked approved ready) =
    object ["pr_number" .= n, "status" .= s, "branch" .= b, "merge_blocked_on_ci" .= blocked, "reviewer_approved" .= approved, "merge_ready" .= ready]

-- | Timeout event
data TimeoutEvent = TimeoutEvent
  { tePrNumber :: Int,
    teMinutesElapsed :: Int
  }
  deriving (Show, Generic)

instance FromJSON TimeoutEvent where
  parseJSON = withObject "TimeoutEvent" $ \v ->
    TimeoutEvent <$> v .: "pr_number" <*> v .: "minutes_elapsed"

instance ToJSON TimeoutEvent where
  toJSON (TimeoutEvent n m) = object ["pr_number" .= n, "minutes_elapsed" .= m]

-- | Sibling merged event
data SiblingMergedEvent = SiblingMergedEvent
  { mergedBranch :: Text,
    parentBranch :: Text,
    siblingPRNumber :: Int
  }
  deriving (Show, Generic)

instance FromJSON SiblingMergedEvent where
  parseJSON = withObject "SiblingMergedEvent" $ \v ->
    SiblingMergedEvent <$> v .: "merged_branch" <*> v .: "parent_branch" <*> v .: "sibling_pr_number"

instance ToJSON SiblingMergedEvent where
  toJSON (SiblingMergedEvent mb pb n) = object ["merged_branch" .= mb, "parent_branch" .= pb, "sibling_pr_number" .= n]

-- | Chainlink issue closed event.
data IssueClosedEvent = IssueClosedEvent
  { issueClosedIssueId :: Int,
    issueClosedBy :: Text
  }
  deriving (Show, Generic)

instance FromJSON IssueClosedEvent where
  parseJSON = withObject "IssueClosedEvent" $ \v ->
    IssueClosedEvent <$> v .: "issue_id" <*> v .: "closed_by"

instance ToJSON IssueClosedEvent where
  toJSON (IssueClosedEvent issueId closedBy) = object ["issue_id" .= issueId, "closed_by" .= closedBy]

-- | Event handler return type
data EventAction
  = InjectMessage Text
  | NotifyParentAction {naMessage :: Text, naPrNumber :: Int}
  | NoAction
  deriving (Show, Generic)

instance ToJSON EventAction where
  toJSON (InjectMessage msg) = object ["action" .= ("inject_message" :: Text), "message" .= msg]
  toJSON (NotifyParentAction msg pr) = object ["action" .= ("notify_parent" :: Text), "message" .= msg, "pr_number" .= pr]
  toJSON NoAction = object ["action" .= ("no_action" :: Text)]

instance FromJSON EventAction where
  parseJSON = withObject "EventAction" $ \v -> do
    action <- v .: "action"
    case action of
      "inject_message" -> InjectMessage <$> v .: "message"
      "notify_parent" -> NotifyParentAction <$> v .: "message" <*> v .: "pr_number"
      "no_action" -> pure NoAction
      other -> fail $ "Unknown EventAction: " <> show (other :: Text)

-- | Configuration for event handlers per role.
data EventHandlerConfig = EventHandlerConfig
  { onPRReview :: PRReviewEvent -> Eff Effects EventAction,
    onCIStatus :: CIStatusEvent -> Eff Effects EventAction,
    onTimeout :: TimeoutEvent -> Eff Effects EventAction,
    onSiblingMerged :: SiblingMergedEvent -> Eff Effects EventAction,
    onIssueClosed :: IssueClosedEvent -> Eff Effects EventAction
  }

-- | Default event handlers (all NoAction).
defaultEventHandlers :: EventHandlerConfig
defaultEventHandlers =
  EventHandlerConfig
    { onPRReview = \_ -> pure NoAction,
      onCIStatus = \_ -> pure NoAction,
      onTimeout = \_ -> pure NoAction,
      onSiblingMerged = \_ -> pure NoAction,
      onIssueClosed = \_ -> pure NoAction
    }

-- | Top-level event type wrapper for dispatching.
data EventInput
  = PRReviewInput PRReviewEvent
  | CIStatusInput CIStatusEvent
  | TimeoutInput TimeoutEvent
  | SiblingMergedInput SiblingMergedEvent
  | IssueClosedInput IssueClosedEvent
  deriving (Show, Generic)

instance FromJSON EventInput where
  parseJSON = withObject "EventInput" $ \v -> do
    eventType <- v .: "event_type"
    payload <- v .: "payload"
    case eventType of
      "pr_review" -> PRReviewInput <$> Aeson.parseJSON payload
      "ci_status" -> CIStatusInput <$> Aeson.parseJSON payload
      "timeout" -> TimeoutInput <$> Aeson.parseJSON payload
      "sibling_merged" -> SiblingMergedInput <$> Aeson.parseJSON payload
      "issue_closed" -> IssueClosedInput <$> Aeson.parseJSON payload
      other -> fail $ "Unknown event_type: " <> show (other :: Text)

-- | Dispatch an event to the appropriate handler.
dispatchEvent :: EventHandlerConfig -> EventInput -> Eff Effects EventAction
dispatchEvent cfg (PRReviewInput ev) = onPRReview cfg ev
dispatchEvent cfg (CIStatusInput ev) = onCIStatus cfg ev
dispatchEvent cfg (TimeoutInput ev) = onTimeout cfg ev
dispatchEvent cfg (SiblingMergedInput ev) = onSiblingMerged cfg ev
dispatchEvent cfg (IssueClosedInput ev) = onIssueClosed cfg ev
