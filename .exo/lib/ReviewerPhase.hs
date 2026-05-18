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
  | ReviewerReviewing PRNumber
  | ReviewerPosted PRNumber
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

  transition _phase event = case event of
    ReviewerApprovedEv prNum ->
      Transitioned (ReviewerPosted prNum)
    ReviewerRequestedChangesEv prNum _comments ->
      Transitioned (ReviewerPosted prNum)
    ReviewerFixesPushedEv prNum _ci ->
      Transitioned (ReviewerReviewing prNum)
    ReviewerCommitsPushedEv prNum _ci ->
      Transitioned (ReviewerReviewing prNum)
    ReviewerMergeReadyEv _prNum _ci _branch ->
      Transitioned ReviewerDone
    ReviewerTimedOutEv _prNum _mins ->
      Transitioned ReviewerDone
    ReviewerStuckEv _prNum _rounds ->
      Transitioned ReviewerDone

  canExit (ReviewerReviewing pr) =
    MustBlock $ "PR #" <> T.pack (show pr) <> " is under review. Post an approval or requested-changes verdict before exiting."
  canExit _ = Clean

instance ToJSON ReviewerPhase where
  toJSON ReviewerSpawned = object ["phase" .= ("reviewer_spawned" :: Text)]
  toJSON (ReviewerReviewing n) = object ["phase" .= ("reviewer_reviewing" :: Text), "pr_number" .= n]
  toJSON (ReviewerPosted n) = object ["phase" .= ("reviewer_posted" :: Text), "pr_number" .= n]
  toJSON ReviewerDone = object ["phase" .= ("reviewer_done" :: Text)]
  toJSON (ReviewerFailed msg) = object ["phase" .= ("reviewer_failed" :: Text), "message" .= msg]

instance FromJSON ReviewerPhase where
  parseJSON = withObject "ReviewerPhase" $ \v -> do
    phase <- v .: "phase"
    case (phase :: Text) of
      "reviewer_spawned" -> pure ReviewerSpawned
      "reviewer_reviewing" -> ReviewerReviewing <$> v .: "pr_number"
      "reviewer_posted" -> ReviewerPosted <$> v .: "pr_number"
      "reviewer_done" -> pure ReviewerDone
      "reviewer_failed" -> ReviewerFailed <$> v .: "message"
      other -> fail $ "Unknown ReviewerPhase: " <> T.unpack other
