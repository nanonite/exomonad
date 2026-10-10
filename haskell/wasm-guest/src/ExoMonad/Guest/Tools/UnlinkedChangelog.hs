{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

-- | Post-merge CHANGELOG boundaries for a merged slice with no Chainlink issue.
--
-- A linked slice's CHANGELOG entry is written by @chainlink close@. An unlinked
-- slice has no issue, so the controller asks one @roles.tl@ judgment whether
-- the CHANGELOG already captures the merged work. These two tools are the I/O
-- on either side of that judgment:
--
-- * @post_merge_changelog_context@ reads the evidence: the @[Unreleased]@
--   section at the synchronized parent head, the paths the merge changed, and
--   the merged pull request's title and body.
-- * @post_merge_changelog_entry@ inserts the judged entry, commits it, and
--   pushes it with the same compare-and-swap lease as the linked parent push.
--   Every failure is returned as a typed outcome after the local parent has
--   been put back on the synchronized head, so nothing is left half-pushed.
module ExoMonad.Guest.Tools.UnlinkedChangelog
  ( UnlinkedChangelogContext,
    UnlinkedChangelogContextArgs (..),
    unlinkedChangelogContextCore,
    unlinkedChangelogContextDescription,
    unlinkedChangelogContextSchema,
    UnlinkedChangelogEntry,
    UnlinkedChangelogEntryArgs (..),
    unlinkedChangelogEntryCore,
    unlinkedChangelogEntryDescription,
    unlinkedChangelogEntrySchema,
    changelogSections,
    extractUnreleased,
    insertChangelogEntry,
    entryCommitArgs,
    intentTrailer,
    validateEntryText,
  )
where

import Control.Monad.Freer (Eff, Member)
import Data.Aeson (FromJSON (..), Value, object, withObject, (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.List (findIndex)
import Data.Maybe (fromMaybe)
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Effects.Fs qualified as Fs
import Effects.Git qualified as Git
import Effects.Github qualified as GH
import ExoMonad.Effects.Fs (FsWriteFile)
import ExoMonad.Effects.Git (GitGetRepoInfo)
import ExoMonad.Effects.GitHub (GitHubGetPullRequest)
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.Suspend.Types (SuspendYield)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Tools.PostMergeRecovery
  ( logInfo,
    parseRemoteHead,
    postMergePushGitArgs,
    refName,
    runGitAt,
    validToken,
  )
import GHC.Generics (Generic)

-- | Keep a Changelog section headings, in their canonical order.
changelogSections :: [Text]
changelogSections = ["Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]

-- | Longest entry text accepted from the judgment.
maxEntryLength :: Int
maxEntryLength = 300

------------------------------------------------------------------------------
-- Context
------------------------------------------------------------------------------

data UnlinkedChangelogContext

data UnlinkedChangelogContextArgs = UnlinkedChangelogContextArgs
  { uccChildId :: Text,
    uccParentBranch :: Text,
    uccExpectedBaseSha :: Text,
    uccPrNumber :: Int,
    uccMergeCommitSha :: Maybe Text,
    uccWorkingDir :: Maybe Text
  }
  deriving (Show, Eq, Generic)

instance FromJSON UnlinkedChangelogContextArgs where
  parseJSON = withObject "UnlinkedChangelogContextArgs" $ \v ->
    UnlinkedChangelogContextArgs
      <$> v .: "child_id"
      <*> v .: "parent_branch"
      <*> v .: "expected_base_sha"
      <*> v .: "pr_number"
      <*> v .:? "merge_commit_sha"
      <*> v .:? "working_dir"

unlinkedChangelogContextDescription :: Text
unlinkedChangelogContextDescription =
  "Read the CHANGELOG [Unreleased] section at the synchronized parent head, the paths a merged unlinked slice changed, and its pull request title and body."

unlinkedChangelogContextSchema :: Aeson.Object
unlinkedChangelogContextSchema =
  genericToolSchemaWith @UnlinkedChangelogContextArgs
    [ ("child_id", "Stable child slice identifier."),
      ("parent_branch", "Direct parent integration branch, which must be checked out."),
      ("expected_base_sha", "Synchronized parent head the evidence is read from."),
      ("pr_number", "Merged pull request number."),
      ("merge_commit_sha", "Authoritative merge commit; its first-parent diff gives the changed paths."),
      ("working_dir", "Optional relative repository working directory.")
    ]

instance MCPTool UnlinkedChangelogContext where
  type ToolArgs UnlinkedChangelogContext = UnlinkedChangelogContextArgs
  toolName = "post_merge_changelog_context"
  toolDescription = unlinkedChangelogContextDescription
  toolSchema = unlinkedChangelogContextSchema
  toolHandlerEff args = either errorResult successResult <$> unlinkedChangelogContextCore args

unlinkedChangelogContextCore :: (Member SuspendYield effs) => UnlinkedChangelogContextArgs -> Eff effs (Either Text Value)
unlinkedChangelogContextCore args =
  case validateContext args of
    Left err -> pure (Left err)
    Right () -> do
      onBase <- requireParentAt (uccWorkingDir args) (uccParentBranch args) (uccExpectedBaseSha args)
      case onBase of
        Left err -> pure (Left err)
        Right () -> do
          listed <- runGitAt (uccWorkingDir args) ["ls-tree", "--name-only", uccExpectedBaseSha args, "--", "CHANGELOG.md"]
          case listed of
            Left err -> pure (Left err)
            Right names -> do
              let present = T.strip names == "CHANGELOG.md"
              unreleased <-
                if present
                  then fmap extractUnreleased <$> runGitAt (uccWorkingDir args) ["show", uccExpectedBaseSha args <> ":CHANGELOG.md"]
                  else pure (Right Nothing)
              case unreleased of
                Left err -> pure (Left err)
                Right section -> do
                  paths <- changedPaths args
                  pr <- pullRequestText (uccPrNumber args)
                  logInfo
                    ( "unlinked changelog context child="
                        <> uccChildId args
                        <> " present="
                        <> T.pack (show present)
                        <> " unreleased="
                        <> T.pack (show (maybe 0 T.length section))
                    )
                  pure $
                    Right $
                      object
                        [ "child_id" .= uccChildId args,
                          "expected_base_sha" .= uccExpectedBaseSha args,
                          "changelog_present" .= present,
                          "unreleased_present" .= maybe False (const True) section,
                          "unreleased_section" .= section,
                          "changed_paths" .= either (const Nothing) Just paths,
                          "changed_paths_error" .= either Just (const Nothing) paths,
                          "pr_edited_changelog" .= either (const Nothing) (Just . elem "CHANGELOG.md") paths,
                          "pr_title" .= either (const Nothing) (Just . fst) pr,
                          "pr_body" .= either (const Nothing) (Just . snd) pr,
                          "pr_lookup_error" .= either Just (const Nothing) pr
                        ]

changedPaths :: (Member SuspendYield effs) => UnlinkedChangelogContextArgs -> Eff effs (Either Text [Text])
changedPaths args =
  case uccMergeCommitSha args of
    Nothing -> pure (Left "no authoritative merge commit is recorded for this slice")
    Just mergeCommit
      | not (validToken mergeCommit) -> pure (Left "merge_commit_sha is invalid")
      | otherwise ->
          fmap (filter (not . T.null) . map T.strip . T.lines)
            <$> runGitAt (uccWorkingDir args) ["diff", "--name-only", mergeCommit <> "^1", mergeCommit]

pullRequestText :: (Member SuspendYield effs) => Int -> Eff effs (Either Text (Text, Text))
pullRequestText prNumber = do
  repoInfo <- suspendEffect @GitGetRepoInfo (Git.GetRepoInfoRequest {Git.getRepoInfoRequestWorkingDir = "."})
  case repoInfo of
    Left err -> pure (Left ("repository lookup failed: " <> T.pack (show err)))
    Right info -> do
      response <-
        suspendEffect @GitHubGetPullRequest
          GH.GetPullRequestRequest
            { GH.getPullRequestRequestOwner = Git.getRepoInfoResponseOwner info,
              GH.getPullRequestRequestRepo = Git.getRepoInfoResponseName info,
              GH.getPullRequestRequestNumber = fromIntegral prNumber,
              GH.getPullRequestRequestIncludeReviews = False
            }
      pure $ case response of
        Left err -> Left ("pull request lookup failed: " <> T.pack (show err))
        Right resp -> case GH.getPullRequestResponsePullRequest resp of
          Nothing -> Left "pull request lookup returned no pull request"
          Just pr -> Right (TL.toStrict (GH.pullRequestTitle pr), TL.toStrict (GH.pullRequestBody pr))

validateContext :: UnlinkedChangelogContextArgs -> Either Text ()
validateContext args
  | not (validToken (uccChildId args)) = Left "child_id is required"
  | not (validToken (uccParentBranch args)) = Left "parent_branch is required"
  | not (validToken (uccExpectedBaseSha args)) = Left "expected_base_sha is required"
  | uccPrNumber args <= 0 = Left "pr_number must be positive"
  | otherwise = Right ()

------------------------------------------------------------------------------
-- Entry
------------------------------------------------------------------------------

data UnlinkedChangelogEntry

data UnlinkedChangelogEntryArgs = UnlinkedChangelogEntryArgs
  { uceChildId :: Text,
    uceRepository :: Text,
    uceParentBranch :: Text,
    uceExpectedBaseSha :: Text,
    uceIntentId :: Text,
    uceSection :: Text,
    uceEntry :: Text,
    uceWorkingDir :: Maybe Text
  }
  deriving (Show, Eq, Generic)

instance FromJSON UnlinkedChangelogEntryArgs where
  parseJSON = withObject "UnlinkedChangelogEntryArgs" $ \v ->
    UnlinkedChangelogEntryArgs
      <$> v .: "child_id"
      <*> v .: "repository"
      <*> v .: "parent_branch"
      <*> v .: "expected_base_sha"
      <*> v .: "intent_id"
      <*> v .: "section"
      <*> v .: "entry"
      <*> v .:? "working_dir"

unlinkedChangelogEntryDescription :: Text
unlinkedChangelogEntryDescription =
  "Insert one CHANGELOG [Unreleased] entry for a merged unlinked slice, commit it on the parent branch, and push it with a compare-and-swap lease; every failure is returned as a typed outcome after the local parent is restored."

unlinkedChangelogEntrySchema :: Aeson.Object
unlinkedChangelogEntrySchema =
  genericToolSchemaWith @UnlinkedChangelogEntryArgs
    [ ("child_id", "Stable child slice identifier."),
      ("repository", "Authoritative repository identity."),
      ("parent_branch", "Direct parent integration branch, which must be checked out."),
      ("expected_base_sha", "Synchronized parent head; the push lease expects the remote here."),
      ("intent_id", "Durable entry intent, recorded as a commit trailer."),
      ("section", "Keep a Changelog section: Added, Changed, Deprecated, Removed, Fixed, or Security."),
      ("entry", "One-line entry text without the leading list marker."),
      ("working_dir", "Optional relative repository working directory.")
    ]

instance MCPTool UnlinkedChangelogEntry where
  type ToolArgs UnlinkedChangelogEntry = UnlinkedChangelogEntryArgs
  toolName = "post_merge_changelog_entry"
  toolDescription = unlinkedChangelogEntryDescription
  toolSchema = unlinkedChangelogEntrySchema
  toolHandlerEff args = either errorResult successResult <$> unlinkedChangelogEntryCore args

-- | Write, commit, and push one entry. 'Left' is reserved for invalid
-- arguments; every Git outcome is a 'Right' carrying a typed @outcome@.
unlinkedChangelogEntryCore :: (Member SuspendYield effs) => UnlinkedChangelogEntryArgs -> Eff effs (Either Text Value)
unlinkedChangelogEntryCore args =
  case validateEntry args of
    Left err -> pure (Left err)
    Right () -> do
      branch <- runGitAt wd ["branch", "--show-current"]
      case branch of
        Left err -> outcome "entry_write_failed" err Nothing
        Right current
          | T.strip current /= uceParentBranch args ->
              outcome "entry_write_failed" "the requested parent branch is not checked out" Nothing
          | otherwise -> do
              headSha <- runGitAt wd ["rev-parse", "HEAD"]
              case headSha of
                Left err -> outcome "entry_write_failed" err Nothing
                Right sha
                  | sha == uceExpectedBaseSha args -> writeAndCommit
                  | otherwise -> resumeCommitted sha
  where
    wd = uceWorkingDir args

    writeAndCommit = do
      original <- runGitAt wd ["show", "HEAD:CHANGELOG.md"]
      case original >>= insertChangelogEntry (uceSection args) (uceEntry args) of
        Left err -> outcome "entry_write_failed" err Nothing
        Right updated -> do
          written <-
            suspendEffect @FsWriteFile
              Fs.WriteFileRequest
                { Fs.writeFileRequestPath = TL.fromStrict (changelogPath wd),
                  Fs.writeFileRequestContent = TL.fromStrict updated,
                  Fs.writeFileRequestCreateParents = False,
                  Fs.writeFileRequestAppend = False
                }
          case written of
            Left err -> restoreThen ("CHANGELOG.md write failed: " <> T.pack (show err))
            Right _ -> do
              staged <- runGitAt wd ["add", "--", "CHANGELOG.md"]
              committed <- either (pure . Left) (const (runGitAt wd (entryCommitArgs (uceChildId args) (uceIntentId args)))) staged
              case committed of
                Left err -> restoreThen err
                Right _ -> do
                  commitSha <- runGitAt wd ["rev-parse", "HEAD"]
                  case commitSha of
                    Left err -> resetThen "entry_write_failed" err
                    Right sha -> push sha

    -- A previous call committed this intent and stopped before reporting.
    resumeCommitted sha = do
      parentSha <- runGitAt wd ["rev-parse", "HEAD^"]
      message <- runGitAt wd ["log", "-1", "--format=%B", "HEAD"]
      case (parentSha, message) of
        (Right parent, Right body)
          | parent == uceExpectedBaseSha args && intentTrailer (uceIntentId args) `T.isInfixOf` body ->
              push sha
        _ -> outcome "entry_write_failed" "parent head moved after synchronization" Nothing

    push commitSha = do
      remote <- remoteHead
      case remote of
        Right observed | observed == commitSha -> added commitSha observed
        _ -> do
          pushed <- runGitAt wd (postMergePushGitArgs (uceParentBranch args) (uceExpectedBaseSha args))
          case pushed of
            Left err -> resetThen "entry_push_failed" err
            Right _ -> do
              observed <- remoteHead
              case observed of
                Left err -> outcome "entry_added" ("push succeeded but the remote head could not be read: " <> err) (Just (commitSha, commitSha))
                Right headSha -> added commitSha headSha

    remoteHead = do
      listed <- runGitAt wd ["ls-remote", "--exit-code", "origin", refName (uceParentBranch args)]
      pure (listed >>= parseRemoteHead)

    added commitSha observed = outcome "entry_added" "" (Just (commitSha, observed))

    restoreThen detail = do
      restored <- runGitAt wd ["checkout", "HEAD", "--", "CHANGELOG.md"]
      outcome "entry_write_failed" (withRecovery detail restored) Nothing

    resetThen kind detail = do
      reset <- runGitAt wd ["reset", "--keep", uceExpectedBaseSha args]
      outcome kind (withRecovery detail reset) Nothing

    withRecovery detail recovered =
      either (\err -> detail <> "; local restore failed: " <> err) (const detail) recovered

    outcome :: (Member SuspendYield effs) => Text -> Text -> Maybe (Text, Text) -> Eff effs (Either Text Value)
    outcome kind detail commit = do
      logInfo ("unlinked changelog entry child=" <> uceChildId args <> " outcome=" <> kind)
      pure $
        Right $
          object
            [ "child_id" .= uceChildId args,
              "repository" .= uceRepository args,
              "parent_branch" .= uceParentBranch args,
              "expected_base_sha" .= uceExpectedBaseSha args,
              "intent_id" .= uceIntentId args,
              "outcome" .= kind,
              "detail" .= T.take 500 detail,
              "commit_sha" .= fmap fst commit,
              "observed_remote_head" .= fmap snd commit
            ]

changelogPath :: Maybe Text -> Text
changelogPath wd = case fromMaybe "." wd of
  "." -> "CHANGELOG.md"
  dir -> T.dropWhileEnd (== '/') dir <> "/CHANGELOG.md"

-- | The commit trailer that makes an interrupted entry recognizable on retry.
intentTrailer :: Text -> Text
intentTrailer intentId = "Exomonad-Changelog-Intent: " <> intentId

-- | Git argv for committing only the CHANGELOG entry.
entryCommitArgs :: Text -> Text -> [Text]
entryCommitArgs childId intentId =
  [ "commit",
    "--only",
    "CHANGELOG.md",
    "-m",
    "chore: record changelog for merged slice " <> childId,
    "-m",
    intentTrailer intentId
  ]

validateEntry :: UnlinkedChangelogEntryArgs -> Either Text ()
validateEntry args
  | not (validToken (uceChildId args)) = Left "child_id is required"
  | not (validToken (uceRepository args)) = Left "repository is required"
  | not (validToken (uceParentBranch args)) = Left "parent_branch is required"
  | not (validToken (uceExpectedBaseSha args)) = Left "expected_base_sha is required"
  | not (validToken (uceIntentId args)) = Left "intent_id is required"
  | uceSection args `notElem` changelogSections = Left "section must be a Keep a Changelog section"
  | otherwise = validateEntryText (uceEntry args)

-- | An entry is one non-empty line that does not carry its own list marker.
validateEntryText :: Text -> Either Text ()
validateEntryText entry
  | T.null stripped = Left "entry is required"
  | stripped /= entry = Left "entry must not have surrounding whitespace"
  | T.any (\c -> c == '\n' || c == '\r') entry = Left "entry must be one line"
  | T.length entry > maxEntryLength = Left "entry is longer than 300 characters"
  | "- " `T.isPrefixOf` entry || "#" `T.isPrefixOf` entry = Left "entry must not start with a list marker or heading"
  | otherwise = Right ()
  where
    stripped = T.strip entry

------------------------------------------------------------------------------
-- Pure CHANGELOG editing
------------------------------------------------------------------------------

-- | The body of the @## [Unreleased]@ section, or 'Nothing' when it is absent.
extractUnreleased :: Text -> Maybe Text
extractUnreleased content = do
  let ls = T.lines content
  start <- findIndex isUnreleasedHeading ls
  let body = takeWhile (not . isReleaseHeading) (drop (start + 1) ls)
  pure (T.strip (T.unlines body))

-- | Insert @- entry@ at the top of @### section@ inside @## [Unreleased]@,
-- creating that heading in canonical Keep a Changelog order when it is absent.
insertChangelogEntry :: Text -> Text -> Text -> Either Text Text
insertChangelogEntry section entry content
  | section `notElem` changelogSections = Left "section must be a Keep a Changelog section"
  | otherwise = do
      validateEntryText entry
      let ls = T.lines content
      start <- maybe (Left "CHANGELOG.md has no ## [Unreleased] section") Right (findIndex isUnreleasedHeading ls)
      let end = maybe (length ls) (+ (start + 1)) (findIndex isReleaseHeading (drop (start + 1) ls))
          inSection = [start + 1 .. end - 1]
          line = "- " <> entry
          headingAt i = subsectionName (ls !! i)
      case filter (\i -> headingAt i == Just section) inSection of
        (heading : _) -> Right (render (insertAt (heading + 1) [line] ls))
        [] -> do
          let later i = maybe False (\name -> rank name > rank section) (headingAt i)
              block = ["### " <> section, line, ""]
          case filter later inSection of
            (next : _) -> Right (render (insertAt next block ls))
            [] ->
              let lastContent = last (start : filter (\i -> not (T.null (T.strip (ls !! i)))) inSection)
               in Right (render (insertAt (lastContent + 1) ["", "### " <> section, line] ls))
  where
    rank name = fromMaybe (length changelogSections) (findIndex (== name) changelogSections)
    insertAt i xs ls = take i ls ++ xs ++ drop i ls
    render ls = T.intercalate "\n" (trimTrailingBlank ls) <> "\n"
    trimTrailingBlank = reverse . dropWhile (T.null . T.strip) . reverse

isUnreleasedHeading :: Text -> Bool
isUnreleasedHeading line = T.strip line == "## [Unreleased]"

isReleaseHeading :: Text -> Bool
isReleaseHeading line = "## " `T.isPrefixOf` line

subsectionName :: Text -> Maybe Text
subsectionName line = T.strip <$> T.stripPrefix "### " line

requireParentAt :: (Member SuspendYield effs) => Maybe Text -> Text -> Text -> Eff effs (Either Text ())
requireParentAt wd branch expected = do
  current <- runGitAt wd ["branch", "--show-current"]
  case current of
    Left err -> pure (Left err)
    Right name
      | T.strip name /= branch -> pure (Left "the requested parent branch is not checked out")
      | otherwise -> do
          headSha <- runGitAt wd ["rev-parse", "HEAD"]
          pure $ case headSha of
            Left err -> Left err
            Right sha
              | sha /= expected -> Left "parent branch moved after synchronization"
              | otherwise -> Right ()
