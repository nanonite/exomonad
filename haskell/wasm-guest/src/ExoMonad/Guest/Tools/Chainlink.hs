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

    -- * Sync
    ChainlinkSync (..),
    chainlinkSyncCore,
    chainlinkSyncDescription,
    chainlinkSyncSchema,

    -- * Worker Status
    ChainlinkWorkerStatus (..),
    chainlinkWorkerStatusCore,
    chainlinkWorkerStatusDescription,
    chainlinkWorkerStatusSchema,
  )
  where

import Control.Monad (void)
import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), ToJSON (..), Value, object, withObject, (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Int (Int32)
import Data.Map qualified as Map
import Data.Maybe (fromMaybe)
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Encoding (encodeUtf8)
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Data.Word (Word64)
import Effects.Process qualified as Proc
import ExoMonad.Chainlink.Pure
import Effects.Events qualified as Events (NotifyParentRequest (..))
import ExoMonad.Effects.Events (EventsNotifyParent)
import ExoMonad.Effects.Process (ProcessRun)
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
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

instance ToJSON ChainlinkIssueCloseArgs where
  toJSON args =
    object
      [ "issue_id" .= cisIssueId args,
        "summary" .= cisSummary args
      ]

chainlinkIssueCloseDescription :: Text
chainlinkIssueCloseDescription =
  "Atomic close sequence: release locks, close the issue, end session, and notify parent. "
    <> "Use this when a task is completed. The issue will be marked as closed and added to the changelog. "
    <> "This is the ONLY close tool you should use — never use raw `chainlink close` from the CLI."

chainlinkIssueCloseSchema :: Aeson.Object
chainlinkIssueCloseSchema =
  genericToolSchemaWith @ChainlinkIssueCloseArgs
    [ ("issue_id", "The numeric ID of the issue to close"),
      ("summary", "Optional summary of what was completed. Defaults to \"Closed #<id>\" if omitted.")
    ]

chainlinkIssueCloseCore :: ChainlinkIssueCloseArgs -> Eff Effects (Either Text ())
chainlinkIssueCloseCore args = do
  let issueId = cisIssueId args
      summary = fromMaybe ("Closed #" <> T.pack (show issueId)) (cisSummary args)
  -- Step 1: release locks (idempotent — no-op if none held)
  step1 <- runChainlink (buildLocksReleaseArgs args)
  case step1 of
    Left err -> pure $ Left ("locks release failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp /= 0 ->
          pure $
            Left $
              "locks release failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)
      | otherwise -> do
          -- Step 2: close the issue
          step2 <- runChainlink (buildCloseArgs args)
          case step2 of
            Left err -> pure $ Left ("chainlink close failed: " <> err)
            Right resp2
              | Proc.runResponseExitCode resp2 /= 0 ->
                  pure $
                    Left $
                      "chainlink close failed (exit "
                        <> exitCodeToText (Proc.runResponseExitCode resp2)
                        <> "): "
                        <> TL.toStrict (Proc.runResponseStderr resp2)
              | otherwise -> do
                  -- Step 3: end session
                  let sessionArgs = ChainlinkSessionEndArgs (Just summary)
                  step3 <- runChainlink (buildSessionEndArgs sessionArgs)
                  case step3 of
                    Left err -> pure $ Left ("session end failed: " <> err)
                    Right resp3
                      | Proc.runResponseExitCode resp3 /= 0 ->
                          pure $
                            Left $
                              "session end failed (exit "
                                <> exitCodeToText (Proc.runResponseExitCode resp3)
                                <> "): "
                                <> TL.toStrict (Proc.runResponseStderr resp3)
                      | otherwise -> do
                           -- Step 4: notify parent (non-fatal — parent may not exist, e.g. root TL)
                           let statusText = "success" :: Text
                               message = "Closed #" <> T.pack (show issueId) <> ": " <> summary
                               notifyPayload =
                                 Events.NotifyParentRequest
                                   { Events.notifyParentRequestAgentId = "",
                                     Events.notifyParentRequestStatus = TL.fromStrict statusText,
                                     Events.notifyParentRequestMessage = TL.fromStrict message,
                                     Events.notifyParentRequestOverrideRecipient = Nothing
                                   }
                           void $ suspendEffect @EventsNotifyParent notifyPayload
                           pure $ Right ()

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
  "Update a chainlink issue's status, priority, labels, or milestone. Use this to track progress, mark blockers, or change priority."

chainlinkIssueUpdateSchema :: Aeson.Object
chainlinkIssueUpdateSchema =
  genericToolSchemaWith @ChainlinkIssueUpdateArgs
    [ ("issue_id", "The numeric ID of the issue to update"),
      ("status", "Optional new status: open, in_progress, blocked, closed"),
      ("priority", "Optional new priority: low, medium, high, critical"),
      ("labels", "Optional new list of labels to set"),
      ("milestone", "Optional milestone name to assign")
    ]

chainlinkIssueUpdateCore :: ChainlinkIssueUpdateArgs -> Eff Effects (Either Text ())
chainlinkIssueUpdateCore args = do
  let cmdArgs = buildUpdateArgs args
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink update failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          pure $ Right ()
      | otherwise ->
          pure $
            Left $
              "chainlink update failed (exit "
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
      <*> v .: "relation"

instance ToJSON ChainlinkRelateArgs where
  toJSON args =
    object
      [ "issue1" .= crIssue1 args,
        "issue2" .= crIssue2 args,
        "relation" .= crRelation args
      ]

chainlinkRelateDescription :: Text
chainlinkRelateDescription =
  "Relate two issues with a specified relationship type (e.g. duplicates, relates_to, blocks, is_blocked_by)."

chainlinkRelateSchema :: Aeson.Object
chainlinkRelateSchema =
  genericToolSchemaWith @ChainlinkRelateArgs
    [ ("issue1", "The numeric ID of the first issue"),
      ("issue2", "The numeric ID of the second issue"),
      ("relation", "The relationship type: duplicates, relates_to, blocks, is_blocked_by")
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

--------------------------------------------------------------------------------
-- Sync
--------------------------------------------------------------------------------

chainlinkSyncDescription :: Text
chainlinkSyncDescription =
  "Sync chainlink lock state and coordination status. Use this to ensure all agents have a consistent view of locks and dependencies."

chainlinkSyncSchema :: Aeson.Object
chainlinkSyncSchema = mempty

chainlinkSyncCore :: Eff Effects (Either Text Text)
chainlinkSyncCore = do
  let cmdArgs = buildSyncArgs
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink sync failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          pure $ Right (TL.toStrict (Proc.runResponseStdout resp))
      | otherwise ->
          pure $
            Left $
              "chainlink sync failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkSync

instance MCPTool ChainlinkSync where
  type ToolArgs ChainlinkSync = ()
  toolName = "chainlink_sync"
  toolDescription = chainlinkSyncDescription
  toolSchema = chainlinkSyncSchema
  toolHandlerEff _ = do
    result <- chainlinkSyncCore
    case result of
      Left err -> pure $ errorResult err
      Right text -> pure $ successResult (object ["output" .= text])

--------------------------------------------------------------------------------
-- Worker Status
--------------------------------------------------------------------------------

chainlinkWorkerStatusDescription :: Text
chainlinkWorkerStatusDescription =
  "Aggregate worker status: lists open issues, active locks, usage data, and uncommitted files. "
    <> "Runs (1) chainlink issue list --status open, (2) chainlink locks list, "
    <> "(3) chainlink usage list, (4) git diff --stat and correlates by issue_id. "
    <> "Returns a JSON array of worker summaries."

chainlinkWorkerStatusSchema :: Aeson.Object
chainlinkWorkerStatusSchema = mempty

chainlinkWorkerStatusCore :: Eff Effects (Either Text [WorkerStatusEntry])
chainlinkWorkerStatusCore = do
  -- Run all four commands, each resilient to failure
  issues <- tryCommand buildWorkerStatusIssueListArgs parseIssueListJson []
  locks <- tryCommand buildWorkerStatusLocksListArgs parseLocksListJson []
  usage <- tryCommand buildWorkerStatusUsageArgs parseUsageListJson []
  diffStat <- tryGitDiffStat
  -- Correlate and return
  let uncommittedFiles = parseGitDiffStat diffStat
  pure $ Right (correlateWorkerStatus issues locks usage uncommittedFiles)
  where
    parseIssueListJson txt =
      case Aeson.eitherDecodeStrict (encodeUtf8 txt) of
        Right items -> items
        Left _ -> []
    parseLocksListJson txt =
      case Aeson.eitherDecodeStrict (encodeUtf8 txt) of
        Right items -> items
        Left _ -> []
    parseUsageListJson txt =
      case Aeson.eitherDecodeStrict (encodeUtf8 txt) of
        Right items -> items
        Left _ -> []

tryCommand :: [String] -> (Text -> a) -> a -> Eff Effects a
tryCommand args parseFn defaultVal = do
  result <- runChainlink args
  case result of
    Left _ -> pure defaultVal
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          pure $ parseFn (TL.toStrict (Proc.runResponseStdout resp))
      | otherwise -> pure defaultVal

tryGitDiffStat :: Eff Effects Text
tryGitDiffStat = do
  result <-
    suspendEffect @ProcessRun
      ( Proc.RunRequest
          { Proc.runRequestCommand = "git",
            Proc.runRequestArgs = V.fromList (TL.pack <$> ["diff", "--stat"]),
            Proc.runRequestWorkingDir = ".",
            Proc.runRequestEnv = Map.empty,
            Proc.runRequestTimeoutMs = 15000
          }
      )
  case result of
    Left _ -> pure ""
    Right resp
      | Proc.runResponseExitCode resp /= 0 -> pure ""
      | otherwise -> pure $ TL.toStrict (Proc.runResponseStdout resp)

data ChainlinkWorkerStatus

instance MCPTool ChainlinkWorkerStatus where
  type ToolArgs ChainlinkWorkerStatus = ()
  toolName = "chainlink_worker_status"
  toolDescription = chainlinkWorkerStatusDescription
  toolSchema = chainlinkWorkerStatusSchema
  toolHandlerEff _ = do
    result <- chainlinkWorkerStatusCore
    case result of
      Left err -> pure $ errorResult err
      Right entries -> pure $ successResult (Aeson.toJSON entries)
