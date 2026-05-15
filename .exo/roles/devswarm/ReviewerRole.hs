{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}
{-# LANGUAGE TypeOperators #-}

-- | Reviewer role: diff review only — no spawn, merge, or PR tools.
--   Tool restrictions enforced at the WASM hook layer.
module ReviewerRole (config, Tools) where

import Data.Aeson (object, (.=))
import Data.Aeson qualified as Aeson
import Data.Text.Encoding qualified as TE
import Data.Text.Lazy.Encoding qualified as TLE
import ExoMonad
import ExoMonad.Guest.Effects.FileSystem qualified as FS
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
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
import Data.ByteString.Lazy qualified as BSL
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

data ApprovePRArgs = ApprovePRArgs
  { apPrNumber :: Int,
    apBody :: Text
  }
  deriving (Show, Eq, Generic)

instance Aeson.FromJSON ApprovePRArgs where
  parseJSON = Aeson.withObject "ApprovePRArgs" $ \v ->
    ApprovePRArgs
      <$> v Aeson..: "pr_number"
      <*> v Aeson..: "body"

data RequestChangesArgs = RequestChangesArgs
  { rcPrNumber :: Int,
    rcBody :: Text,
    rcPath :: Maybe Text,
    rcDiffHunk :: Maybe Text
  }
  deriving (Show, Eq, Generic)

instance Aeson.FromJSON RequestChangesArgs where
  parseJSON = Aeson.withObject "RequestChangesArgs" $ \v ->
    RequestChangesArgs
      <$> v Aeson..: "pr_number"
      <*> v Aeson..: "body"
      <*> v Aeson..:? "path"
      <*> v Aeson..:? "diff_hunk"

data PostReviewCommentArgs = PostReviewCommentArgs
  { pcPrNumber :: Int,
    pcBody :: Text,
    pcPath :: Maybe Text,
    pcDiffHunk :: Maybe Text,
    pcThreadId :: Maybe Text
  }
  deriving (Show, Eq, Generic)

instance Aeson.FromJSON PostReviewCommentArgs where
  parseJSON = Aeson.withObject "PostReviewCommentArgs" $ \v ->
    PostReviewCommentArgs
      <$> v Aeson..: "pr_number"
      <*> v Aeson..: "body"
      <*> v Aeson..:? "path"
      <*> v Aeson..:? "diff_hunk"
      <*> v Aeson..:? "thread_id"

data ReviewComment = ReviewComment
  { commentBody :: Text,
    commentPath :: Maybe Text,
    commentDiffHunk :: Maybe Text,
    commentThreadId :: Maybe Text,
    commentResolved :: Bool
  }
  deriving (Show, Eq, Generic)

instance Aeson.ToJSON ReviewComment where
  toJSON c =
    object
      [ "body" .= commentBody c,
        "path" .= commentPath c,
        "diff_hunk" .= commentDiffHunk c,
        "thread_id" .= commentThreadId c,
        "resolved" .= commentResolved c
      ]

instance Aeson.FromJSON ReviewComment where
  parseJSON = Aeson.withObject "ReviewComment" $ \v ->
    ReviewComment
      <$> v Aeson..:? "body" Aeson..!= ""
      <*> v Aeson..:? "path"
      <*> v Aeson..:? "diff_hunk"
      <*> v Aeson..:? "thread_id"
      <*> v Aeson..:? "resolved" Aeson..!= False

data ReviewFile = ReviewFile
  { reviewState :: Text,
    reviewComments :: [ReviewComment]
  }
  deriving (Show, Eq, Generic)

instance Aeson.ToJSON ReviewFile where
  toJSON r =
    object
      [ "state" .= reviewState r,
        "comments" .= reviewComments r
      ]

instance Aeson.FromJSON ReviewFile where
  parseJSON = Aeson.withObject "ReviewFile" $ \v ->
    ReviewFile
      <$> v Aeson..:? "state" Aeson..!= "none"
      <*> v Aeson..:? "comments" Aeson..!= []

reviewFilePath :: Int -> Text
reviewFilePath prNumber = ".exo/reviews/pr_" <> T.pack (show prNumber) <> ".json"

reviewFileToText :: ReviewFile -> Text
reviewFileToText =
  TL.toStrict . TLE.decodeUtf8 . Aeson.encode

decodeReviewFile :: Text -> Maybe ReviewFile
decodeReviewFile =
  Aeson.decode . BSL.fromStrict . TE.encodeUtf8

readExistingReviewFile :: Int -> Eff Effects ReviewFile
readExistingReviewFile prNumber = do
  result <- FS.readFile (reviewFilePath prNumber) 0
  pure $ case result of
    Right output ->
      maybe (ReviewFile "none" []) id (decodeReviewFile (FS.rfoContent output))
    Left _ -> ReviewFile "none" []

writeReviewFile :: Int -> ReviewFile -> Eff Effects (Either Text Text)
writeReviewFile prNumber reviewFile = do
  result <- FS.writeFile (reviewFilePath prNumber) (reviewFileToText reviewFile) True
  pure $ case result of
    Left err -> Left err
    Right output -> Right (FS.wfoPath output)

data ReviewerApprovePR

instance MCPTool ReviewerApprovePR where
  type ToolArgs ReviewerApprovePR = ApprovePRArgs
  toolName = "approve_pr"
  toolDescription = "Approve a local PR by writing `.exo/reviews/pr_{N}.json` for the ExoMonad review watcher."
  toolSchema =
    genericToolSchemaWith @ApprovePRArgs
      [ ("pr_number", "Local PR number to approve"),
        ("body", "Concise approval summary")
      ]
  toolHandlerEff args = do
    result <- writeReviewFile (apPrNumber args) (ReviewFile "approved" [])
    case result of
      Left err -> pure $ errorResult err
      Right path -> pure $ successResult $ object ["success" .= True, "path" .= path]

data ReviewerRequestChanges

instance MCPTool ReviewerRequestChanges where
  type ToolArgs ReviewerRequestChanges = RequestChangesArgs
  toolName = "request_changes"
  toolDescription = "Request changes on a local PR by writing `.exo/reviews/pr_{N}.json` with review comments for the ExoMonad review watcher."
  toolSchema =
    genericToolSchemaWith @RequestChangesArgs
      [ ("pr_number", "Local PR number to review"),
        ("body", "Specific requested change"),
        ("path", "Optional file path for the comment"),
        ("diff_hunk", "Optional diff hunk for the comment")
      ]
  toolHandlerEff args = do
    let comment =
          ReviewComment
            { commentBody = rcBody args,
              commentPath = rcPath args,
              commentDiffHunk = rcDiffHunk args,
              commentThreadId = Nothing,
              commentResolved = False
            }
    result <- writeReviewFile (rcPrNumber args) (ReviewFile "changes_requested" [comment])
    case result of
      Left err -> pure $ errorResult err
      Right path -> pure $ successResult $ object ["success" .= True, "path" .= path]

data ReviewerPostReviewComment

instance MCPTool ReviewerPostReviewComment where
  type ToolArgs ReviewerPostReviewComment = PostReviewCommentArgs
  toolName = "post_review_comment"
  toolDescription = "Append a local PR review comment to `.exo/reviews/pr_{N}.json` without changing an existing review decision."
  toolSchema =
    genericToolSchemaWith @PostReviewCommentArgs
      [ ("pr_number", "Local PR number to comment on"),
        ("body", "Comment body"),
        ("path", "Optional file path for the comment"),
        ("diff_hunk", "Optional diff hunk for the comment"),
        ("thread_id", "Optional thread identifier")
      ]
  toolHandlerEff args = do
    existing <- readExistingReviewFile (pcPrNumber args)
    let comment =
          ReviewComment
            { commentBody = pcBody args,
              commentPath = pcPath args,
              commentDiffHunk = pcDiffHunk args,
              commentThreadId = pcThreadId args,
              commentResolved = False
            }
        next = existing {reviewComments = reviewComments existing <> [comment]}
    result <- writeReviewFile (pcPrNumber args) next
    case result of
      Left err -> pure $ errorResult err
      Right path -> pure $ successResult $ object ["success" .= True, "path" .= path]

data Tools mode = Tools
  { notifyParent :: mode :- ReviewerNotifyParent,
    approvePr :: mode :- ReviewerApprovePR,
    requestChanges :: mode :- ReviewerRequestChanges,
    postReviewComment :: mode :- ReviewerPostReviewComment,
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
            approvePr = mkHandler @ReviewerApprovePR,
            requestChanges = mkHandler @ReviewerRequestChanges,
            postReviewComment = mkHandler @ReviewerPostReviewComment,
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
  pure (NotifyParentAction ("[STUCK: " <> T.pack (show n) <> ", rounds=" <> T.pack (show rounds_) <> "] Review did not converge. Dev leaf remains alive; ask the human for clarification.") n)

reviewerPRReviewHandler (MergeReady n ci branch_) = do
  logHandler $ "PR #" <> T.pack (show n) <> " merge ready, CI: " <> ci
  pure (NotifyParentAction ("[MERGE READY] PR #" <> T.pack (show n) <> " on branch " <> branch_ <> " has CI status " <> ci) n)

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
