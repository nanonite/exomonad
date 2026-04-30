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
        buildCloseArgs (ChainlinkIssueCloseArgs 42)
          @=? ["close", "42", "-q"],

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
      testCase "chainlinkWorkerProtocolText contains atomic close steps" $
        "4-step atomic close sequence" `T.isInfixOf` chainlinkWorkerProtocolText @?= True,
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
        decoded @=? Just item
    ]
