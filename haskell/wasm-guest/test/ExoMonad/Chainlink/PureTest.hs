module ExoMonad.Chainlink.PureTest (pureTests) where

import Data.Aeson (decode, encode)
import Data.Text (Text)
import Data.Text qualified as T
import ExoMonad.Chainlink.Pure
import Test.Tasty (TestTree, testGroup)
import Test.Tasty.HUnit (testCase, (@=?), (@?=))

pureTests :: TestTree
pureTests =
  testGroup "Pure parsing and argument building"
    [ -- parseIssueId
      testCase "parseIssueId: plain number" $
        parseIssueId "42" @=? Just 42,
      testCase "parseIssueId: trims whitespace" $
        parseIssueId "  123  " @=? Just 123,
      testCase "parseIssueId: non-numeric returns Nothing" $
        parseIssueId "abc" @=? Nothing,
      testCase "parseIssueId: empty string returns Nothing" $
        parseIssueId "" @=? Nothing,
      testCase "parseIssueId: mixed content returns Nothing" $
        parseIssueId "12abc34" @=? Nothing,
      testCase "parseIssueId: zero" $
        parseIssueId "0" @=? Just 0,

      -- buildCreateArgs
      testCase "buildCreateArgs: title only" $
        buildCreateArgs (ChainlinkIssueCreateArgs "My issue" Nothing Nothing Nothing)
          @=? ["create", "My issue", "-q"],
      testCase "buildCreateArgs: with priority" $
        buildCreateArgs (ChainlinkIssueCreateArgs "Bug fix" Nothing (Just "high") Nothing)
          @=? ["create", "Bug fix", "-q", "-p", "high"],
      testCase "buildCreateArgs: with labels" $
        buildCreateArgs (ChainlinkIssueCreateArgs "Feature" Nothing Nothing (Just ["bug", "frontend"]))
          @=? ["create", "Feature", "-q", "-l", "bug", "-l", "frontend"],
      testCase "buildCreateArgs: priority + labels" $
        buildCreateArgs (ChainlinkIssueCreateArgs "Release" Nothing (Just "critical") (Just ["ops"]))
          @=? ["create", "Release", "-q", "-p", "critical", "-l", "ops"],

      -- buildShowArgs
      testCase "buildShowArgs: basic" $
        buildShowArgs 42 @=? ["issue", "show", "42", "--json"],

      -- buildCommentArgs
      testCase "buildCommentArgs: basic" $
        buildCommentArgs (ChainlinkIssueCommentArgs 1 "looks good")
          @=? ["comment", "1", "looks good"],
      testCase "buildCommentArgs: message with spaces" $
        buildCommentArgs (ChainlinkIssueCommentArgs 7 "needs more testing")
          @=? ["comment", "7", "needs more testing"],

      -- buildSubissueArgs
      testCase "buildSubissueArgs: title only" $
        buildSubissueArgs (ChainlinkSubissueCreateArgs 5 "Sub task" Nothing Nothing)
          @=? ["subissue", "5", "Sub task"],
      testCase "buildSubissueArgs: with priority" $
        buildSubissueArgs (ChainlinkSubissueCreateArgs 5 "Sub task" (Just "high") Nothing)
          @=? ["subissue", "5", "Sub task", "-p", "high"],
      testCase "buildSubissueArgs: with labels" $
        buildSubissueArgs (ChainlinkSubissueCreateArgs 5 "Sub task" Nothing (Just ["bug"]))
          @=? ["subissue", "5", "Sub task", "-l", "bug"],
      testCase "buildSubissueArgs: priority + labels" $
        buildSubissueArgs (ChainlinkSubissueCreateArgs 5 "Sub task" (Just "low") (Just ["enhancement", "docs"]))
          @=? ["subissue", "5", "Sub task", "-p", "low", "-l", "enhancement", "-l", "docs"],

      -- buildSessionWorkArgs
      testCase "buildSessionWorkArgs: basic" $
        buildSessionWorkArgs (ChainlinkSessionWorkArgs 99)
          @=? ["session", "work", "99"],

      -- buildSessionEndArgs
      testCase "buildSessionEndArgs: no notes" $
        buildSessionEndArgs (ChainlinkSessionEndArgs Nothing)
          @=? ["session", "end"],
      testCase "buildSessionEndArgs: with notes" $
        buildSessionEndArgs (ChainlinkSessionEndArgs (Just "Implemented the feature"))
          @=? ["session", "end", "--notes", "Implemented the feature"],

      -- buildCloseArgs
      testCase "buildCloseArgs: basic" $
        buildCloseArgs (ChainlinkIssueCloseArgs 42 Nothing)
          @=? ["close", "42", "-q"],

      -- buildLocksReleaseArgs
      testCase "buildLocksReleaseArgs: basic" $
        buildLocksReleaseArgs (ChainlinkIssueCloseArgs 42 Nothing)
          @=? ["locks", "release", "42"],

      -- ChainlinkIssueShowOutput JSON roundtrip
      testCase "ChainlinkIssueShowOutput JSON roundtrip: all fields" $ do
        let output = ChainlinkIssueShowOutput
              { cisoId = 42,
                cisoTitle = "Test issue",
                cisoStatus = "open",
                cisoPriority = Just "high",
                cisoLabels = ["bug", "frontend"]
              }
            decoded = decode (encode output) :: Maybe ChainlinkIssueShowOutput
        decoded @=? Just output,
      testCase "ChainlinkIssueShowOutput JSON roundtrip: no priority" $ do
        let output = ChainlinkIssueShowOutput
              { cisoId = 1,
                cisoTitle = "Simple task",
                cisoStatus = "closed",
                cisoPriority = Nothing,
                cisoLabels = []
              }
            decoded = decode (encode output) :: Maybe ChainlinkIssueShowOutput
        decoded @=? Just output,

      -- chainlinkWorkerProtocolText content
      testCase "chainlinkWorkerProtocolText has correct header" $
        "# Chainlink Worker Protocol" `T.isPrefixOf` chainlinkWorkerProtocolText @?= True,
      testCase "chainlinkWorkerProtocolText contains single atomic close step" $
        "single atomic close tool" `T.isInfixOf` chainlinkWorkerProtocolText @?= True,
      testCase "chainlinkWorkerProtocolText contains hard rules" $
        "## Hard Rules" `T.isInfixOf` chainlinkWorkerProtocolText @?= True,
      testCase "chainlinkWorkerProtocolText contains MCP tools table" $
        "| Tool | Purpose |" `T.isInfixOf` chainlinkWorkerProtocolText @?= True,

      -- buildListArgs
      testCase "buildListArgs: no filters" $
        buildListArgs (ChainlinkIssueListArgs Nothing Nothing Nothing Nothing)
          @=? ["issue", "list", "--json"],
      testCase "buildListArgs: with status" $
        buildListArgs (ChainlinkIssueListArgs (Just "open") Nothing Nothing Nothing)
          @=? ["issue", "list", "--json", "--status", "open"],
      testCase "buildListArgs: with priority and labels" $
        buildListArgs (ChainlinkIssueListArgs Nothing (Just "high") (Just ["bug"]) Nothing)
          @=? ["issue", "list", "--json", "--priority", "high", "--label", "bug"],
      testCase "buildListArgs: with milestone" $
        buildListArgs (ChainlinkIssueListArgs Nothing Nothing Nothing (Just "M1"))
          @=? ["issue", "list", "--json", "--milestone", "M1"],

      -- buildUpdateArgs
      testCase "buildUpdateArgs: id only" $
        buildUpdateArgs (ChainlinkIssueUpdateArgs 5 Nothing Nothing Nothing Nothing)
          @=? ["issue", "update", "5"],
      testCase "buildUpdateArgs: status + priority" $
        buildUpdateArgs (ChainlinkIssueUpdateArgs 5 (Just "blocked") (Just "high") Nothing Nothing)
          @=? ["issue", "update", "5", "-s", "blocked", "-p", "high"],
      testCase "buildUpdateArgs: with labels" $
        buildUpdateArgs (ChainlinkIssueUpdateArgs 5 Nothing Nothing (Just ["bug", "frontend"]) Nothing)
          @=? ["issue", "update", "5", "-l", "bug", "-l", "frontend"],
      testCase "buildUpdateArgs: with milestone" $
        buildUpdateArgs (ChainlinkIssueUpdateArgs 5 Nothing Nothing Nothing (Just "M2"))
          @=? ["issue", "update", "5", "-m", "M2"],

      -- buildBlockArgs
      testCase "buildBlockArgs: basic" $
        buildBlockArgs (ChainlinkBlockArgs 5 10)
          @=? ["block", "5", "10"],

      -- buildRelateArgs
      testCase "buildRelateArgs: basic" $
        buildRelateArgs (ChainlinkRelateArgs 1 2 "duplicates")
          @=? ["relate", "1", "2", "duplicates"],

      -- buildCascadeArgs
      testCase "buildCascadeArgs: basic" $
        buildCascadeArgs (ChainlinkCascadeArgs 42)
          @=? ["cascade", "42"],

      -- buildMilestoneCreateArgs
      testCase "buildMilestoneCreateArgs: title only" $
        buildMilestoneCreateArgs (ChainlinkMilestoneCreateArgs "M1" Nothing)
          @=? ["milestone", "create", "M1"],
      testCase "buildMilestoneCreateArgs: with description" $
        buildMilestoneCreateArgs (ChainlinkMilestoneCreateArgs "M1" (Just "First milestone"))
          @=? ["milestone", "create", "M1", "--description", "First milestone"],

      -- buildMilestoneListArgs
      testCase "buildMilestoneListArgs: basic" $
        buildMilestoneListArgs @=? ["milestone", "list", "--json"],

      -- buildSyncArgs
      testCase "buildSyncArgs: basic" $
        buildSyncArgs @=? ["sync"],

      -- ChainlinkIssueListItem JSON roundtrip
      testCase "ChainlinkIssueListItem JSON roundtrip: all fields" $ do
        let item = ChainlinkIssueListItem
              { ciliId = 42,
                ciliTitle = "Bug fix",
                ciliStatus = "open",
                ciliPriority = Just "high",
                ciliLabels = ["bug"]
              }
            decoded = decode (encode item) :: Maybe ChainlinkIssueListItem
        decoded @=? Just item,
      testCase "ChainlinkIssueListItem JSON roundtrip: no priority" $ do
        let item = ChainlinkIssueListItem
              { ciliId = 1,
                ciliTitle = "Simple task",
                ciliStatus = "closed",
                ciliPriority = Nothing,
                ciliLabels = []
              }
            decoded = decode (encode item) :: Maybe ChainlinkIssueListItem
        decoded @=? Just item,

      -- ChainlinkMilestoneCreateOutput JSON roundtrip
      testCase "ChainlinkMilestoneCreateOutput JSON roundtrip" $ do
        let output = ChainlinkMilestoneCreateOutput { cmcoMilestoneId = 5 }
            decoded = decode (encode output) :: Maybe ChainlinkMilestoneCreateOutput
        decoded @=? Just output,

      -- ChainlinkMilestoneListItem JSON roundtrip
      testCase "ChainlinkMilestoneListItem JSON roundtrip: all fields" $ do
        let item = ChainlinkMilestoneListItem
              { cmliId = 3,
                cmliTitle = "Alpha",
                cmliDescription = Just "First release"
              }
            decoded = decode (encode item) :: Maybe ChainlinkMilestoneListItem
        decoded @=? Just item,
      testCase "ChainlinkMilestoneListItem JSON roundtrip: no description" $ do
        let item = ChainlinkMilestoneListItem
              { cmliId = 4,
                cmliTitle = "Beta",
                cmliDescription = Nothing
              }
            decoded = decode (encode item) :: Maybe ChainlinkMilestoneListItem
        decoded @=? Just item,

      -- buildLocksListArgs
      testCase "buildLocksListArgs: basic" $
        buildLocksListArgs @=? ["locks", "list", "--json"],

      -- hasActiveLocks
      testCase "hasActiveLocks: empty list returns False" $
        hasActiveLocks "[]" @=? False,
      testCase "hasActiveLocks: non-empty list returns True" $
        hasActiveLocks "[{\"id\":1,\"issue_id\":42}]" @=? True,
      testCase "hasActiveLocks: no issue_id field" $
        hasActiveLocks "[{\"id\":1}]" @=? True,
      testCase "hasActiveLocks: invalid JSON returns False" $
        hasActiveLocks "not json" @=? False,

      -- LocksListEntry JSON roundtrip
      testCase "LocksListEntry JSON roundtrip: all fields" $ do
        let entry = LocksListEntry { lleId = 5, lleIssueId = Just 42 }
            decoded = decode (encode entry) :: Maybe LocksListEntry
        decoded @=? Just entry,
      testCase "LocksListEntry JSON roundtrip: no issue_id" $ do
        let entry = LocksListEntry { lleId = 5, lleIssueId = Nothing }
            decoded = decode (encode entry) :: Maybe LocksListEntry
        decoded @=? Just entry,

      ----------------------------------------------------------------------
      -- Worker Status
      ----------------------------------------------------------------------

      -- parseGitDiffStat
      testCase "parseGitDiffStat: single file" $
        parseGitDiffStat " src/main.rs | 5 +++++\n 1 file changed, 5 insertions(+)\n"
          @=? ["src/main.rs"],
      testCase "parseGitDiffStat: multiple files" $
        parseGitDiffStat " src/a.hs | 2 +-\n src/b.hs | 3 +++\n 2 files changed, 4 insertions(+), 1 deletion(-)\n"
          @=? ["src/a.hs", "src/b.hs"],
      testCase "parseGitDiffStat: empty output" $
        parseGitDiffStat "" @=? [],
      testCase "parseGitDiffStat: only summary line" $
        parseGitDiffStat " 0 files changed\n" @=? [],
      testCase "parseGitDiffStat: no changes" $
        parseGitDiffStat "" @=? [],

      -- UsageRecord JSON roundtrip
      testCase "UsageRecord JSON roundtrip: all fields" $ do
        let record = UsageRecord
              { urIssueId = Just 42,
                urInputTokens = Just 1500,
                urOutputTokens = Just 300,
                urEstimatedCostUsd = Just 0.015
              }
            decoded = decode (encode record) :: Maybe UsageRecord
        decoded @=? Just record,
      testCase "UsageRecord JSON roundtrip: no optional fields" $ do
        let record = UsageRecord
              { urIssueId = Nothing,
                urInputTokens = Nothing,
                urOutputTokens = Nothing,
                urEstimatedCostUsd = Nothing
              }
            decoded = decode (encode record) :: Maybe UsageRecord
        decoded @=? Just record,

      -- WorkerStatusEntry JSON roundtrip
      testCase "WorkerStatusEntry JSON roundtrip: all fields" $ do
        let entry = WorkerStatusEntry
              { wseAgentId = Just "agent-1",
                wseIssueId = 42,
                wseIssueTitle = "Fix bug",
                wseLockHeldMinutes = Just 15.5,
                wseInputTokens = Just 5000,
                wseOutputTokens = Just 1000,
                wseEstimatedCostUsd = Just 0.05,
                wseUncommittedFiles = ["src/main.rs"]
              }
            decoded = decode (encode entry) :: Maybe WorkerStatusEntry
        decoded @=? Just entry,
      testCase "WorkerStatusEntry JSON roundtrip: minimal" $ do
        let entry = WorkerStatusEntry
              { wseAgentId = Nothing,
                wseIssueId = 1,
                wseIssueTitle = "Task",
                wseLockHeldMinutes = Nothing,
                wseInputTokens = Nothing,
                wseOutputTokens = Nothing,
                wseEstimatedCostUsd = Nothing,
                wseUncommittedFiles = []
              }
            decoded = decode (encode entry) :: Maybe WorkerStatusEntry
        decoded @=? Just entry,

      -- correlateWorkerStatus
      testCase "correlateWorkerStatus: empty inputs returns empty list" $
        correlateWorkerStatus [] [] [] [] @=? [],
      testCase "correlateWorkerStatus: one issue, no locks, no usage" $ do
        let issues = [ChainlinkIssueListItem 1 "Task A" "open" Nothing []]
            result = correlateWorkerStatus issues [] [] []
        length result @?= 1
        result @?=
          [ WorkerStatusEntry
              { wseAgentId = Nothing,
                wseIssueId = 1,
                wseIssueTitle = "Task A",
                wseLockHeldMinutes = Nothing,
                wseInputTokens = Nothing,
                wseOutputTokens = Nothing,
                wseEstimatedCostUsd = Nothing,
                wseUncommittedFiles = []
              }
          ],
      testCase "correlateWorkerStatus: issue with matching lock" $ do
        let issues = [ChainlinkIssueListItem 1 "Task A" "open" Nothing []]
            locks = [LocksListEntry 10 (Just 1)]
            result = correlateWorkerStatus issues locks [] []
        wseLockHeldMinutes (head result) @?= Just 0,
      testCase "correlateWorkerStatus: issue with non-matching lock" $ do
        let issues = [ChainlinkIssueListItem 1 "Task A" "open" Nothing []]
            locks = [LocksListEntry 10 (Just 99)]
            result = correlateWorkerStatus issues locks [] []
        wseLockHeldMinutes (head result) @?= Nothing,
      testCase "correlateWorkerStatus: issue with usage records" $ do
        let issues = [ChainlinkIssueListItem 1 "Task A" "open" Nothing []]
            usage = [UsageRecord (Just 1) (Just 1000) (Just 200) (Just 0.01)]
            result = correlateWorkerStatus issues [] usage []
            entry = head result
        wseInputTokens entry @?= Just 1000
        wseOutputTokens entry @?= Just 200
        wseEstimatedCostUsd entry @?= Just 0.01,
      testCase "correlateWorkerStatus: multiple issues with mixed data" $ do
        let issues =
              [ ChainlinkIssueListItem 1 "Task A" "open" Nothing [],
                ChainlinkIssueListItem 2 "Task B" "open" Nothing []
              ]
            locks = [LocksListEntry 10 (Just 1)]
            usage =
              [ UsageRecord (Just 1) (Just 500) (Just 100) (Just 0.005),
                UsageRecord (Just 1) (Just 200) (Just 50) (Just 0.002)
              ]
            result = correlateWorkerStatus issues locks usage ["src/lib.rs"]
            entryA = head result
            entryB = result !! 1
        wseLockHeldMinutes entryA @?= Just 0
        wseLockHeldMinutes entryB @?= Nothing
        wseInputTokens entryA @?= Just 700  -- 500 + 200
        wseOutputTokens entryA @?= Just 150  -- 100 + 50
        wseEstimatedCostUsd entryA @?= Just 0.007  -- 0.005 + 0.002
        wseInputTokens entryB @?= Nothing
        wseOutputTokens entryB @?= Nothing
        wseEstimatedCostUsd entryB @?= Nothing
        wseUncommittedFiles entryA @?= ["src/lib.rs"]
        wseUncommittedFiles entryB @?= ["src/lib.rs"],
      testCase "correlateWorkerStatus: uncommitted files propagated" $ do
        let issues = [ChainlinkIssueListItem 1 "Task A" "open" Nothing []]
            result = correlateWorkerStatus issues [] [] ["a.rs", "b.rs"]
        wseUncommittedFiles (head result) @?= ["a.rs", "b.rs"]
    ]
