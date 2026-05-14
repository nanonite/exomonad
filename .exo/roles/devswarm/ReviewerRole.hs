{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}
{-# LANGUAGE TypeOperators #-}

-- | Reviewer role: diff review only — no spawn, merge, or PR tools.
--   Tool restrictions enforced at the WASM hook layer.
module ReviewerRole (config, Tools) where

import Data.Aeson (object, (.=))
import ExoMonad
import ExoMonad.Guest.Tools.Events
  ( notifyParentCore, notifyParentDescription, notifyParentSchema, NotifyParentArgs
  )
import ExoMonad.Guest.Types (allowResponse, allowStopResponse, BeforeModelOutput (..), AfterModelOutput (..))
import ExoMonad.Types (HookConfig (..), defaultSessionStartHook)
import HookPolicy (preToolUseWithGhBlock)
import ExoMonad.Guest.Events
  ( PRReviewEvent (..), CIStatusEvent (..), SiblingMergedEvent (..),
    EventHandlerConfig (..), EventAction (..), defaultEventHandlers
  )
import Data.Text (Text)
import Data.Text qualified as T
import Control.Monad (void)
import Control.Monad.Freer (Eff)
import ExoMonad.Guest.Types (Effects)
import ExoMonad.Effects.Log qualified as Log
import Data.Text.Lazy qualified as TL
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect_)

-- | Reviewer notify_parent: thin wrapper, no phase transitions.
data ReviewerNotifyParent

instance MCPTool ReviewerNotifyParent where
  type ToolArgs ReviewerNotifyParent = NotifyParentArgs
  toolName = "notify_parent"
  toolDescription = notifyParentDescription
  toolSchema = notifyParentSchema
  toolHandlerEff args = do
    result <- notifyParentCore args
    case result of
      Left err -> pure $ errorResult err
      Right _ -> pure $ successResult $ object ["success" .= True]

data Tools mode = Tools
  { notifyParent :: mode :- ReviewerNotifyParent,
    sendMessage :: mode :- SendMessage
  }
  deriving (Generic)

config :: RoleConfig (Tools AsHandler)
config =
  RoleConfig
    { roleName = "reviewer",
      tools =
        Tools
          { notifyParent = mkHandler @ReviewerNotifyParent,
            sendMessage = mkHandler @SendMessage
          },
      hooks =
        HookConfig
          { preToolUse = preToolUseWithGhBlock (\_ -> pure (allowResponse Nothing)),
            postToolUse = \_ -> pure (allowResponse Nothing),
            onStop = \_ -> pure allowStopResponse,
            onSubagentStop = \_ -> pure allowStopResponse,
            onSessionStart = defaultSessionStartHook,
            beforeModel = \_ -> pure (BeforeModelAllow Nothing),
            afterModel = \_ -> pure (AfterModelAllow Nothing)
          },
      eventHandlers = reviewerEventHandlers
    }

-- | Event handlers for the reviewer role.
--   Handles incoming PR review events so the reviewer agent can respond.
reviewerEventHandlers :: EventHandlerConfig
reviewerEventHandlers =
  defaultEventHandlers
    { onPRReview = reviewerPRReviewHandler,
      onCIStatus = \_ -> pure NoAction,
      onSiblingMerged = reviewerSiblingMergedHandler
    }

reviewerPRReviewHandler :: PRReviewEvent -> Eff Effects EventAction
reviewerPRReviewHandler (ReviewReceived n comments_) = do
  logHandler $ "Review received on PR #" <> T.pack (show n)
  pure (InjectMessage $ "[REVIEW] PR #" <> T.pack (show n) <> " received comments:\n" <> comments_)

reviewerPRReviewHandler (ReviewApproved n) = do
  logHandler $ "PR #" <> T.pack (show n) <> " approved"
  pure (NotifyParentAction ("[REVIEWER APPROVED] PR #" <> T.pack (show n) <> " approved by reviewer") n)

reviewerPRReviewHandler (ReviewTimeout n mins) = do
  logHandler $ "PR #" <> T.pack (show n) <> " timed out after " <> T.pack (show mins) <> " minutes"
  pure NoAction

reviewerPRReviewHandler (FixesPushed n ci) = do
  logHandler $ "Fixes pushed on PR #" <> T.pack (show n) <> ", CI: " <> ci
  pure (InjectMessage $ "[FIXES PUSHED] PR #" <> T.pack (show n) <> " CI: " <> ci)

reviewerPRReviewHandler (CommitsPushed n ci) = do
  logHandler $ "New commits pushed on PR #" <> T.pack (show n) <> ", CI: " <> ci
  pure NoAction

reviewerPRReviewHandler (ReviewerApproved n) = do
  logHandler $ "Reviewer approved PR #" <> T.pack (show n)
  pure (NotifyParentAction ("[REVIEWER APPROVED] PR #" <> T.pack (show n) <> " approved by reviewer agent") n)

reviewerPRReviewHandler (ReviewerRequestedChanges n comments_) = do
  logHandler $ "Reviewer requested changes on PR #" <> T.pack (show n)
  pure (InjectMessage $ "[CHANGES REQUESTED] PR #" <> T.pack (show n) <> ":\n" <> comments_)

reviewerPRReviewHandler (RateLimited remaining secs) = do
  logHandler $ "Rate limited: " <> T.pack (show remaining) <> " retries, " <> T.pack (show secs) <> "s"
  pure NoAction

reviewerPRReviewHandler (Stuck n rounds_) = do
  logHandler $ "PR #" <> T.pack (show n) <> " stuck after " <> T.pack (show rounds_) <> " rounds"
  pure (NotifyParentAction ("[STUCK: " <> T.pack (show n) <> ", rounds=" <> T.pack (show rounds_) <> "] PR requires human intervention") n)

reviewerSiblingMergedHandler :: SiblingMergedEvent -> Eff Effects EventAction
reviewerSiblingMergedHandler ev = do
  logHandler $ "Sibling merged: " <> mergedBranch ev
  pure NoAction

logHandler :: Text -> Eff Effects ()
logHandler msg =
  void $ suspendEffect_ @Log.LogInfo $ Log.InfoRequest
    { Log.infoRequestMessage = TL.fromStrict $ "[ReviewerRole] " <> msg
    , Log.infoRequestFields = ""
    }
