{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}
{-# LANGUAGE TypeOperators #-}

-- | Dev role config: PR and notify tools with state transitions and stop hook checks.
module DevRole (config, Tools) where

import Control.Monad (void)
import Data.Aeson (object, (.=))
import Data.Aeson qualified as Aeson
import ExoMonad
import ExoMonad.Guest.Tools.Chainlink
  ( ChainlinkSessionStart (..),
    ChainlinkSessionStatus (..),
    ChainlinkIssueShow (..),
    ChainlinkIssueComment (..),
    ChainlinkSubissueCreate (..),
    ChainlinkSessionWork (..),
    ChainlinkSessionEnd (..),
    ChainlinkSubissueClose (..)
  )
import ExoMonad.Guest.Tools.FilePR (filePRCore, filePRDescription, filePRSchema, FilePRArgs, FilePROutput (..))
import ExoMonad.Guest.Tools.Events
  ( notifyParentCore, notifyParentDescription, notifyParentSchema, NotifyParentArgs (..), NotifyStatus (..)
  )
import ExoMonad.Guest.Tools.Tasks
  ( taskListCore, taskListDescription, taskListSchema, TaskListArgs,
    taskGetCore, taskGetDescription, taskGetSchema, TaskGetArgs,
    taskUpdateCore, taskUpdateDescription, taskUpdateSchema, TaskUpdateArgs
  )
import ExoMonad.Guest.StateMachine (applyEvent)
import ExoMonad.Guest.Effects.StopHook (getCurrentBranch)
import DevPhase (DevPhase (..), DevEvent (..))
import HttpDevHooks (httpDevHooks)
import PRReviewHandler (prReviewEventHandlers)

-- | Dev-specific file_pr: files PR, then transitions DevPhase.
data DevFilePR

instance MCPTool DevFilePR where
  type ToolArgs DevFilePR = FilePRArgs
  toolName = "file_pr"
  toolDescription = filePRDescription
  toolSchema = filePRSchema
  toolHandlerEff args = do
    result <- filePRCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> do
        branch <- getCurrentBranch
        void $ applyEvent @DevPhase @DevEvent branch DevSpawned
          (PRCreated (fpoNumber output) (fpoUrl output) (fpoHeadBranch output))
        pure $ successResult (Aeson.toJSON output)

-- | Dev-specific notify_parent: notifies parent, then transitions to DevDone/DevFailed.
data DevNotifyParent

instance MCPTool DevNotifyParent where
  type ToolArgs DevNotifyParent = NotifyParentArgs
  toolName = "notify_parent"
  toolDescription = notifyParentDescription
  toolSchema = notifyParentSchema
  toolHandlerEff args = do
    result <- notifyParentCore args
    case result of
      Left err -> pure $ errorResult err
      Right _ -> do
        branch <- getCurrentBranch
        case npStatus args of
          Success -> void $ applyEvent @DevPhase @DevEvent branch DevSpawned (NotifyParentSuccess (npMessage args))
          Failure -> void $ applyEvent @DevPhase @DevEvent branch DevSpawned (NotifyParentFailure (npMessage args))
        pure $ successResult $ object ["success" .= True]

data DevTaskList

instance MCPTool DevTaskList where
  type ToolArgs DevTaskList = TaskListArgs
  toolName = "task_list"
  toolDescription = taskListDescription
  toolSchema = taskListSchema
  toolHandlerEff args = do
    result <- taskListCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult output

data DevTaskGet

instance MCPTool DevTaskGet where
  type ToolArgs DevTaskGet = TaskGetArgs
  toolName = "task_get"
  toolDescription = taskGetDescription
  toolSchema = taskGetSchema
  toolHandlerEff args = do
    result <- taskGetCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult output

data DevTaskUpdate

instance MCPTool DevTaskUpdate where
  type ToolArgs DevTaskUpdate = TaskUpdateArgs
  toolName = "task_update"
  toolDescription = taskUpdateDescription
  toolSchema = taskUpdateSchema
  toolHandlerEff args = do
    result <- taskUpdateCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult output

data Tools mode = Tools
  { pr :: mode :- DevFilePR,
    notifyParent :: mode :- DevNotifyParent,
    sendTmuxMessage :: mode :- SendTmuxMessage,
    sendMailboxMessage :: mode :- SendMailboxMessage,
    taskList :: mode :- DevTaskList,
    taskGet :: mode :- DevTaskGet,
    taskUpdate :: mode :- DevTaskUpdate,
    chainlinkSessionStart :: mode :- ChainlinkSessionStart,
    chainlinkSessionStatus :: mode :- ChainlinkSessionStatus,
    chainlinkIssueShow :: mode :- ChainlinkIssueShow,
    chainlinkIssueComment :: mode :- ChainlinkIssueComment,
    chainlinkSubissueCreate :: mode :- ChainlinkSubissueCreate,
    chainlinkSessionWork :: mode :- ChainlinkSessionWork,
    chainlinkSessionEnd :: mode :- ChainlinkSessionEnd,
    chainlinkSubissueClose :: mode :- ChainlinkSubissueClose
  }
  deriving (Generic)

config :: RoleConfig (Tools AsHandler)
config =
  RoleConfig
    { roleName = "dev",
      tools =
        Tools
          { pr = mkHandler @DevFilePR,
            notifyParent = mkHandler @DevNotifyParent,
            sendTmuxMessage = mkHandler @SendTmuxMessage,
            sendMailboxMessage = mkHandler @SendMailboxMessage,
            taskList = mkHandler @DevTaskList,
            taskGet = mkHandler @DevTaskGet,
            taskUpdate = mkHandler @DevTaskUpdate,
            chainlinkSessionStart = mkHandler @ChainlinkSessionStart,
            chainlinkSessionStatus = mkHandler @ChainlinkSessionStatus,
            chainlinkIssueShow = mkHandler @ChainlinkIssueShow,
            chainlinkIssueComment = mkHandler @ChainlinkIssueComment,
            chainlinkSubissueCreate = mkHandler @ChainlinkSubissueCreate,
            chainlinkSessionWork = mkHandler @ChainlinkSessionWork,
            chainlinkSessionEnd = mkHandler @ChainlinkSessionEnd,
            chainlinkSubissueClose = mkHandler @ChainlinkSubissueClose
          },
      hooks = httpDevHooks,
      eventHandlers = prReviewEventHandlers
    }
