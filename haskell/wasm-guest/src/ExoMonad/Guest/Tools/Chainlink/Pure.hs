module ExoMonad.Guest.Tools.Chainlink.Pure
  ( -- * Issue Create
    ChainlinkIssueCreateArgs (..),
    buildCreateArgs,

    -- * Session Start
    buildSessionStartArgs,

    -- * Session Status
    buildSessionStatusArgs,

    -- * Issue Show
    ChainlinkIssueShowOutput (..),
    buildShowArgs,

    -- * Issue Comment
    ChainlinkIssueCommentArgs (..),
    buildCommentArgs,
    ChainlinkIssueCommentOutput (..),

    -- * Subissue Create
    ChainlinkSubissueCreateArgs (..),
    buildSubissueArgs,

    -- * Session Work
    ChainlinkSessionWorkArgs (..),
    buildSessionWorkArgs,

    -- * Session End
    ChainlinkSessionEndArgs (..),
    buildSessionEndArgs,

    -- * Issue Close
    ChainlinkIssueCloseArgs (..),
    buildCloseArgs,

    -- * Timer
    ChainlinkTimerStartArgs (..),
    ChainlinkTimerStopArgs (..),
    ChainlinkTimerStatusArgs (..),
    buildTimerStartArgs,
    buildTimerStopArgs,
    buildTimerStatusArgs,

    -- * Issue List
    ChainlinkIssueListArgs (..),
    buildListArgs,
    ChainlinkIssueListItem (..),

    -- * Issue Update
    ChainlinkIssueUpdateArgs (..),
    buildUpdateArgs,
    buildUpdateCommands,
    isSupportedIssueUpdateStatus,

    -- * Block
    ChainlinkBlockArgs (..),
    buildBlockArgs,

    -- * Relate
    ChainlinkRelateArgs (..),
    buildRelateArgs,

    -- * Cascade
    ChainlinkCascadeArgs (..),
    buildCascadeArgs,

    -- * Milestone
    ChainlinkMilestoneCreateArgs (..),
    buildMilestoneCreateArgs,
    ChainlinkMilestoneCreateOutput (..),
    buildMilestoneListArgs,
    ChainlinkMilestoneListItem (..),

    -- * Worker Protocol Text
    chainlinkWorkerProtocolText,

    -- * Utilities
    parseIssueId,
  )
where

import Data.Aeson (FromJSON (..), ToJSON (..), object, withObject, (.!=), (.:), (.:?), (.=))
import Data.Maybe (mapMaybe)
import Data.Text (Text)
import Data.Text qualified as T
import GHC.Generics (Generic)

data ChainlinkIssueCreateArgs = ChainlinkIssueCreateArgs
  { cicaTitle :: Text,
    cicaDescription :: Maybe Text,
    cicaPriority :: Maybe Text,
    cicaLabels :: Maybe [Text]
  }
  deriving (Generic, Show)

data ChainlinkIssueShowOutput = ChainlinkIssueShowOutput
  { cisoId :: Int,
    cisoTitle :: Text,
    cisoStatus :: Text,
    cisoPriority :: Maybe Text,
    cisoLabels :: [Text]
  }
  deriving (Generic, Show, Eq)

instance FromJSON ChainlinkIssueShowOutput where
  parseJSON = withObject "ChainlinkIssueShowOutput" $ \v ->
    ChainlinkIssueShowOutput
      <$> v .: "id"
      <*> v .: "title"
      <*> v .: "status"
      <*> v .:? "priority"
      <*> v .:? "labels" .!= []

instance ToJSON ChainlinkIssueShowOutput where
  toJSON o =
    object
      [ "id" .= cisoId o,
        "title" .= cisoTitle o,
        "status" .= cisoStatus o,
        "priority" .= cisoPriority o,
        "labels" .= cisoLabels o
      ]

data ChainlinkIssueCommentArgs = ChainlinkIssueCommentArgs
  { cicIssueId :: Int,
    cicMessage :: Text
  }
  deriving (Generic, Show)

data ChainlinkIssueCommentOutput = ChainlinkIssueCommentOutput
  { cicoSuccess :: Bool
  }
  deriving (Generic, Show)

instance FromJSON ChainlinkIssueCommentOutput where
  parseJSON = withObject "ChainlinkIssueCommentOutput" $ \v ->
    ChainlinkIssueCommentOutput <$> v .: "success"

instance ToJSON ChainlinkIssueCommentOutput where
  toJSON o = object ["success" .= cicoSuccess o]

data ChainlinkSubissueCreateArgs = ChainlinkSubissueCreateArgs
  { cscParentId :: Int,
    cscTitle :: Text,
    cscPriority :: Maybe Text,
    cscLabels :: Maybe [Text]
  }
  deriving (Generic, Show)

data ChainlinkSessionWorkArgs = ChainlinkSessionWorkArgs
  { cswIssueId :: Int
  }
  deriving (Generic, Show)

data ChainlinkSessionEndArgs = ChainlinkSessionEndArgs
  { cseNotes :: Maybe Text
  }
  deriving (Generic, Show)

data ChainlinkIssueCloseArgs = ChainlinkIssueCloseArgs
  { cisIssueId :: Int,
    cisSummary :: Maybe Text,
    cisForce :: Bool
  }
  deriving (Generic, Show)

data ChainlinkTimerStartArgs = ChainlinkTimerStartArgs
  { ctsIssueId :: Int
  }
  deriving (Generic, Show)

data ChainlinkTimerStopArgs = ChainlinkTimerStopArgs
  { ctstopIssueId :: Int
  }
  deriving (Generic, Show)

data ChainlinkTimerStatusArgs = ChainlinkTimerStatusArgs
  { ctstatusIssueId :: Maybe Int
  }
  deriving (Generic, Show)

data ChainlinkIssueListArgs = ChainlinkIssueListArgs
  { cilStatus :: Maybe Text,
    cilPriority :: Maybe Text,
    cilLabels :: Maybe [Text],
    cilMilestone :: Maybe Text
  }
  deriving (Generic, Show)

data ChainlinkIssueListItem = ChainlinkIssueListItem
  { ciliId :: Int,
    ciliTitle :: Text,
    ciliStatus :: Text,
    ciliPriority :: Maybe Text,
    ciliLabels :: [Text]
  }
  deriving (Generic, Show, Eq)

instance FromJSON ChainlinkIssueListItem where
  parseJSON = withObject "ChainlinkIssueListItem" $ \v ->
    ChainlinkIssueListItem
      <$> v .: "id"
      <*> v .: "title"
      <*> v .: "status"
      <*> v .:? "priority"
      <*> v .:? "labels" .!= []

instance ToJSON ChainlinkIssueListItem where
  toJSON o =
    object
      [ "id" .= ciliId o,
        "title" .= ciliTitle o,
        "status" .= ciliStatus o,
        "priority" .= ciliPriority o,
        "labels" .= ciliLabels o
      ]

data ChainlinkIssueUpdateArgs = ChainlinkIssueUpdateArgs
  { ciuIssueId :: Int,
    ciuStatus :: Maybe Text,
    ciuPriority :: Maybe Text,
    ciuLabels :: Maybe [Text],
    ciuMilestone :: Maybe Text
  }
  deriving (Generic, Show)

data ChainlinkBlockArgs = ChainlinkBlockArgs
  { cbChildId :: Int,
    cbBlockerId :: Int
  }
  deriving (Generic, Show)

data ChainlinkRelateArgs = ChainlinkRelateArgs
  { crIssue1 :: Int,
    crIssue2 :: Int
  }
  deriving (Generic, Show)

data ChainlinkCascadeArgs = ChainlinkCascadeArgs
  { ccIssueId :: Int
  }
  deriving (Generic, Show)

data ChainlinkMilestoneCreateArgs = ChainlinkMilestoneCreateArgs
  { cmcTitle :: Text,
    cmcDescription :: Maybe Text
  }
  deriving (Generic, Show)

data ChainlinkMilestoneCreateOutput = ChainlinkMilestoneCreateOutput
  { cmcoMilestoneId :: Int
  }
  deriving (Generic, Show, Eq)

instance FromJSON ChainlinkMilestoneCreateOutput where
  parseJSON = withObject "ChainlinkMilestoneCreateOutput" $ \v ->
    ChainlinkMilestoneCreateOutput <$> v .: "id"

instance ToJSON ChainlinkMilestoneCreateOutput where
  toJSON o = object ["id" .= cmcoMilestoneId o]

data ChainlinkMilestoneListItem = ChainlinkMilestoneListItem
  { cmliId :: Int,
    cmliTitle :: Text,
    cmliDescription :: Maybe Text
  }
  deriving (Generic, Show, Eq)

instance FromJSON ChainlinkMilestoneListItem where
  parseJSON = withObject "ChainlinkMilestoneListItem" $ \v ->
    ChainlinkMilestoneListItem
      <$> v .: "id"
      <*> v .: "title"
      <*> v .:? "description"

instance ToJSON ChainlinkMilestoneListItem where
  toJSON o =
    object
      [ "id" .= cmliId o,
        "title" .= cmliTitle o,
        "description" .= cmliDescription o
      ]

-- | Chainlink worker protocol text, injected into worker prompts.
-- Defined here so it's testable natively (wasm-guest-pure builds on any arch).
chainlinkWorkerProtocolText :: Text
chainlinkWorkerProtocolText =
  T.unlines
    [ "# Chainlink Worker Protocol",
      "",
      "You are a worker enhanced with chainlink for structured task tracking and completion.",
      "",
      "Chainlink is your contract with your parent TL. Start a session, mark the assigned issue as active work, report progress, and end the session with handoff notes.",
      "",
      "## Worker Chainlink Workflow",
      "",
      "### 1. Start Your Session",
      "",
      "Immediately on spawn, start a session and mark the assigned issue as active work:",
      "",
      "```",
      "chainlink_session_start",
      "chainlink_session_work issue_id=<assigned issue id>",
      "```",
      "",
      "The issue ID should be embedded in your task description from the TL.",
      "",
      "### 2. Read the Spec",
      "",
      "Read the full issue spec before doing any work:",
      "",
      "```",
      "chainlink_issue_show issue_id=<assigned issue id>",
      "```",
      "",
      "This returns the issue title, status, priority, labels, and any supported issue metadata.",
      "",
      "### 3. Do the Work",
      "",
      "- Stay within the files listed in the issue spec.",
      "- Use `chainlink_issue_comment` to post progress updates after meaningful milestones.",
      "- If blocked, do not silently stall. Use `chainlink_issue_comment` to record the blocker and `notify_parent` with `BLOCKED: <reason>`.",
      "- If scope creep appears, notify the parent.",
      "",
      "### 4. End The Session",
      "",
      "When the work is complete, end the session with handoff notes and notify the parent:",
      "",
      "```",
      "chainlink_session_end notes=\"<what was done>\"",
      "notify_parent status=success message=\"<assigned issue id> ready for parent close\"",
      "```",
      "",
      "Do not close your assigned issue. Close authority belongs to the parent coordinator after review.",
      "",
      "## Stuck-Escalation Path",
      "",
      "If you are stuck, blocked, confused, or the spec is ambiguous:",
      "",
      "1. `chainlink_issue_comment issue_id=<id> message=\"BLOCKED: <specific reason>\"`",
      "2. `notify_parent` with `BLOCKED: <specific reason>`",
      "3. If direct coordination is required, use `send_tmux_message` to the TL.",
      "",
      "Do not implement past ambiguity. Report exactly what is unclear.",
      "",
      "## Scope-Creep Path",
      "",
      "If the task grows beyond the original issue spec:",
      "",
      "1. File a new subissue with `chainlink_subissue_create`.",
      "2. `notify_parent` with `SCOPE: Created subissue #<new-id> for <description>`.",
      "3. Continue on the original issue unless redirected.",
      "",
      "## Available MCP Tools",
      "",
      "| Tool | Purpose |",
      "|------|---------|",
      "| `chainlink_session_start` | Start a chainlink work session |",
      "| `chainlink_session_work` | Mark an issue as the active work item |",
      "| `chainlink_issue_show` | Read issue details |",
      "| `chainlink_issue_comment` | Post a progress comment on the issue |",
      "| `chainlink_session_end` | End session with optional handoff notes |",
      "| `notify_parent` | Report results or issues to parent TL |",
      "| `send_tmux_message` | Send messages to the TL when coordination is needed |",
      "",
      "## Hard Rules",
      "",
      "- Start a session before marking work active.",
      "- Never close your assigned issue; end the session with handoff notes for the parent coordinator.",
      "- Never create branches or commit unless explicitly instructed.",
      "- If blocked, report immediately.",
      "- If scope creep appears, report it; do not absorb extra work silently."
    ]

parseIssueId :: Text -> Maybe Int
parseIssueId output =
  case T.strip output of
    t
      | not (T.null t), T.all isDigit t -> Just (read (T.unpack t))
      | otherwise -> Nothing
  where
    isDigit c = c >= '0' && c <= '9'

buildCreateArgs :: ChainlinkIssueCreateArgs -> [String]
buildCreateArgs args =
  ["create", T.unpack (cicaTitle args), "-q"]
    ++ case cicaPriority args of
      Just p -> ["-p", T.unpack p]
      Nothing -> []
    ++ case cicaLabels args of
      Just labels -> concatMap (\l -> ["-l", T.unpack l]) labels
      Nothing -> []

buildSessionStartArgs :: [String]
buildSessionStartArgs = ["session", "start"]

buildSessionStatusArgs :: [String]
buildSessionStatusArgs = ["session", "status", "--json"]

buildShowArgs :: Int -> [String]
buildShowArgs issueId = ["issue", "show", show issueId, "--json"]

buildCommentArgs :: ChainlinkIssueCommentArgs -> [String]
buildCommentArgs args =
  ["comment", show (cicIssueId args), T.unpack (cicMessage args)]

buildSubissueArgs :: ChainlinkSubissueCreateArgs -> [String]
buildSubissueArgs args =
  ["subissue", show (cscParentId args), T.unpack (cscTitle args)]
    ++ case cscPriority args of
      Just p -> ["-p", T.unpack p]
      Nothing -> []
    ++ case cscLabels args of
      Just labels -> concatMap (\l -> ["-l", T.unpack l]) labels
      Nothing -> []

buildSessionWorkArgs :: ChainlinkSessionWorkArgs -> [String]
buildSessionWorkArgs args = ["session", "work", show (cswIssueId args)]

buildSessionEndArgs :: ChainlinkSessionEndArgs -> [String]
buildSessionEndArgs args =
  ["session", "end"]
    ++ case cseNotes args of
      Just n -> ["--notes", T.unpack n]
      Nothing -> []

buildCloseArgs :: ChainlinkIssueCloseArgs -> [String]
buildCloseArgs args = ["close", show (cisIssueId args), "-q"]

buildTimerStartArgs :: ChainlinkTimerStartArgs -> [String]
buildTimerStartArgs args = ["timer", "start", show (ctsIssueId args)]

buildTimerStopArgs :: ChainlinkTimerStopArgs -> [String]
buildTimerStopArgs args = ["timer", "stop", show (ctstopIssueId args)]

buildTimerStatusArgs :: ChainlinkTimerStatusArgs -> [String]
buildTimerStatusArgs args =
  ["timer", "show"]
    ++ case ctstatusIssueId args of
      Just issueId -> [show issueId]
      Nothing -> []

buildListArgs :: ChainlinkIssueListArgs -> [String]
buildListArgs args =
  ["issue", "list", "--json"]
    ++ case cilStatus args of
      Just s -> ["--status", T.unpack s]
      Nothing -> []
    ++ case cilPriority args of
      Just p -> ["--priority", T.unpack p]
      Nothing -> []
    ++ case cilLabels args of
      Just labels -> concatMap (\l -> ["--label", T.unpack l]) labels
      Nothing -> []
    ++ case cilMilestone args of
      Just m -> ["--milestone", T.unpack m]
      Nothing -> []

buildUpdateArgs :: ChainlinkIssueUpdateArgs -> [String]
buildUpdateArgs args =
  case buildUpdateCommands args of
    command : _ -> command
    [] -> ["issue", "update", show (ciuIssueId args)]

buildUpdateCommands :: ChainlinkIssueUpdateArgs -> [[String]]
buildUpdateCommands args =
  statusCommands
    <> priorityCommands
    <> labelCommands
    <> milestoneCommands
  where
    issueId = show (ciuIssueId args)
    statusCommands =
      case ciuStatus args of
        Just status -> issueStatusCommand issueId status
        Nothing -> []
    priorityCommands =
      case ciuPriority args of
        Just priority -> [["issue", "update", issueId, "--priority", T.unpack priority]]
        Nothing -> []
    labelCommands =
      case ciuLabels args of
        Just labels -> map (\label -> ["issue", "label", issueId, T.unpack label]) labels
        Nothing -> []
    milestoneCommands =
      case ciuMilestone args of
        Just milestone -> [["milestone", "add", T.unpack milestone, issueId]]
        Nothing -> []

issueStatusCommand :: String -> Text -> [[String]]
issueStatusCommand issueId status =
  case T.toLower status of
    "in_progress" -> [["session", "work", issueId]]
    "open" -> [["issue", "reopen", issueId, "--quiet"]]
    "closed" -> [["issue", "close", issueId, "--quiet"]]
    _ -> []

isSupportedIssueUpdateStatus :: Text -> Bool
isSupportedIssueUpdateStatus status =
  T.toLower status `elem` ["in_progress", "open", "closed"]

buildBlockArgs :: ChainlinkBlockArgs -> [String]
buildBlockArgs args = ["block", show (cbChildId args), show (cbBlockerId args)]

buildRelateArgs :: ChainlinkRelateArgs -> [String]
buildRelateArgs args =
  ["relate", show (crIssue1 args), show (crIssue2 args)]

buildCascadeArgs :: ChainlinkCascadeArgs -> [String]
buildCascadeArgs args = ["cascade", show (ccIssueId args)]

buildMilestoneCreateArgs :: ChainlinkMilestoneCreateArgs -> [String]
buildMilestoneCreateArgs args =
  ["milestone", "create", T.unpack (cmcTitle args)]
    ++ case cmcDescription args of
      Just d -> ["--description", T.unpack d]
      Nothing -> []

buildMilestoneListArgs :: [String]
buildMilestoneListArgs = ["milestone", "list", "--json"]
