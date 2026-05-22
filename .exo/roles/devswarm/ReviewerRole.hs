{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}
{-# LANGUAGE TypeOperators #-}

-- | Reviewer role: diff review only — no spawn, merge, or PR tools.
--   Tool restrictions enforced at the WASM hook layer.
module ReviewerRole (config, Tools, appendVerdict, emptyReviewFile, ReviewFile (..), ReviewVerdict (..)) where

import Control.Monad (void)
import Control.Monad.Freer (Eff)
import Data.Aeson (object, (.=))
import Data.Aeson qualified as Aeson
import Data.ByteString.Lazy qualified as BSL
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Encoding qualified as TE
import Data.Text.Lazy qualified as TL
import Data.Text.Lazy.Encoding qualified as TLE
import ExoMonad
import Effects.FilePr qualified as FPR
import ExoMonad.Effects.FilePR (FilePRLocalPrGet)
import ExoMonad.Effects.Log qualified as Log
import ExoMonad.Guest.Effects.FileSystem qualified as FS
import ExoMonad.Guest.Effects.StopHook (getCurrentBranch)
import ExoMonad.Guest.Events
  ( CIStatusEvent (..),
    EventAction (..),
    EventHandlerConfig (..),
    PRReviewEvent (..),
    SiblingMergedEvent (..),
    defaultEventHandlers,
  )
import ExoMonad.Guest.StateMachine (StopCheckResult (..), applyEvent, checkExit)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect, suspendEffect_)
import ExoMonad.Guest.Types (AfterModelOutput (..), BeforeModelOutput (..), Effects, HookInput (..), HookOutput, StopDecision (..), StopHookOutput (..), allowResponse, allowStopResponse, blockStopResponse, postToolUseResponse)
import ExoMonad.Types (HookConfig (..), defaultSessionStartHook)
import HookPolicy (preToolUseWithGitAuthorAndImplementationBlock)
import ReviewerPhase (ReviewerEvent (..), ReviewerPhase (..))

reviewerRedispatchMessage :: Text -> Text
reviewerRedispatchMessage toolName =
  "Reviewers do not edit code. The "
    <> toolName
    <> " tool is unavailable in reviewer sessions. Use `request_changes` or `post_review_comment` to relay the fix to the worker that owns the worktree. The worker's git identity is the canonical author of every commit on its branch."

reviewerVerdictExitNudge :: Text
reviewerVerdictExitNudge =
  "Verdict written. Exit now; do not continue reviewing or edit code. The watcher will route the result."

reviewerPostToolUse :: HookInput -> Eff Effects HookOutput
reviewerPostToolUse input =
  case hiToolName input of
    Just "approve_pr" -> pure $ postToolUseResponse (Just reviewerVerdictExitNudge)
    Just "request_changes" -> pure $ postToolUseResponse (Just reviewerVerdictExitNudge)
    _ -> pure $ postToolUseResponse Nothing

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
    commentResolved :: Bool,
    commentAuthorBranch :: Maybe Text
  }
  deriving (Show, Eq, Generic)

instance Aeson.ToJSON ReviewComment where
  toJSON c =
    object
      [ "body" .= commentBody c,
        "path" .= commentPath c,
        "diff_hunk" .= commentDiffHunk c,
        "thread_id" .= commentThreadId c,
        "resolved" .= commentResolved c,
        "author_branch" .= commentAuthorBranch c
      ]

instance Aeson.FromJSON ReviewComment where
  parseJSON = Aeson.withObject "ReviewComment" $ \v ->
    ReviewComment
      <$> v Aeson..:? "body" Aeson..!= ""
      <*> v Aeson..:? "path"
      <*> v Aeson..:? "diff_hunk"
      <*> v Aeson..:? "thread_id"
      <*> v Aeson..:? "resolved" Aeson..!= False
      <*> v Aeson..:? "author_branch"

data ReviewVerdict = ReviewVerdict
  { verdictState :: Text,
    verdictBody :: Text,
    verdictComments :: [ReviewComment],
    verdictAuthorBranch :: Maybe Text,
    verdictHeadSha :: Maybe Text
  }
  deriving (Show, Eq, Generic)

instance Aeson.ToJSON ReviewVerdict where
  toJSON v =
    object
      [ "state" .= verdictState v,
        "body" .= verdictBody v,
        "comments" .= verdictComments v,
        "author_branch" .= verdictAuthorBranch v,
        "head_sha" .= verdictHeadSha v
      ]

instance Aeson.FromJSON ReviewVerdict where
  parseJSON = Aeson.withObject "ReviewVerdict" $ \v ->
    ReviewVerdict
      <$> v Aeson..:? "state" Aeson..!= "none"
      <*> v Aeson..:? "body" Aeson..!= ""
      <*> v Aeson..:? "comments" Aeson..!= []
      <*> v Aeson..:? "author_branch"
      <*> v Aeson..:? "head_sha"

data ReviewFile = ReviewFile
  { reviewState :: Text,
    reviewComments :: [ReviewComment],
    reviewVerdicts :: [ReviewVerdict]
  }
  deriving (Show, Eq, Generic)

instance Aeson.ToJSON ReviewFile where
  toJSON r =
    object
      [ "state" .= reviewState r,
        "comments" .= reviewComments r,
        "verdicts" .= reviewVerdicts r
      ]

instance Aeson.FromJSON ReviewFile where
  parseJSON = Aeson.withObject "ReviewFile" $ \v ->
    ReviewFile
      <$> v Aeson..:? "state" Aeson..!= "none"
      <*> v Aeson..:? "comments" Aeson..!= []
      <*> v Aeson..:? "verdicts" Aeson..!= []

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
      maybe emptyReviewFile id (decodeReviewFile (FS.rfoContent output))
    Left _ -> emptyReviewFile

emptyReviewFile :: ReviewFile
emptyReviewFile = ReviewFile "none" [] []

appendVerdict :: Int -> Text -> Text -> Text -> Maybe Text -> [ReviewComment] -> ReviewFile -> Either Text ReviewFile
appendVerdict prNumber headSha state body authorBranch comments existing
  | T.null headSha = Left $ missingHeadShaMessage prNumber
  | any (sameHeadSha headSha) (reviewVerdicts existing) = Left $ duplicateVerdictMessage prNumber headSha
  | otherwise =
      Right $
        ReviewFile
          { reviewState = state,
            reviewComments = comments,
            reviewVerdicts =
              reviewVerdicts existing
                <> [ ReviewVerdict
                       { verdictState = state,
                         verdictBody = body,
                         verdictComments = comments,
                         verdictAuthorBranch = authorBranch,
                         verdictHeadSha = Just headSha
                       }
                   ]
          }

sameHeadSha :: Text -> ReviewVerdict -> Bool
sameHeadSha headSha verdict =
  verdictHeadSha verdict == Just headSha

duplicateVerdictMessage :: Int -> Text -> Text
duplicateVerdictMessage prNumber headSha =
  "Refused: verdict for PR #"
    <> T.pack (show prNumber)
    <> " at SHA "
    <> headSha
    <> " already exists. Reviewers are ephemeral per round; if you believe the PR needs re-review, the dev-leaf must push a new SHA first."

missingHeadShaMessage :: Int -> Text
missingHeadShaMessage prNumber =
  "Refused: cannot determine head SHA for PR #"
    <> T.pack (show prNumber)
    <> ". Verdicts are locked per PR/SHA round, so the PR registry must include last_head_sha before review."

currentHeadShaForPR :: Int -> Eff Effects (Either Text Text)
currentHeadShaForPR prNumber = do
  result <-
    suspendEffect @FilePRLocalPrGet
      FPR.LocalPrGetRequest
        { FPR.localPrGetRequestPrNumber = fromIntegral prNumber
        }
  pure $ case result of
    Right response
      | FPR.localPrResponseFound response && not (TL.null (FPR.localPrResponseLastHeadSha response)) ->
          Right (TL.toStrict (FPR.localPrResponseLastHeadSha response))
    Right _ -> Left $ missingHeadShaMessage prNumber
    Left err -> Left $ "Failed to load PR #" <> T.pack (show prNumber) <> " before writing verdict: " <> T.pack (show err)

getReviewerBranch :: Eff Effects (Either Text Text)
getReviewerBranch = do
  branch <- getCurrentBranch
  pure $
    if T.null branch || branch == "unknown"
      then Left "Refused: reviewer branch identity is unknown. Detached reviewer worktrees must report the agent birth branch before writing verdicts."
      else Right branch

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
    existing <- readExistingReviewFile (apPrNumber args)
    branchResult <- getReviewerBranch
    headShaResult <- currentHeadShaForPR (apPrNumber args)
    case (branchResult, headShaResult) of
      (Left err, _) -> pure $ errorResult err
      (_, Left err) -> pure $ errorResult err
      (Right branch, Right headSha) ->
        case appendVerdict (apPrNumber args) headSha "approved" (apBody args) (Just branch) [] existing of
          Left err -> pure $ errorResult err
          Right next -> do
            result <- writeReviewFile (apPrNumber args) next
            case result of
              Left err -> pure $ errorResult err
              Right path -> do
                void $ applyEvent @ReviewerPhase @ReviewerEvent branch ReviewerSpawned (ReviewerApprovedEv (apPrNumber args))
                pure $ successResult $ object ["success" .= True, "path" .= path]

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
    branchResult <- getReviewerBranch
    headShaResult <- currentHeadShaForPR (rcPrNumber args)
    case (branchResult, headShaResult) of
      (Left err, _) -> pure $ errorResult err
      (_, Left err) -> pure $ errorResult err
      (Right branch, Right headSha) -> do
        let comment =
              ReviewComment
                { commentBody = rcBody args,
                  commentPath = rcPath args,
                  commentDiffHunk = rcDiffHunk args,
                  commentThreadId = Nothing,
                  commentResolved = False,
                  commentAuthorBranch = Just branch
                }
        existing <- readExistingReviewFile (rcPrNumber args)
        case appendVerdict (rcPrNumber args) headSha "changes_requested" (rcBody args) (Just branch) [comment] existing of
          Left err -> pure $ errorResult err
          Right next -> do
            result <- writeReviewFile (rcPrNumber args) next
            case result of
              Left err -> pure $ errorResult err
              Right path -> do
                void $ applyEvent @ReviewerPhase @ReviewerEvent branch ReviewerSpawned (ReviewerRequestedChangesEv (rcPrNumber args) (rcBody args))
                pure $ successResult $ object ["success" .= True, "path" .= path]

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
    branchResult <- getReviewerBranch
    case branchResult of
      Left err -> pure $ errorResult err
      Right branch -> do
        let comment =
              ReviewComment
                { commentBody = pcBody args,
                  commentPath = pcPath args,
                  commentDiffHunk = pcDiffHunk args,
                  commentThreadId = pcThreadId args,
                  commentResolved = False,
                  commentAuthorBranch = Just branch
                }
            next = existing {reviewComments = reviewComments existing <> [comment]}
        result <- writeReviewFile (pcPrNumber args) next
        case result of
          Left err -> pure $ errorResult err
          Right path -> pure $ successResult $ object ["success" .= True, "path" .= path]

data Tools mode = Tools
  { approvePr :: mode :- ReviewerApprovePR,
    requestChanges :: mode :- ReviewerRequestChanges,
    postReviewComment :: mode :- ReviewerPostReviewComment
  }
  deriving (Generic)

config :: RoleConfig (Tools AsHandler)
config =
  RoleConfig
    { roleName = "reviewer",
      tools =
          Tools
            { approvePr = mkHandler @ReviewerApprovePR,
              requestChanges = mkHandler @ReviewerRequestChanges,
              postReviewComment = mkHandler @ReviewerPostReviewComment
          },
      hooks =
        HookConfig
          { preToolUse = preToolUseWithGitAuthorAndImplementationBlock reviewerRedispatchMessage (\_ -> pure (allowResponse Nothing)),
            postToolUse = reviewerPostToolUse,
            onStop = \_ -> reviewerStopCheck,
            onSubagentStop = \_ -> reviewerStopCheck,
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
  branch <- getCurrentBranch
  void $ applyEvent @ReviewerPhase @ReviewerEvent branch ReviewerSpawned (ReviewerRequestedChangesEv n comments_)
  pure (InjectMessage $ "[REVIEW] PR #" <> T.pack (show n) <> " received comments:\n" <> comments_)
reviewerPRReviewHandler (ReviewApproved n) = do
  logHandler $ "PR #" <> T.pack (show n) <> " approved"
  branch <- getCurrentBranch
  void $ applyEvent @ReviewerPhase @ReviewerEvent branch ReviewerSpawned (ReviewerApprovedEv n)
  pure NoAction
reviewerPRReviewHandler (ReviewTimeout n mins) = do
  logHandler $ "PR #" <> T.pack (show n) <> " timed out after " <> T.pack (show mins) <> " minutes"
  branch <- getCurrentBranch
  void $ applyEvent @ReviewerPhase @ReviewerEvent branch ReviewerSpawned (ReviewerTimedOutEv n mins)
  pure NoAction
reviewerPRReviewHandler (FixesPushed n ci _headSha) = do
  logHandler $ "Fixes pushed on PR #" <> T.pack (show n) <> ", CI: " <> ci
  branch <- getCurrentBranch
  void $ applyEvent @ReviewerPhase @ReviewerEvent branch ReviewerSpawned (ReviewerFixesPushedEv n ci)
  pure (InjectMessage $ "[FIXES PUSHED] PR #" <> T.pack (show n) <> " CI: " <> ci)
reviewerPRReviewHandler (CommitsPushed n ci) = do
  logHandler $ "New commits pushed on PR #" <> T.pack (show n) <> ", CI: " <> ci
  branch <- getCurrentBranch
  void $ applyEvent @ReviewerPhase @ReviewerEvent branch ReviewerSpawned (ReviewerCommitsPushedEv n ci)
  pure NoAction
reviewerPRReviewHandler (ReviewerApproved n) = do
  logHandler $ "Reviewer approved PR #" <> T.pack (show n)
  branch <- getCurrentBranch
  void $ applyEvent @ReviewerPhase @ReviewerEvent branch ReviewerSpawned (ReviewerApprovedEv n)
  pure NoAction
reviewerPRReviewHandler (ReviewerRequestedChanges n comments_) = do
  logHandler $ "Reviewer requested changes on PR #" <> T.pack (show n)
  branch <- getCurrentBranch
  void $ applyEvent @ReviewerPhase @ReviewerEvent branch ReviewerSpawned (ReviewerRequestedChangesEv n comments_)
  pure (InjectMessage $ "[CHANGES REQUESTED] PR #" <> T.pack (show n) <> ":\n" <> comments_)
reviewerPRReviewHandler (RateLimited remaining secs) = do
  logHandler $ "Rate limited: " <> T.pack (show remaining) <> " retries, " <> T.pack (show secs) <> "s"
  pure NoAction
reviewerPRReviewHandler (Stuck n rounds_) = do
  logHandler $ "PR #" <> T.pack (show n) <> " stuck after " <> T.pack (show rounds_) <> " rounds"
  branch <- getCurrentBranch
  void $ applyEvent @ReviewerPhase @ReviewerEvent branch ReviewerSpawned (ReviewerStuckEv n rounds_)
  pure NoAction
reviewerPRReviewHandler (MergeReady n ci _branch) = do
  logHandler $ "PR #" <> T.pack (show n) <> " merge ready, CI: " <> ci <> " (watcher-owned; reviewer ignores)"
  pure NoAction

reviewerSiblingMergedHandler :: SiblingMergedEvent -> Eff Effects EventAction
reviewerSiblingMergedHandler ev = do
  logHandler $ "Sibling merged: " <> mergedBranch ev
  pure NoAction

logHandler :: Text -> Eff Effects ()
logHandler msg =
  void $
    suspendEffect_ @Log.LogInfo $
      Log.InfoRequest
        { Log.infoRequestMessage = TL.fromStrict $ "[ReviewerRole] " <> msg,
          Log.infoRequestFields = ""
        }

reviewerStopCheck :: Eff Effects StopHookOutput
reviewerStopCheck = do
  branch <- getCurrentBranch
  if branch `elem` ["main", "master"]
    then pure allowStopResponse
    else do
      result <- checkExit @ReviewerPhase @ReviewerEvent branch ReviewerSpawned
      case result of
        MustBlock msg -> pure $ blockStopResponse msg
        ShouldNudge msg -> pure $ StopHookOutput Allow (Just msg)
        Clean -> pure allowStopResponse
