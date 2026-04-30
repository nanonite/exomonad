module ExoMonad.Chainlink.PureTest (pureTests) where

import ExoMonad.Chainlink.Pure
import Test.Tasty (TestTree, testGroup)
import Test.Tasty.HUnit (testCase, (@=?))

pureTests :: TestTree
pureTests =
  testGroup "Pure parsing and argument building"
    [ testCase "parseIssueId: plain number" $
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
          @=? ["create", "Release", "-q", "-p", "critical", "-l", "ops"]
    ]
