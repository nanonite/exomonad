{-# LANGUAGE DataKinds #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.Chainlink
  ( -- * Issue Create
    ChainlinkIssueCreate (..),
    chainlinkIssueCreateCore,
    chainlinkIssueCreateDescription,
    chainlinkIssueCreateSchema,
    ChainlinkIssueCreateArgs (..),
    ChainlinkIssueCreateOutput (..),

    -- * Session Start
    ChainlinkSessionStart (..),
    chainlinkSessionStartCore,
    chainlinkSessionStartDescription,
    chainlinkSessionStartSchema,

    -- * Session Status
    ChainlinkSessionStatus (..),
    chainlinkSessionStatusCore,
    chainlinkSessionStatusDescription,
    chainlinkSessionStatusSchema,

    -- * Issue Show
    ChainlinkIssueShow (..),
    chainlinkIssueShowCore,
    chainlinkIssueShowDescription,
    chainlinkIssueShowSchema,
    ChainlinkIssueShowArgs (..),

    -- * Issue Comment
    ChainlinkIssueComment (..),
    chainlinkIssueCommentCore,
    chainlinkIssueCommentDescription,
    chainlinkIssueCommentSchema,
    ChainlinkIssueCommentArgs (..),

    -- * Subissue Create
    ChainlinkSubissueCreate (..),
    chainlinkSubissueCreateCore,
    chainlinkSubissueCreateDescription,
    chainlinkSubissueCreateSchema,
    ChainlinkSubissueCreateArgs (..),

    -- * Session Work
    ChainlinkSessionWork (..),
    chainlinkSessionWorkCore,
    chainlinkSessionWorkDescription,
    chainlinkSessionWorkSchema,
    ChainlinkSessionWorkArgs (..),

    -- * Session End
    ChainlinkSessionEnd (..),
    chainlinkSessionEndCore,
    chainlinkSessionEndDescription,
    chainlinkSessionEndSchema,
    ChainlinkSessionEndArgs (..),

    -- * Issue Close
    ChainlinkIssueClose (..),
    chainlinkIssueCloseCore,
    chainlinkIssueCloseDescription,
    chainlinkIssueCloseSchema,
    ChainlinkIssueCloseArgs (..),

    -- * Subissue Close
    ChainlinkSubissueClose (..),
    chainlinkSubissueCloseDescription,
    chainlinkSubissueCloseSchema,

    -- * Timer
    ChainlinkTimerStart (..),
    ChainlinkTimerStop (..),
    ChainlinkTimerStatus (..),
    chainlinkTimerStartCore,
    chainlinkTimerStopCore,
    chainlinkTimerStatusCore,
    chainlinkTimerStartDescription,
    chainlinkTimerStopDescription,
    chainlinkTimerStatusDescription,
    chainlinkTimerStartSchema,
    chainlinkTimerStopSchema,
    chainlinkTimerStatusSchema,

    -- * Issue List
    ChainlinkIssueList (..),
    chainlinkIssueListCore,
    chainlinkIssueListDescription,
    chainlinkIssueListSchema,

    -- * Issue Update
    ChainlinkIssueUpdate (..),
    chainlinkIssueUpdateCore,
    chainlinkIssueUpdateDescription,
    chainlinkIssueUpdateSchema,

    -- * Block
    ChainlinkBlock (..),
    chainlinkBlockCore,
    chainlinkBlockDescription,
    chainlinkBlockSchema,

    -- * Relate
    ChainlinkRelate (..),
    chainlinkRelateCore,
    chainlinkRelateDescription,
    chainlinkRelateSchema,

    -- * Cascade
    ChainlinkCascade (..),
    chainlinkCascadeCore,
    chainlinkCascadeDescription,
    chainlinkCascadeSchema,

    -- * Milestone Create
    ChainlinkMilestoneCreate (..),
    chainlinkMilestoneCreateCore,
    chainlinkMilestoneCreateDescription,
    chainlinkMilestoneCreateSchema,

    -- * Milestone List
    ChainlinkMilestoneList (..),
    chainlinkMilestoneListCore,
    chainlinkMilestoneListDescription,
    chainlinkMilestoneListSchema,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), ToJSON (..), Value, object, withObject, (.!=), (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Int (Int32)
import Data.Map qualified as Map
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Encoding (encodeUtf8)
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Data.Word (Word64)
import Effects.Git qualified as Git
import Effects.Process qualified as Proc
import ExoMonad.Effects.Git (GitGetStatus)
import ExoMonad.Effects.Process (ProcessRun)
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Tools.Chainlink.Pure
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

chainlinkTimeoutMs :: Word64
chainlinkTimeoutMs = 30000

runChainlink :: [String] -> Eff Effects (Either Text Proc.RunResponse)
runChainlink cmdArgs = do
  result <-
    suspendEffect @ProcessRun
      ( Proc.RunRequest
          { Proc.runRequestCommand = "chainlink",
            Proc.runRequestArgs = V.fromList (TL.pack <$> cmdArgs),
            Proc.runRequestWorkingDir = ".",
            Proc.runRequestEnv = Map.empty,
            Proc.runRequestTimeoutMs = chainlinkTimeoutMs
          }
      )
  case result of
    Left err -> pure $ Left (T.pack (show err))
    Right resp -> pure $ Right resp

exitCodeToText :: Int32 -> Text
exitCodeToText = T.pack . show

--------------------------------------------------------------------------------
-- Issue Create
--------------------------------------------------------------------------------

instance FromJSON ChainlinkIssueCreateArgs where
  parseJSON = withObject "ChainlinkIssueCreateArgs" $ \v ->
    ChainlinkIssueCreateArgs
      <$> v .: "title"
      <*> v .:? "description"
      <*> v .:? "priority"
      <*> v .:? "labels"

instance ToJSON ChainlinkIssueCreateArgs where
  toJSON args =
    object
      [ "title" .= cicaTitle args,
        "description" .= cicaDescription args,
        "priority" .= cicaPriority args,
        "labels" .= cicaLabels args
      ]

data ChainlinkIssueCreateOutput = ChainlinkIssueCreateOutput
  { cicoIssueId :: Int
  }
  deriving (Generic, Show)

instance FromJSON ChainlinkIssueCreateOutput

instance ToJSON ChainlinkIssueCreateOutput

chainlinkIssueCreateDescription :: Text
chainlinkIssueCreateDescription =
  "Create a new chainlink issue. Returns the created issue ID. Use this to track work items, bugs, features, and tasks in the project's chainlink tracker."

chainlinkIssueCreateSchema :: Aeson.Object
chainlinkIssueCreateSchema =
  genericToolSchemaWith @ChainlinkIssueCreateArgs
    [ ("title", "Issue title — should be a concise, changelog-ready description of the work"),
      ("description", "Optional detailed description of the issue"),
      ("priority", "Optional priority: low, medium, high, critical (default: medium)"),
      ("labels", "Optional list of labels to apply (e.g. bug, feature, enhancement)")
    ]

chainlinkIssueCreateCore :: ChainlinkIssueCreateArgs -> Eff Effects (Either Text ChainlinkIssueCreateOutput)
chainlinkIssueCreateCore args = do
  let cmdArgs = buildCreateArgs args
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink create failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          case parseIssueId (TL.toStrict (Proc.runResponseStdout resp)) of
            Just issueId ->
              pure $ Right (ChainlinkIssueCreateOutput issueId)
            Nothing ->
              pure $ Left ("could not parse issue ID from output: " <> TL.toStrict (Proc.runResponseStdout resp))
      | otherwise ->
          pure $
            Left $
              "chainlink create failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkIssueCreate

instance MCPTool ChainlinkIssueCreate where
  type ToolArgs ChainlinkIssueCreate = ChainlinkIssueCreateArgs
  toolName = "chainlink_issue_create"
  toolDescription = chainlinkIssueCreateDescription
  toolSchema = chainlinkIssueCreateSchema
  toolHandlerEff args = do
    result <- chainlinkIssueCreateCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult (Aeson.toJSON output)

--------------------------------------------------------------------------------
-- Session Start
--------------------------------------------------------------------------------

chainlinkSessionStartDescription :: Text
chainlinkSessionStartDescription =
  "Start a chainlink work session for the current agent. Call this before chainlink_session_work."

chainlinkSessionStartSchema :: Aeson.Object
chainlinkSessionStartSchema = mempty

chainlinkSessionStartCore :: Eff Effects (Either Text ())
chainlinkSessionStartCore = do
  result <- runChainlink buildSessionStartArgs
  case result of
    Left err -> pure $ Left ("chainlink session start failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          pure $ Right ()
      | otherwise ->
          pure $
            Left $
              "chainlink session start failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkSessionStart

instance MCPTool ChainlinkSessionStart where
  type ToolArgs ChainlinkSessionStart = ()
  toolName = "chainlink_session_start"
  toolDescription = chainlinkSessionStartDescription
  toolSchema = chainlinkSessionStartSchema
  toolHandlerEff _ = do
    result <- chainlinkSessionStartCore
    case result of
      Left err -> pure $ errorResult err
      Right _ -> pure $ successResult (object ["success" .= True])

--------------------------------------------------------------------------------
-- Session Status
--------------------------------------------------------------------------------

chainlinkSessionStatusDescription :: Text
chainlinkSessionStatusDescription =
  "Read the current Chainlink session status. Coordinators use this for work-state telemetry; it does not mutate issue state."

chainlinkSessionStatusSchema :: Aeson.Object
chainlinkSessionStatusSchema = mempty

chainlinkSessionStatusCore :: Eff Effects (Either Text Value)
chainlinkSessionStatusCore = do
  result <- runChainlink buildSessionStatusArgs
  case result of
    Left err -> pure $ Left ("chainlink session status failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          case Aeson.eitherDecodeStrict (encodeUtf8 (TL.toStrict (Proc.runResponseStdout resp))) of
            Right value -> pure $ Right value
            Left err -> pure $ Left ("chainlink session status returned invalid JSON: " <> T.pack err)
      | otherwise ->
          pure $
            Left $
              "chainlink session status failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkSessionStatus

instance MCPTool ChainlinkSessionStatus where
  type ToolArgs ChainlinkSessionStatus = ()
  toolName = "chainlink_session_status"
  toolDescription = chainlinkSessionStatusDescription
  toolSchema = chainlinkSessionStatusSchema
  toolHandlerEff _ = do
    result <- chainlinkSessionStatusCore
    case result of
      Left err -> pure $ errorResult err
      Right value -> pure $ successResult value

--------------------------------------------------------------------------------
-- Issue Show
--------------------------------------------------------------------------------

data ChainlinkIssueShowArgs = ChainlinkIssueShowArgs
  { cisaIssueId :: Int
  }
  deriving (Generic, Show)

instance FromJSON ChainlinkIssueShowArgs where
  parseJSON = withObject "ChainlinkIssueShowArgs" $ \v ->
    ChainlinkIssueShowArgs <$> v .: "issue_id"

instance ToJSON ChainlinkIssueShowArgs where
  toJSON args =
    object
      [ "issue_id" .= cisaIssueId args
      ]

chainlinkIssueShowDescription :: Text
chainlinkIssueShowDescription =
  "Show details of a chainlink issue by ID. Returns the issue title, status, priority, and labels. Use this to inspect a task before working on it."

chainlinkIssueShowSchema :: Aeson.Object
chainlinkIssueShowSchema =
  genericToolSchemaWith @ChainlinkIssueShowArgs
    [ ("issue_id", "The numeric ID of the issue to view")
    ]

chainlinkIssueShowCore :: ChainlinkIssueShowArgs -> Eff Effects (Either Text ChainlinkIssueShowOutput)
chainlinkIssueShowCore args = do
  let cmdArgs = buildShowArgs (cisaIssueId args)
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink issue show failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          case Aeson.eitherDecodeStrict (encodeUtf8 (TL.toStrict (Proc.runResponseStdout resp))) of
            Right output -> pure $ Right output
            Left parseErr ->
              pure $ Left ("could not parse issue show output: " <> T.pack parseErr)
      | otherwise ->
          pure $
            Left $
              "chainlink issue show failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkIssueShow

instance MCPTool ChainlinkIssueShow where
  type ToolArgs ChainlinkIssueShow = ChainlinkIssueShowArgs
  toolName = "chainlink_issue_show"
  toolDescription = chainlinkIssueShowDescription
  toolSchema = chainlinkIssueShowSchema
  toolHandlerEff args = do
    result <- chainlinkIssueShowCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult (Aeson.toJSON output)

--------------------------------------------------------------------------------
-- Issue Comment
--------------------------------------------------------------------------------

instance FromJSON ChainlinkIssueCommentArgs where
  parseJSON = withObject "ChainlinkIssueCommentArgs" $ \v ->
    ChainlinkIssueCommentArgs
      <$> v .: "issue_id"
      <*> v .: "message"

instance ToJSON ChainlinkIssueCommentArgs where
  toJSON args =
    object
      [ "issue_id" .= cicIssueId args,
        "message" .= cicMessage args
      ]

chainlinkIssueCommentDescription :: Text
chainlinkIssueCommentDescription =
  "Add a comment to a chainlink issue. Use this to report progress, ask questions, or provide updates on a task."

chainlinkIssueCommentSchema :: Aeson.Object
chainlinkIssueCommentSchema =
  genericToolSchemaWith @ChainlinkIssueCommentArgs
    [ ("issue_id", "The numeric ID of the issue to comment on"),
      ("message", "The comment text to add")
    ]

chainlinkIssueCommentCore :: ChainlinkIssueCommentArgs -> Eff Effects (Either Text ChainlinkIssueCommentOutput)
chainlinkIssueCommentCore args = do
  let cmdArgs = buildCommentArgs args
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink comment failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          pure $ Right (ChainlinkIssueCommentOutput True)
      | otherwise ->
          pure $
            Left $
              "chainlink comment failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkIssueComment

instance MCPTool ChainlinkIssueComment where
  type ToolArgs ChainlinkIssueComment = ChainlinkIssueCommentArgs
  toolName = "chainlink_issue_comment"
  toolDescription = chainlinkIssueCommentDescription
  toolSchema = chainlinkIssueCommentSchema
  toolHandlerEff args = do
    result <- chainlinkIssueCommentCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult (Aeson.toJSON output)

--------------------------------------------------------------------------------
-- Subissue Create
--------------------------------------------------------------------------------

instance FromJSON ChainlinkSubissueCreateArgs where
  parseJSON = withObject "ChainlinkSubissueCreateArgs" $ \v ->
    ChainlinkSubissueCreateArgs
      <$> v .: "parent_id"
      <*> v .: "title"
      <*> v .:? "priority"
      <*> v .:? "labels"

instance ToJSON ChainlinkSubissueCreateArgs where
  toJSON args =
    object
      [ "parent_id" .= cscParentId args,
        "title" .= cscTitle args,
        "priority" .= cscPriority args,
        "labels" .= cscLabels args
      ]

chainlinkSubissueCreateDescription :: Text
chainlinkSubissueCreateDescription =
  "Create a new sub-issue under a parent chainlink issue. Returns the created sub-issue ID. Use this to break down tasks into smaller work items."

chainlinkSubissueCreateSchema :: Aeson.Object
chainlinkSubissueCreateSchema =
  genericToolSchemaWith @ChainlinkSubissueCreateArgs
    [ ("parent_id", "The numeric ID of the parent issue"),
      ("title", "Sub-issue title — should be a concise, changelog-ready description"),
      ("priority", "Optional priority: low, medium, high, critical (default: medium)"),
      ("labels", "Optional list of labels to apply (e.g. bug, feature, enhancement)")
    ]

chainlinkSubissueCreateCore :: ChainlinkSubissueCreateArgs -> Eff Effects (Either Text ChainlinkIssueCreateOutput)
chainlinkSubissueCreateCore args = do
  let cmdArgs = buildSubissueArgs args
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink subissue create failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          case parseIssueId (TL.toStrict (Proc.runResponseStdout resp)) of
            Just issueId ->
              pure $ Right (ChainlinkIssueCreateOutput issueId)
            Nothing ->
              pure $ Left ("could not parse sub-issue ID from output: " <> TL.toStrict (Proc.runResponseStdout resp))
      | otherwise ->
          pure $
            Left $
              "chainlink subissue create failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkSubissueCreate

instance MCPTool ChainlinkSubissueCreate where
  type ToolArgs ChainlinkSubissueCreate = ChainlinkSubissueCreateArgs
  toolName = "chainlink_subissue_create"
  toolDescription = chainlinkSubissueCreateDescription
  toolSchema = chainlinkSubissueCreateSchema
  toolHandlerEff args = do
    result <- chainlinkSubissueCreateCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult (Aeson.toJSON output)

--------------------------------------------------------------------------------
-- Session Work
--------------------------------------------------------------------------------

instance FromJSON ChainlinkSessionWorkArgs where
  parseJSON = withObject "ChainlinkSessionWorkArgs" $ \v ->
    ChainlinkSessionWorkArgs <$> v .: "issue_id"

instance ToJSON ChainlinkSessionWorkArgs where
  toJSON args =
    object
      [ "issue_id" .= cswIssueId args
      ]

chainlinkSessionWorkDescription :: Text
chainlinkSessionWorkDescription =
  "Mark a chainlink issue as the current active work item. Use this to indicate which issue you are actively working on."

chainlinkSessionWorkSchema :: Aeson.Object
chainlinkSessionWorkSchema =
  genericToolSchemaWith @ChainlinkSessionWorkArgs
    [ ("issue_id", "The numeric ID of the issue to mark as active work")
    ]

chainlinkSessionWorkCore :: ChainlinkSessionWorkArgs -> Eff Effects (Either Text ())
chainlinkSessionWorkCore args = do
  let cmdArgs = buildSessionWorkArgs args
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink session work failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          pure $ Right ()
      | otherwise ->
          pure $
            Left $
              "chainlink session work failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkSessionWork

instance MCPTool ChainlinkSessionWork where
  type ToolArgs ChainlinkSessionWork = ChainlinkSessionWorkArgs
  toolName = "chainlink_session_work"
  toolDescription = chainlinkSessionWorkDescription
  toolSchema = chainlinkSessionWorkSchema
  toolHandlerEff args = do
    result <- chainlinkSessionWorkCore args
    case result of
      Left err -> pure $ errorResult err
      Right _ -> pure $ successResult (object ["success" .= True])

--------------------------------------------------------------------------------
-- Session End
--------------------------------------------------------------------------------

instance FromJSON ChainlinkSessionEndArgs where
  parseJSON = withObject "ChainlinkSessionEndArgs" $ \v ->
    ChainlinkSessionEndArgs <$> v .:? "notes"

instance ToJSON ChainlinkSessionEndArgs where
  toJSON args =
    object
      [ "notes" .= cseNotes args
      ]

chainlinkSessionEndDescription :: Text
chainlinkSessionEndDescription =
  "End the current chainlink session with optional notes. Use this when you finish working on a task to record what was done."

chainlinkSessionEndSchema :: Aeson.Object
chainlinkSessionEndSchema =
  genericToolSchemaWith @ChainlinkSessionEndArgs
    [ ("notes", "Optional handoff notes describing what was accomplished during the session")
    ]

chainlinkSessionEndCore :: ChainlinkSessionEndArgs -> Eff Effects (Either Text ())
chainlinkSessionEndCore args = do
  let cmdArgs = buildSessionEndArgs args
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink session end failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          pure $ Right ()
      | otherwise ->
          pure $
            Left $
              "chainlink session end failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkSessionEnd

instance MCPTool ChainlinkSessionEnd where
  type ToolArgs ChainlinkSessionEnd = ChainlinkSessionEndArgs
  toolName = "chainlink_session_end"
  toolDescription = chainlinkSessionEndDescription
  toolSchema = chainlinkSessionEndSchema
  toolHandlerEff args = do
    result <- chainlinkSessionEndCore args
    case result of
      Left err -> pure $ errorResult err
      Right _ -> pure $ successResult (object ["success" .= True])

--------------------------------------------------------------------------------
-- Issue Close
--------------------------------------------------------------------------------

instance FromJSON ChainlinkIssueCloseArgs where
  parseJSON = withObject "ChainlinkIssueCloseArgs" $ \v ->
    ChainlinkIssueCloseArgs
      <$> v .: "issue_id"
      <*> v .:? "summary"
      <*> v .:? "force" .!= False

instance ToJSON ChainlinkIssueCloseArgs where
  toJSON args =
    object
      [ "issue_id" .= cisIssueId args,
        "summary" .= cisSummary args
      ]

chainlinkIssueCloseDescription :: Text
chainlinkIssueCloseDescription =
  "Coordinator-only close tool. Use after the implementing agent ended its session and PR review, CI, and merge conditions are satisfied. "
    <> "This runs only `chainlink close`; it never touches Chainlink locks or agent identity."

chainlinkIssueCloseSchema :: Aeson.Object
chainlinkIssueCloseSchema =
  genericToolSchemaWith @ChainlinkIssueCloseArgs
    [ ("issue_id", "The numeric ID of the issue to close"),
      ("summary", "Optional summary of what was completed. Defaults to \"Closed #<id>\" if omitted.")
    ]

chainlinkIssueCloseCore :: ChainlinkIssueCloseArgs -> Eff Effects (Either Text ())
chainlinkIssueCloseCore args = do
  clean <- if cisForce args then pure (Right ()) else ensureCleanWorktreeForClose (cisIssueId args)
  case clean of
    Left err -> pure (Left err)
    Right () -> do
      result <- runChainlink (buildCloseArgs args)
      case result of
        Left err -> pure $ Left ("chainlink close failed: " <> err)
        Right resp
          | Proc.runResponseExitCode resp == 0 -> pure $ Right ()
          | otherwise ->
              pure $
                Left $
                  "chainlink close failed (exit "
                    <> exitCodeToText (Proc.runResponseExitCode resp)
                    <> "): "
                    <> TL.toStrict (Proc.runResponseStderr resp)

ensureCleanWorktreeForClose :: Int -> Eff Effects (Either Text ())
ensureCleanWorktreeForClose issueId = do
  statusResult <- suspendEffect @GitGetStatus (Git.GetStatusRequest {Git.getStatusRequestWorkingDir = "."})
  case statusResult of
    Right status
      | not (null (Git.getStatusResponseDirtyFiles status) && null (Git.getStatusResponseStagedFiles status)) ->
          pure $ Left $ closeDirtyMessage issueId status
    _ -> pure (Right ())

closeDirtyMessage :: Int -> Git.GetStatusResponse -> Text
closeDirtyMessage issueId status =
  "Cannot close issue #"
    <> T.pack (show issueId)
    <> ": uncommitted changes in this worktree:\n"
    <> formatGitStatus status
    <> "\nCommit, discard, or use `discard_worker_output` first."

formatGitStatus :: Git.GetStatusResponse -> Text
formatGitStatus status =
  T.unlines $
    map (("staged: " <>) . TL.toStrict) (V.toList (Git.getStatusResponseStagedFiles status))
      <> map (("dirty: " <>) . TL.toStrict) (V.toList (Git.getStatusResponseDirtyFiles status))

data ChainlinkIssueClose

instance MCPTool ChainlinkIssueClose where
  type ToolArgs ChainlinkIssueClose = ChainlinkIssueCloseArgs
  toolName = "chainlink_issue_close"
  toolDescription = chainlinkIssueCloseDescription
  toolSchema = chainlinkIssueCloseSchema
  toolHandlerEff args = do
    result <- chainlinkIssueCloseCore args
    case result of
      Left err -> pure $ errorResult err
      Right _ -> pure $ successResult (object ["success" .= True])

--------------------------------------------------------------------------------
-- Subissue Close
--------------------------------------------------------------------------------

chainlinkSubissueCloseDescription :: Text
chainlinkSubissueCloseDescription =
  "Coordinator-only close tool for child subissues. A dev leaf uses this only after reviewing a worker's ended session and accepting the child work."

chainlinkSubissueCloseSchema :: Aeson.Object
chainlinkSubissueCloseSchema = chainlinkIssueCloseSchema

data ChainlinkSubissueClose

instance MCPTool ChainlinkSubissueClose where
  type ToolArgs ChainlinkSubissueClose = ChainlinkIssueCloseArgs
  toolName = "chainlink_subissue_close"
  toolDescription = chainlinkSubissueCloseDescription
  toolSchema = chainlinkSubissueCloseSchema
  toolHandlerEff args = do
    result <- chainlinkIssueCloseCore args
    case result of
      Left err -> pure $ errorResult err
      Right _ -> pure $ successResult (object ["success" .= True])

--------------------------------------------------------------------------------
-- Timer
--------------------------------------------------------------------------------

instance FromJSON ChainlinkTimerStartArgs where
  parseJSON = withObject "ChainlinkTimerStartArgs" $ \v ->
    ChainlinkTimerStartArgs <$> v .: "issue_id"

instance ToJSON ChainlinkTimerStartArgs where
  toJSON args = object ["issue_id" .= ctsIssueId args]

instance FromJSON ChainlinkTimerStopArgs where
  parseJSON = withObject "ChainlinkTimerStopArgs" $ \v ->
    ChainlinkTimerStopArgs <$> v .: "issue_id"

instance ToJSON ChainlinkTimerStopArgs where
  toJSON args = object ["issue_id" .= ctstopIssueId args]

instance FromJSON ChainlinkTimerStatusArgs where
  parseJSON = withObject "ChainlinkTimerStatusArgs" $ \v ->
    ChainlinkTimerStatusArgs <$> v .:? "issue_id"

instance ToJSON ChainlinkTimerStatusArgs where
  toJSON args = object ["issue_id" .= ctstatusIssueId args]

chainlinkTimerStartDescription :: Text
chainlinkTimerStartDescription =
  "TL/SubTL-only timer start. Starts Chainlink timing for a coordinator-owned task lifecycle."

chainlinkTimerStartSchema :: Aeson.Object
chainlinkTimerStartSchema =
  genericToolSchemaWith @ChainlinkTimerStartArgs
    [("issue_id", "The numeric issue ID whose coordinator-owned lifecycle timer should start")]

chainlinkTimerStopDescription :: Text
chainlinkTimerStopDescription =
  "TL/SubTL-only timer stop. Stops the Chainlink timer for a specific coordinator-owned issue after validation and merge."

chainlinkTimerStopSchema :: Aeson.Object
chainlinkTimerStopSchema =
  genericToolSchemaWith @ChainlinkTimerStopArgs
    [("issue_id", "The numeric issue ID whose active timer should stop")]

chainlinkTimerStatusDescription :: Text
chainlinkTimerStatusDescription =
  "TL/SubTL-only timer status. Shows active Chainlink timers, or one issue's timer when issue_id is provided."

chainlinkTimerStatusSchema :: Aeson.Object
chainlinkTimerStatusSchema =
  genericToolSchemaWith @ChainlinkTimerStatusArgs
    [("issue_id", "Optional numeric issue ID whose active timer should be shown")]

runChainlinkText :: Text -> [String] -> Eff Effects (Either Text Text)
runChainlinkText label args = do
  result <- runChainlink args
  case result of
    Left err -> pure $ Left (label <> " failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 -> pure $ Right (TL.toStrict (Proc.runResponseStdout resp))
      | otherwise ->
          pure $
            Left $
              label
                <> " failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

chainlinkTimerStartCore :: ChainlinkTimerStartArgs -> Eff Effects (Either Text Text)
chainlinkTimerStartCore args = runChainlinkText "chainlink timer start" (buildTimerStartArgs args)

chainlinkTimerStopCore :: ChainlinkTimerStopArgs -> Eff Effects (Either Text Text)
chainlinkTimerStopCore args = runChainlinkText "chainlink timer stop" (buildTimerStopArgs args)

chainlinkTimerStatusCore :: ChainlinkTimerStatusArgs -> Eff Effects (Either Text Text)
chainlinkTimerStatusCore args = runChainlinkText "chainlink timer status" (buildTimerStatusArgs args)

data ChainlinkTimerStart

instance MCPTool ChainlinkTimerStart where
  type ToolArgs ChainlinkTimerStart = ChainlinkTimerStartArgs
  toolName = "chainlink_timer_start"
  toolDescription = chainlinkTimerStartDescription
  toolSchema = chainlinkTimerStartSchema
  toolHandlerEff args = do
    result <- chainlinkTimerStartCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult (object ["output" .= output])

data ChainlinkTimerStop

instance MCPTool ChainlinkTimerStop where
  type ToolArgs ChainlinkTimerStop = ChainlinkTimerStopArgs
  toolName = "chainlink_timer_stop"
  toolDescription = chainlinkTimerStopDescription
  toolSchema = chainlinkTimerStopSchema
  toolHandlerEff args = do
    result <- chainlinkTimerStopCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult (object ["output" .= output])

data ChainlinkTimerStatus

instance MCPTool ChainlinkTimerStatus where
  type ToolArgs ChainlinkTimerStatus = ChainlinkTimerStatusArgs
  toolName = "chainlink_timer_status"
  toolDescription = chainlinkTimerStatusDescription
  toolSchema = chainlinkTimerStatusSchema
  toolHandlerEff args = do
    result <- chainlinkTimerStatusCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult (object ["output" .= output])

--------------------------------------------------------------------------------
-- Issue List
--------------------------------------------------------------------------------

instance FromJSON ChainlinkIssueListArgs where
  parseJSON = withObject "ChainlinkIssueListArgs" $ \v ->
    ChainlinkIssueListArgs
      <$> v .:? "status"
      <*> v .:? "priority"
      <*> v .:? "labels"
      <*> v .:? "milestone"

instance ToJSON ChainlinkIssueListArgs where
  toJSON args =
    object
      [ "status" .= cilStatus args,
        "priority" .= cilPriority args,
        "labels" .= cilLabels args,
        "milestone" .= cilMilestone args
      ]

chainlinkIssueListDescription :: Text
chainlinkIssueListDescription =
  "List chainlink issues with optional filters. Returns issues matching the given status, priority, labels, or milestone."

chainlinkIssueListSchema :: Aeson.Object
chainlinkIssueListSchema =
  genericToolSchemaWith @ChainlinkIssueListArgs
    [ ("status", "Optional status filter: open, closed, in_progress, blocked"),
      ("priority", "Optional priority filter: low, medium, high, critical"),
      ("labels", "Optional list of labels to filter by"),
      ("milestone", "Optional milestone name to filter by")
    ]

chainlinkIssueListCore :: ChainlinkIssueListArgs -> Eff Effects (Either Text [ChainlinkIssueListItem])
chainlinkIssueListCore args = do
  let cmdArgs = buildListArgs args
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink list failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          case Aeson.eitherDecodeStrict (encodeUtf8 (TL.toStrict (Proc.runResponseStdout resp))) of
            Right items -> pure $ Right items
            Left parseErr ->
              pure $ Left ("could not parse issue list output: " <> T.pack parseErr)
      | otherwise ->
          pure $
            Left $
              "chainlink list failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkIssueList

instance MCPTool ChainlinkIssueList where
  type ToolArgs ChainlinkIssueList = ChainlinkIssueListArgs
  toolName = "chainlink_issue_list"
  toolDescription = chainlinkIssueListDescription
  toolSchema = chainlinkIssueListSchema
  toolHandlerEff args = do
    result <- chainlinkIssueListCore args
    case result of
      Left err -> pure $ errorResult err
      Right items -> pure $ successResult (Aeson.toJSON items)

--------------------------------------------------------------------------------
-- Issue Update
--------------------------------------------------------------------------------

instance FromJSON ChainlinkIssueUpdateArgs where
  parseJSON = withObject "ChainlinkIssueUpdateArgs" $ \v ->
    ChainlinkIssueUpdateArgs
      <$> v .: "issue_id"
      <*> v .:? "status"
      <*> v .:? "priority"
      <*> v .:? "labels"
      <*> v .:? "milestone"

instance ToJSON ChainlinkIssueUpdateArgs where
  toJSON args =
    object
      [ "issue_id" .= ciuIssueId args,
        "status" .= ciuStatus args,
        "priority" .= ciuPriority args,
        "labels" .= ciuLabels args,
        "milestone" .= ciuMilestone args
      ]

chainlinkIssueUpdateDescription :: Text
chainlinkIssueUpdateDescription =
  "Update a chainlink issue using the current chainlink CLI. status=in_progress marks active work; status=open reopens; status=closed closes; priority, labels, and milestone use their dedicated commands."

chainlinkIssueUpdateSchema :: Aeson.Object
chainlinkIssueUpdateSchema =
  genericToolSchemaWith @ChainlinkIssueUpdateArgs
    [ ("issue_id", "The numeric ID of the issue to update"),
      ("status", "Optional status action: in_progress marks current work, open reopens, closed closes"),
      ("priority", "Optional new priority: low, medium, high, critical"),
      ("labels", "Optional labels to add"),
      ("milestone", "Optional milestone ID to assign")
    ]

chainlinkIssueUpdateCore :: ChainlinkIssueUpdateArgs -> Eff Effects (Either Text ())
chainlinkIssueUpdateCore args =
  case ciuStatus args of
    Just status
      | not (isSupportedIssueUpdateStatus status) ->
          pure $ Left ("unsupported chainlink issue status for update: " <> status)
    _ -> runChainlinkUpdateCommands commands
  where
    builtCommands = buildUpdateCommands args
    commands =
      if null builtCommands
        then [buildUpdateArgs args]
        else builtCommands

runChainlinkUpdateCommands :: [[String]] -> Eff Effects (Either Text ())
runChainlinkUpdateCommands [] = pure $ Right ()
runChainlinkUpdateCommands (cmdArgs : rest) = do
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink update failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 -> runChainlinkUpdateCommands rest
      | otherwise ->
          pure $
            Left $
              "chainlink update failed running `chainlink "
                <> T.pack (unwords cmdArgs)
                <> "` (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkIssueUpdate

instance MCPTool ChainlinkIssueUpdate where
  type ToolArgs ChainlinkIssueUpdate = ChainlinkIssueUpdateArgs
  toolName = "chainlink_issue_update"
  toolDescription = chainlinkIssueUpdateDescription
  toolSchema = chainlinkIssueUpdateSchema
  toolHandlerEff args = do
    result <- chainlinkIssueUpdateCore args
    case result of
      Left err -> pure $ errorResult err
      Right _ -> pure $ successResult (object ["success" .= True])

--------------------------------------------------------------------------------
-- Block
--------------------------------------------------------------------------------

instance FromJSON ChainlinkBlockArgs where
  parseJSON = withObject "ChainlinkBlockArgs" $ \v ->
    ChainlinkBlockArgs
      <$> v .: "child_id"
      <*> v .: "blocker_id"

instance ToJSON ChainlinkBlockArgs where
  toJSON args =
    object
      [ "child_id" .= cbChildId args,
        "blocker_id" .= cbBlockerId args
      ]

chainlinkBlockDescription :: Text
chainlinkBlockDescription =
  "Mark one issue as blocked by another. The child issue cannot proceed until the blocker issue is resolved."

chainlinkBlockSchema :: Aeson.Object
chainlinkBlockSchema =
  genericToolSchemaWith @ChainlinkBlockArgs
    [ ("child_id", "The numeric ID of the issue that is blocked"),
      ("blocker_id", "The numeric ID of the issue that is blocking progress")
    ]

chainlinkBlockCore :: ChainlinkBlockArgs -> Eff Effects (Either Text ())
chainlinkBlockCore args = do
  let cmdArgs = buildBlockArgs args
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink block failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          pure $ Right ()
      | otherwise ->
          pure $
            Left $
              "chainlink block failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkBlock

instance MCPTool ChainlinkBlock where
  type ToolArgs ChainlinkBlock = ChainlinkBlockArgs
  toolName = "chainlink_issue_block"
  toolDescription = chainlinkBlockDescription
  toolSchema = chainlinkBlockSchema
  toolHandlerEff args = do
    result <- chainlinkBlockCore args
    case result of
      Left err -> pure $ errorResult err
      Right _ -> pure $ successResult (object ["success" .= True])

--------------------------------------------------------------------------------
-- Relate
--------------------------------------------------------------------------------

instance FromJSON ChainlinkRelateArgs where
  parseJSON = withObject "ChainlinkRelateArgs" $ \v ->
    ChainlinkRelateArgs
      <$> v .: "issue1"
      <*> v .: "issue2"

instance ToJSON ChainlinkRelateArgs where
  toJSON args =
    object
      [ "issue1" .= crIssue1 args,
        "issue2" .= crIssue2 args
      ]

chainlinkRelateDescription :: Text
chainlinkRelateDescription =
  "Relate two issues. Chainlink CLI does not support a positional relation type."

chainlinkRelateSchema :: Aeson.Object
chainlinkRelateSchema =
  genericToolSchemaWith @ChainlinkRelateArgs
    [ ("issue1", "The numeric ID of the first issue"),
      ("issue2", "The numeric ID of the second issue")
    ]

chainlinkRelateCore :: ChainlinkRelateArgs -> Eff Effects (Either Text ())
chainlinkRelateCore args = do
  let cmdArgs = buildRelateArgs args
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink relate failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          pure $ Right ()
      | otherwise ->
          pure $
            Left $
              "chainlink relate failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkRelate

instance MCPTool ChainlinkRelate where
  type ToolArgs ChainlinkRelate = ChainlinkRelateArgs
  toolName = "chainlink_issue_relate"
  toolDescription = chainlinkRelateDescription
  toolSchema = chainlinkRelateSchema
  toolHandlerEff args = do
    result <- chainlinkRelateCore args
    case result of
      Left err -> pure $ errorResult err
      Right _ -> pure $ successResult (object ["success" .= True])

--------------------------------------------------------------------------------
-- Cascade
--------------------------------------------------------------------------------

instance FromJSON ChainlinkCascadeArgs where
  parseJSON = withObject "ChainlinkCascadeArgs" $ \v ->
    ChainlinkCascadeArgs <$> v .: "issue_id"

instance ToJSON ChainlinkCascadeArgs where
  toJSON args =
    object
      [ "issue_id" .= ccIssueId args
      ]

chainlinkCascadeDescription :: Text
chainlinkCascadeDescription =
  "Show the falsification cascade for an issue — what downstream work would be affected if this issue's assumptions are wrong."

chainlinkCascadeSchema :: Aeson.Object
chainlinkCascadeSchema =
  genericToolSchemaWith @ChainlinkCascadeArgs
    [ ("issue_id", "The numeric ID of the issue to cascade from")
    ]

chainlinkCascadeCore :: ChainlinkCascadeArgs -> Eff Effects (Either Text Text)
chainlinkCascadeCore args = do
  let cmdArgs = buildCascadeArgs args
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink cascade failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          pure $ Right (TL.toStrict (Proc.runResponseStdout resp))
      | otherwise ->
          pure $
            Left $
              "chainlink cascade failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkCascade

instance MCPTool ChainlinkCascade where
  type ToolArgs ChainlinkCascade = ChainlinkCascadeArgs
  toolName = "chainlink_issue_cascade"
  toolDescription = chainlinkCascadeDescription
  toolSchema = chainlinkCascadeSchema
  toolHandlerEff args = do
    result <- chainlinkCascadeCore args
    case result of
      Left err -> pure $ errorResult err
      Right text -> pure $ successResult (object ["cascade" .= text])

--------------------------------------------------------------------------------
-- Milestone Create
--------------------------------------------------------------------------------

instance FromJSON ChainlinkMilestoneCreateArgs where
  parseJSON = withObject "ChainlinkMilestoneCreateArgs" $ \v ->
    ChainlinkMilestoneCreateArgs
      <$> v .: "title"
      <*> v .:? "description"

instance ToJSON ChainlinkMilestoneCreateArgs where
  toJSON args =
    object
      [ "title" .= cmcTitle args,
        "description" .= cmcDescription args
      ]

chainlinkMilestoneCreateDescription :: Text
chainlinkMilestoneCreateDescription =
  "Create a new milestone for grouping issues. Returns the created milestone ID."

chainlinkMilestoneCreateSchema :: Aeson.Object
chainlinkMilestoneCreateSchema =
  genericToolSchemaWith @ChainlinkMilestoneCreateArgs
    [ ("title", "Milestone title (e.g. M1, M2, Alpha, Beta)"),
      ("description", "Optional description of the milestone's goals")
    ]

chainlinkMilestoneCreateCore :: ChainlinkMilestoneCreateArgs -> Eff Effects (Either Text ChainlinkMilestoneCreateOutput)
chainlinkMilestoneCreateCore args = do
  let cmdArgs = buildMilestoneCreateArgs args
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink milestone create failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          case Aeson.eitherDecodeStrict (encodeUtf8 (TL.toStrict (Proc.runResponseStdout resp))) of
            Right output -> pure $ Right output
            Left parseErr ->
              pure $ Left ("could not parse milestone create output: " <> T.pack parseErr)
      | otherwise ->
          pure $
            Left $
              "chainlink milestone create failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkMilestoneCreate

instance MCPTool ChainlinkMilestoneCreate where
  type ToolArgs ChainlinkMilestoneCreate = ChainlinkMilestoneCreateArgs
  toolName = "chainlink_milestone_create"
  toolDescription = chainlinkMilestoneCreateDescription
  toolSchema = chainlinkMilestoneCreateSchema
  toolHandlerEff args = do
    result <- chainlinkMilestoneCreateCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult (Aeson.toJSON output)

--------------------------------------------------------------------------------
-- Milestone List
--------------------------------------------------------------------------------

chainlinkMilestoneListDescription :: Text
chainlinkMilestoneListDescription =
  "List all milestones with their IDs, titles, and descriptions."

chainlinkMilestoneListSchema :: Aeson.Object
chainlinkMilestoneListSchema = mempty

chainlinkMilestoneListCore :: Eff Effects (Either Text [ChainlinkMilestoneListItem])
chainlinkMilestoneListCore = do
  let cmdArgs = buildMilestoneListArgs
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink milestone list failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          case Aeson.eitherDecodeStrict (encodeUtf8 (TL.toStrict (Proc.runResponseStdout resp))) of
            Right items -> pure $ Right items
            Left parseErr ->
              pure $ Left ("could not parse milestone list output: " <> T.pack parseErr)
      | otherwise ->
          pure $
            Left $
              "chainlink milestone list failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkMilestoneList

instance MCPTool ChainlinkMilestoneList where
  type ToolArgs ChainlinkMilestoneList = ()
  toolName = "chainlink_milestone_list"
  toolDescription = chainlinkMilestoneListDescription
  toolSchema = chainlinkMilestoneListSchema
  toolHandlerEff _ = do
    result <- chainlinkMilestoneListCore
    case result of
      Left err -> pure $ errorResult err
      Right items -> pure $ successResult (Aeson.toJSON items)
