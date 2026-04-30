module ExoMonad.Chainlink.Pure
  ( -- * Issue Create
    ChainlinkIssueCreateArgs (..),
    buildCreateArgs,

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
    buildLocksReleaseArgs,

    -- * Issue List
    ChainlinkIssueListArgs (..),
    buildListArgs,
    ChainlinkIssueListItem (..),

    -- * Issue Update
    ChainlinkIssueUpdateArgs (..),
    buildUpdateArgs,

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

    -- * Sync
    buildSyncArgs,

    -- * Locks List
    buildLocksListArgs,
    LocksListEntry (..),
    hasActiveLocks,

    -- * Worker Protocol Text
    chainlinkWorkerProtocolText,

    -- * Utilities
    parseIssueId,
  )
where

import Data.Aeson (FromJSON (..), ToJSON (..), Value (Object), object, withObject, (.:), (.:?), (.!=), (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Encoding (encodeUtf8)
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
    cisSummary :: Maybe Text
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
    crIssue2 :: Int,
    crRelation :: Text
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
chainlinkWorkerProtocolText = "# Chainlink Worker Protocol\n\nYou are a worker enhanced with chainlink for structured task tracking and completion.\n\nChainlink is your contract with your parent TL. Claim the issue, do the work, report progress, close atomically.\n\n## Worker Chainlink Workflow\n\n### 1. Claim Your Issue\n\nImmediately on spawn, claim the chainlink issue that was assigned to you:\n\n```\nchainlink agent init <issue-id>     # Link this agent session to the issue\nchainlink session start             # Start timing\nchainlink_issue_claim               # Mark issue as claimed (prevents double-work)\n```\n\nThe issue ID should be embedded in your task description from the TL.\n\n### 2. Read the Spec\n\nRead the full issue spec before doing any work:\n\n```\nchainlink_issue_show\n```\n\nThis returns the issue description, acceptance criteria, dependencies, and any comments from the TL.\n\n### 3. Do the Work\n\n- Stay within the files listed in the issue spec\n- Use `chainlink issue comment <text>` to post progress updates after meaningful milestones\n- If blocked, do NOT silently stall \x2014 use `chainlink issue update <id> -s blocked` and `notify_parent(\"BLOCKED: <reason>\")`\n- If scope creep appears, file a `chainlink subissue <parent-id> \"New scope\"` and notify the parent\n\n### 4. Close Atomically (Single MCP Call)\n\nWhen the work is complete, call the **single atomic close tool**:\n\n```\nchainlink_issue_close issue_id=<id> summary=\"<what was done>\"\n```\n\nThe `chainlink_issue_close` tool atomically runs the full close sequence internally: release locks \x2192 close issue \x2192 end session \x2192 notify parent. If any step fails, the sequence stops and the issue remains open (safe to retry).\n\n**NEVER use `chainlink close` from the CLI.** Only use the `chainlink_issue_close` MCP tool. The CLI version bypasses the atomic sequence and leaves dangling locks + no notification.\n\n## Stuck-Escalation Path\n\nIf you are stuck (blocked, confused, or the spec is ambiguous):\n\n1. `chainlink issue update <id> -s blocked`\n2. `notify_parent(\"BLOCKED: <specific reason>\")`\n3. If no response within a reasonable time, `send_message` to the TL's team channel\n\nDo not guess. Do not implement past the ambiguity. Report exactly what is unclear.\n\n## Scope-Creep Path\n\nIf the task grows beyond the original issue spec (TL adds extra requests, or you discover prerequisite work):\n\n1. File a new sub-issue: `chainlink subissue <parent-id> \"New scope description\"`\n2. `notify_parent(\"SCOPE: Created subissue #<new-id> for <description>\")`\n3. Continue on the original issue unless redirected\n\n## Available MCP Tools\n\n| Tool | Purpose |\n|------|---------|\n| `chainlink_issue_claim` | Claim an issue (prevents double-work) |\n| `chainlink_issue_show` | Read full issue spec including description and comments |\n| `chainlink_issue_close` | Close the issue with atomic 4-step sequence |\n| `chainlink_issue_comment` | Post a progress comment on the issue |\n| `chainlink_issue_update` | Update issue status (blocked, in_progress, etc.) |\n| `chainlink_subissue_create` | Create a child issue for scope creep |\n| `chainlink_locks_release` | Release claimed locks |\n| `chainlink_locks_status` | Check what locks you hold |\n| `chainlink_timer_start` | Start a timer for time tracking |\n| `chainlink_timer_show` | Show current timer state |\n| `chainlink_session_end` | End session with optional handoff notes |\n| `notify_parent` | Report results or issues to parent TL |\n| `send_message` | Send messages to TL's team channel |\n\n## Hard Rules\n\n- Claim the issue first thing on spawn \x2014 never start work without claiming\n- Never use `chainlink close` CLI \x2014 always use `chainlink_issue_close` MCP\n- Always complete the 4-step atomic sequence (locks \x2192 close \x2192 session end \x2192 notify)\n- Never create branches or commit unless explicitly instructed\n- If blocked, report immediately \x2014 never wait more than 2 minutes before escalating\n- If scope creep appears, file a subissue \x2014 do not absorb extra work silently"

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

buildLocksReleaseArgs :: ChainlinkIssueCloseArgs -> [String]
buildLocksReleaseArgs args = ["locks", "release", show (cisIssueId args)]

buildLocksListArgs :: [String]
buildLocksListArgs = ["locks", "list", "--json"]

data LocksListEntry = LocksListEntry
  { lleId :: Int,
    lleIssueId :: Maybe Int
  }
  deriving (Generic, Show, Eq)

instance FromJSON LocksListEntry where
  parseJSON = withObject "LocksListEntry" $ \v ->
    LocksListEntry
      <$> v .: "id"
      <*> v .:? "issue_id"

instance ToJSON LocksListEntry where
  toJSON o =
    object
      [ "id" .= lleId o,
        "issue_id" .= lleIssueId o
      ]

-- | Parse locks list JSON and return True if any locks are held.
hasActiveLocks :: Text -> Bool
hasActiveLocks json =
  case Aeson.eitherDecodeStrict (encodeUtf8 json) of
    Right [] -> False
    Right (_ :: [LocksListEntry]) -> True
    Left _err -> False

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
  ["issue", "update", show (ciuIssueId args)]
    ++ case ciuStatus args of
      Just s -> ["-s", T.unpack s]
      Nothing -> []
    ++ case ciuPriority args of
      Just p -> ["-p", T.unpack p]
      Nothing -> []
    ++ case ciuLabels args of
      Just labels -> concatMap (\l -> ["-l", T.unpack l]) labels
      Nothing -> []
    ++ case ciuMilestone args of
      Just m -> ["-m", T.unpack m]
      Nothing -> []

buildBlockArgs :: ChainlinkBlockArgs -> [String]
buildBlockArgs args = ["block", show (cbChildId args), show (cbBlockerId args)]

buildRelateArgs :: ChainlinkRelateArgs -> [String]
buildRelateArgs args =
  ["relate", show (crIssue1 args), show (crIssue2 args), T.unpack (crRelation args)]

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

buildSyncArgs :: [String]
buildSyncArgs = ["sync"]
