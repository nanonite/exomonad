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
  ( ChainlinkIssueComment (..),
    ChainlinkIssueShow (..),
    ChainlinkSessionEnd (..),
    ChainlinkSessionStart (..),
    ChainlinkSessionWork (..),
  )
import ExoMonad.Guest.Tools.Events
  ( NotifyParentArgs,
    notifyParentCore,
    notifyParentDescription,
    notifyParentSchema,
  )
import ExoMonad.Guest.Tools.Inbox (CheckInbox (..))
import ExoMonad.Guest.Tools.Tasks
  ( TaskGetArgs,
    TaskListArgs,
    TaskUpdateArgs,
    taskGetCore,
    taskGetDescription,
    taskGetSchema,
    taskListCore,
    taskListDescription,
    taskListSchema,
    taskUpdateCore,
    taskUpdateDescription,
    taskUpdateSchema,
  )
import ExoMonad.Guest.Types (AfterModelOutput (..), BeforeModelOutput (..), allowResponse, allowStopResponse, postToolUseResponse)
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
    sendTmuxMessage :: mode :- SendTmuxMessage,
    sendMailboxMessage :: mode :- SendMailboxMessage,
    checkInbox :: mode :- CheckInbox,
    taskList :: mode :- WorkerTaskList,
    taskGet :: mode :- WorkerTaskGet,
    taskUpdate :: mode :- WorkerTaskUpdate,
    chainlinkSessionStart :: mode :- ChainlinkSessionStart,
    chainlinkIssueShow :: mode :- ChainlinkIssueShow,
    chainlinkIssueComment :: mode :- ChainlinkIssueComment,
    chainlinkSessionWork :: mode :- ChainlinkSessionWork,
    chainlinkSessionEnd :: mode :- ChainlinkSessionEnd
  }
  deriving (Generic)

config :: RoleConfig (Tools AsHandler)
config =
  RoleConfig
    { roleName = "worker",
      tools =
        Tools
          { notifyParent = mkHandler @WorkerNotifyParent,
            sendTmuxMessage = mkHandler @SendTmuxMessage,
            sendMailboxMessage = mkHandler @SendMailboxMessage,
            checkInbox = mkHandler @CheckInbox,
            taskList = mkHandler @WorkerTaskList,
            taskGet = mkHandler @WorkerTaskGet,
            taskUpdate = mkHandler @WorkerTaskUpdate,
            chainlinkSessionStart = mkHandler @ChainlinkSessionStart,
            chainlinkIssueShow = mkHandler @ChainlinkIssueShow,
            chainlinkIssueComment = mkHandler @ChainlinkIssueComment,
            chainlinkSessionWork = mkHandler @ChainlinkSessionWork,
            chainlinkSessionEnd = mkHandler @ChainlinkSessionEnd
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
