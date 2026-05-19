{-# LANGUAGE OverloadedStrings #-}

module PRReviewHandler
  ( prReviewEventHandlers,
    siblingMergedHandler,
  )
where

import Control.Monad (void)
import Control.Monad.Freer (Eff)
import Data.Text (Text)
import Data.Text qualified as T
import Data.Aeson qualified as Aeson
import Data.Aeson.KeyMap qualified as KM
import Data.Text.Lazy qualified as TL
import DevPhase (DevEvent (..), DevPhase (..))
import ExoMonad.Effects.Log qualified as Log
import ExoMonad.Guest.Effects.StopHook (getCurrentBranch)
import ExoMonad.Guest.Events (CIStatusEvent (..), EventAction (..), EventHandlerConfig (..), IssueClosedEvent (..), PRReviewEvent (..), SiblingMergedEvent (..), defaultEventHandlers)
import ExoMonad.Guest.Events.Templates qualified as Tpl
import ExoMonad.Guest.StateMachine (applyEvent)
import ExoMonad.Guest.Tools.Chainlink (chainlinkSessionStatusCore)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect_)
import ExoMonad.Guest.Types (Effects)

-- | Event handler config with PR review handling.
prReviewEventHandlers :: EventHandlerConfig
prReviewEventHandlers =
  defaultEventHandlers
    { onPRReview = prReviewHandler,
      onCIStatus = ciStatusHandler,
      onSiblingMerged = siblingMergedHandler,
      onIssueClosed = issueClosedHandler
    }

-- | Handle PR review events for dev/tl roles.
prReviewHandler :: PRReviewEvent -> Eff Effects EventAction
prReviewHandler (ReviewReceived n comments_) = do
  logHandler $ "Review received on PR #" <> T.pack (show n)
  branch <- getCurrentBranch
  phase <- applyEvent @DevPhase @DevEvent branch DevSpawned (ReviewReceivedEv n comments_)
  pure $ reviewRequestAction n comments_ phase
prReviewHandler (ReviewApproved n) = do
  logHandler $ "PR #" <> T.pack (show n) <> " approved (reviewer agent)"
  branch <- getCurrentBranch
  void $ applyEvent @DevPhase @DevEvent branch DevSpawned (ReviewApprovedEv n)
  pure NoAction
prReviewHandler (ReviewerApproved n) = do
  logHandler $ "PR #" <> T.pack (show n) <> " approved by reviewer agent"
  branch <- getCurrentBranch
  void $ applyEvent @DevPhase @DevEvent branch DevSpawned (ReviewApprovedEv n)
  pure NoAction
prReviewHandler (ReviewTimeout n mins) = do
  logHandler $ "PR #" <> T.pack (show n) <> " timed out after " <> T.pack (show mins) <> " minutes"
  pure NoAction
prReviewHandler (FixesPushed n ci _headSha) = do
  logHandler $ "Fixes pushed on PR #" <> T.pack (show n) <> ", CI: " <> ci
  branch <- getCurrentBranch
  void $ applyEvent @DevPhase @DevEvent branch DevSpawned (FixesPushedEv n ci)
  pure NoAction
prReviewHandler (CommitsPushed n ci) = do
  logHandler $ "New commits pushed on PR #" <> T.pack (show n) <> ", CI: " <> ci
  branch <- getCurrentBranch
  void $ applyEvent @DevPhase @DevEvent branch DevSpawned (CommitsPushedEv n ci)
  pure NoAction
prReviewHandler (ReviewerRequestedChanges n comments_) = do
  logHandler $ "Reviewer requested changes on PR #" <> T.pack (show n)
  branch <- getCurrentBranch
  phase <- applyEvent @DevPhase @DevEvent branch DevSpawned (ReviewReceivedEv n comments_)
  pure $ reviewRequestAction n comments_ phase
prReviewHandler (RateLimited remaining secs) = do
  logHandler $ "Rate limited: " <> T.pack (show remaining) <> " retries, " <> T.pack (show secs) <> "s until reset"
  pure NoAction
prReviewHandler (Stuck n rounds_) = do
  logHandler $ "PR #" <> T.pack (show n) <> " stuck after " <> T.pack (show rounds_) <> " rounds"
  branch <- getCurrentBranch
  void $
    applyEvent @DevPhase @DevEvent branch DevSpawned $
      ReviewReceivedEv n ("Review loop exceeded " <> T.pack (show rounds_) <> " rounds. Stay alive and wait for TL clarification.")
  pure $
    InjectMessage $
      "Review loop stopped for PR #"
        <> T.pack (show n)
        <> " after "
        <> T.pack (show rounds_)
        <> " rounds. Stay alive and wait for TL clarification."
prReviewHandler (MergeReady n ci branch_) = do
  logHandler $ "PR #" <> T.pack (show n) <> " merge ready, CI: " <> ci
  branch <- getCurrentBranch
  void $ applyEvent @DevPhase @DevEvent branch DevSpawned (MergeReadyEv n ci branch_)
  pure (NotifyParentAction (Tpl.mergeReady n ci branch_) n)
prReviewHandler (DevNotPushing n) = do
  logHandler $ "PR #" <> T.pack (show n) <> " dev leaf stopped pushing fixes"
  pure NoAction
prReviewHandler (ReviewerNotResponding n) = do
  logHandler $ "PR #" <> T.pack (show n) <> " reviewer stopped responding"
  pure NoAction
prReviewHandler (ReviewerNeverStarted n) = do
  logHandler $ "PR #" <> T.pack (show n) <> " reviewer never started"
  pure NoAction
prReviewHandler (ReviewDevFailed n) = do
  logHandler $ "PR #" <> T.pack (show n) <> " dev leaf reported failure"
  pure NoAction


-- | Handle Chainlink issue closure events for dev leaves.
issueClosedHandler :: IssueClosedEvent -> Eff Effects EventAction
issueClosedHandler (IssueClosedEvent issueId closedBy) = do
  activeIssue <- currentChainlinkIssueId
  if issueId == 0 || activeIssue == Just issueId
    then do
      branch <- getCurrentBranch
      void $ applyEvent @DevPhase @DevEvent branch DevSpawned (IssueClosedEv issueId closedBy)
      pure $
        InjectMessage $
          "[ISSUE CLOSED: #"
            <> T.pack (show issueId)
            <> " closed by "
            <> closedBy
            <> ". Exiting; your worktree will be cleaned up.]"
    else pure NoAction

currentChainlinkIssueId :: Eff Effects (Maybe Int)
currentChainlinkIssueId = do
  result <- chainlinkSessionStatusCore
  pure $ case result of
    Right (Aeson.Object obj) -> do
      Aeson.Object activeIssue <- KM.lookup "active_issue" obj
      value <- KM.lookup "id" activeIssue
      case Aeson.fromJSON value of
        Aeson.Success issueId -> Just issueId
        Aeson.Error _ -> Nothing
    _ -> Nothing

-- | Handle sibling merged events.
siblingMergedHandler :: SiblingMergedEvent -> Eff Effects EventAction
siblingMergedHandler (SiblingMergedEvent merged parent _prNum) = do
  logHandler $ "Sibling branch merged: " <> merged
  pure (InjectMessage (Tpl.siblingMerged merged parent))

-- | Handle CI status events.
ciStatusHandler :: CIStatusEvent -> Eff Effects EventAction
ciStatusHandler (CIStatusEvent n status_ branch_ mergeBlockedOnCI _reviewerApproved mergeReady_) = do
  logHandler $ "CI status changed on PR #" <> T.pack (show n) <> ": " <> status_
  if (mergeBlockedOnCI || mergeReady_) && status_ `elem` ["success", "neutral"]
    then do
      branch <- getCurrentBranch
      void $ applyEvent @DevPhase @DevEvent branch DevSpawned (MergeReadyEv n status_ branch_)
      pure (NotifyParentAction (Tpl.mergeReady n status_ branch_) n)
    else pure (InjectMessage (Tpl.ciStatus n status_ branch_))

reviewRequestAction :: Int -> Text -> Maybe DevPhase -> EventAction
reviewRequestAction n _comments (Just (DevNeedsHumanDirection _ reason)) =
  InjectMessage
    ( "Review loop needs human direction for PR #"
        <> T.pack (show n)
        <> ": "
        <> reason
        <> ". Stay alive and wait for TL clarification."
    )
reviewRequestAction n comments_ _ =
  InjectMessage (Tpl.reviewReceived n comments_)

-- | Helper to log handler entry.
logHandler :: Text -> Eff Effects ()
logHandler msg =
  void $
    suspendEffect_ @Log.LogInfo $
      Log.InfoRequest
        { Log.infoRequestMessage = TL.fromStrict $ "[PRReviewHandler] " <> msg,
          Log.infoRequestFields = ""
        }
