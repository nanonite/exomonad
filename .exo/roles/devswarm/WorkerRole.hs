{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}
{-# LANGUAGE TypeOperators #-}

-- | Worker role config: notify_parent + task tools + chainlink tools, allow-all hooks, no state transitions.
module WorkerRole (config, Tools) where

import Data.Aeson (object, (.=))
import ExoMonad
import ExoMonad.Guest.Tools.Chainlink
  ( ChainlinkIssueShow (..),
    ChainlinkIssueComment (..),
    ChainlinkSubissueCreate (..),
    ChainlinkSessionWork (..),
    ChainlinkSessionEnd (..),
    ChainlinkIssueClose (..)
  )
import ExoMonad.Guest.Tools.Events
  ( notifyParentCore, notifyParentDescription, notifyParentSchema, NotifyParentArgs
  )
import ExoMonad.Guest.Tools.Tasks
  ( taskListCore, taskListDescription, taskListSchema, TaskListArgs,
    taskGetCore, taskGetDescription, taskGetSchema, TaskGetArgs,
    taskUpdateCore, taskUpdateDescription, taskUpdateSchema, TaskUpdateArgs
  )
import ExoMonad.Guest.Types (allowResponse, allowStopResponse, postToolUseResponse, BeforeModelOutput (..), AfterModelOutput (..))
import ExoMonad.Types (HookConfig (..), defaultSessionStartHook)
import HookPolicy (preToolUseWithGhBlock)
import WorkerStopCheck (workerStopCheck)

-- | Worker notify_parent: thin wrapper, no phase transitions.
data WorkerNotifyParent

instance MCPTool WorkerNotifyParent where
  type ToolArgs WorkerNotifyParent = NotifyParentArgs
  toolName = "notify_parent"
  toolDescription = notifyParentDescription
  toolSchema = notifyParentSchema
  toolHandlerEff args = do
    result <- notifyParentCore args
    case result of
      Left err -> pure $ errorResult err
      Right _ -> pure $ successResult $ object ["success" .= True]

data WorkerTaskList

instance MCPTool WorkerTaskList where
  type ToolArgs WorkerTaskList = TaskListArgs
  toolName = "task_list"
  toolDescription = taskListDescription
  toolSchema = taskListSchema
  toolHandlerEff args = do
    result <- taskListCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult output

data WorkerTaskGet

instance MCPTool WorkerTaskGet where
  type ToolArgs WorkerTaskGet = TaskGetArgs
  toolName = "task_get"
  toolDescription = taskGetDescription
  toolSchema = taskGetSchema
  toolHandlerEff args = do
    result <- taskGetCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult output

data WorkerTaskUpdate

instance MCPTool WorkerTaskUpdate where
  type ToolArgs WorkerTaskUpdate = TaskUpdateArgs
  toolName = "task_update"
  toolDescription = taskUpdateDescription
  toolSchema = taskUpdateSchema
  toolHandlerEff args = do
    result <- taskUpdateCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult output

data Tools mode = Tools
  { notifyParent :: mode :- WorkerNotifyParent,
    sendMessage :: mode :- SendMessage,
    taskList :: mode :- WorkerTaskList,
    taskGet :: mode :- WorkerTaskGet,
    taskUpdate :: mode :- WorkerTaskUpdate,
    chainlinkIssueShow :: mode :- ChainlinkIssueShow,
    chainlinkIssueComment :: mode :- ChainlinkIssueComment,
    chainlinkSubissueCreate :: mode :- ChainlinkSubissueCreate,
    chainlinkSessionWork :: mode :- ChainlinkSessionWork,
    chainlinkSessionEnd :: mode :- ChainlinkSessionEnd,
    chainlinkIssueClose :: mode :- ChainlinkIssueClose
  }
  deriving (Generic)

config :: RoleConfig (Tools AsHandler)
config =
  RoleConfig
    { roleName = "worker",
      tools =
        Tools
          { notifyParent = mkHandler @WorkerNotifyParent,
            sendMessage = mkHandler @SendMessage,
            taskList = mkHandler @WorkerTaskList,
            taskGet = mkHandler @WorkerTaskGet,
            taskUpdate = mkHandler @WorkerTaskUpdate,
            chainlinkIssueShow = mkHandler @ChainlinkIssueShow,
            chainlinkIssueComment = mkHandler @ChainlinkIssueComment,
            chainlinkSubissueCreate = mkHandler @ChainlinkSubissueCreate,
            chainlinkSessionWork = mkHandler @ChainlinkSessionWork,
            chainlinkSessionEnd = mkHandler @ChainlinkSessionEnd,
            chainlinkIssueClose = mkHandler @ChainlinkIssueClose
          },
      hooks =
        HookConfig
          { preToolUse = preToolUseWithGhBlock (\_ -> pure (allowResponse Nothing)),
            postToolUse = \_ -> pure (postToolUseResponse Nothing),
            onStop = \_ -> workerStopCheck,
            onSubagentStop = \_ -> pure allowStopResponse,
            onSessionStart = defaultSessionStartHook,
            beforeModel = \_ -> pure (BeforeModelAllow Nothing),
            afterModel = \_ -> pure (AfterModelAllow Nothing)
          },
      eventHandlers = defaultEventHandlers
    }
