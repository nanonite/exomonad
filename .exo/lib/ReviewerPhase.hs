{-# LANGUAGE OverloadedStrings #-}

module ReviewerPhase
  ( ReviewerPhase (..),
    ReviewerEvent (..),
  )
where

import Data.Aeson (FromJSON (..), ToJSON (..), object, withObject, (.:), (.=))
import Data.Text (Text)
import Data.Text qualified as T
import ExoMonad.Guest.StateMachine (StateMachine (..), StopCheckResult (..), TransitionResult (..))

type PRNumber = Int

data ReviewerPhase
  = ReviewerSpawned
  | ReviewerReviewing PRNumber Int
  | ReviewerApprovedAwaitingCI PRNumber
  | ReviewerChangesRequested PRNumber Text
  | ReviewerDone
  | ReviewerFailed Text
  deriving (Show, Eq)

data ReviewerEvent
  = ReviewerApprovedEv PRNumber
  | ReviewerRequestedChangesEv PRNumber Text
  | ReviewerFixesPushedEv PRNumber Text
  | ReviewerCommitsPushedEv PRNumber Text
  | ReviewerMergeReadyEv PRNumber Text Text
  | ReviewerTimedOutEv PRNumber Int
  | ReviewerStuckEv PRNumber Int
  deriving (Show, Eq)

instance StateMachine ReviewerPhase ReviewerEvent where
  machineName = "reviewer"

  transition phase event = case event of
    ReviewerApprovedEv prNum ->
      Transitioned (ReviewerApprovedAwaitingCI prNum)
    ReviewerRequestedChangesEv prNum comments ->
      Transitioned (ReviewerChangesRequested prNum comments)
    ReviewerFixesPushedEv prNum _ci ->
      Transitioned (ReviewerReviewing prNum (nextRound phase))
    ReviewerCommitsPushedEv prNum _ci ->
      Transitioned (ReviewerReviewing prNum (nextRound phase))
    ReviewerMergeReadyEv _prNum _ci _branch ->
      Transitioned ReviewerDone
    ReviewerTimedOutEv _prNum _mins ->
      Transitioned ReviewerDone
    ReviewerStuckEv _prNum _rounds ->
      Transitioned ReviewerDone

  canExit (ReviewerReviewing pr _) =
    MustBlock $ "PR #" <> T.pack (show pr) <> " is under review. Stay alive until merge-ready."
  canExit (ReviewerApprovedAwaitingCI pr) =
    MustBlock $ "PR #" <> T.pack (show pr) <> " approved, waiting for CI merge-ready signal."
  canExit (ReviewerChangesRequested pr _) =
    MustBlock $ "PR #" <> T.pack (show pr) <> " has requested changes. Stay alive for fixes."
  canExit _ = Clean

nextRound :: ReviewerPhase -> Int
nextRound (ReviewerReviewing _ round_) = round_ + 1
nextRound _ = 1

instance ToJSON ReviewerPhase where
  toJSON ReviewerSpawned = object ["phase" .= ("reviewer_spawned" :: Text)]
  toJSON (ReviewerReviewing n r) = object ["phase" .= ("reviewer_reviewing" :: Text), "pr_number" .= n, "review_round" .= r]
  toJSON (ReviewerApprovedAwaitingCI n) = object ["phase" .= ("reviewer_approved_awaiting_ci" :: Text), "pr_number" .= n]
  toJSON (ReviewerChangesRequested n comments) = object ["phase" .= ("reviewer_requested_changes" :: Text), "pr_number" .= n, "comments" .= comments]
  toJSON ReviewerDone = object ["phase" .= ("reviewer_done" :: Text)]
  toJSON (ReviewerFailed msg) = object ["phase" .= ("reviewer_failed" :: Text), "message" .= msg]

instance FromJSON ReviewerPhase where
  parseJSON = withObject "ReviewerPhase" $ \v -> do
    phase <- v .: "phase"
    case (phase :: Text) of
      "reviewer_spawned" -> pure ReviewerSpawned
      "reviewer_reviewing" -> ReviewerReviewing <$> v .: "pr_number" <*> v .: "review_round"
      "reviewer_approved_awaiting_ci" -> ReviewerApprovedAwaitingCI <$> v .: "pr_number"
      "reviewer_requested_changes" -> ReviewerChangesRequested <$> v .: "pr_number" <*> v .: "comments"
      "reviewer_done" -> pure ReviewerDone
      "reviewer_failed" -> ReviewerFailed <$> v .: "message"
      other -> fail $ "Unknown ReviewerPhase: " <> T.unpack other
