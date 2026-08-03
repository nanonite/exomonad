{-# LANGUAGE OverloadedStrings #-}

module ExoMonad.Guest.Tools.PollWorkersTest (pollWorkersTests) where

import Data.Aeson (Value, object, (.=))
import Data.Text (Text)
import Data.Text qualified as T
import ExoMonad.Guest.Tools.PollWorkers (pollWorkersNote, renderWorkersTable)
import Test.Tasty (TestTree, testGroup)
import Test.Tasty.HUnit (assertBool, testCase)

pollWorkersTests :: TestTree
pollWorkersTests =
  testGroup
    "poll_workers routing display"
    [ testCase "retired window routing is rendered as a target with lifecycle" $ do
        let table = renderWorkersTable [retiredWindowRow]
        assertContains "@17" table
        assertContains "RETIRED(exit_code=0)" table
        assertContains "TARGET" table,
      testCase "missing routing is distinguished from a dead target" $ do
        let table = renderWorkersTable [unroutedRow]
        assertContains "NO-ROUTING-RECORDED" table
        assertContains "no persisted routing target" (pollWorkersNote [unroutedRow]),
      testCase "missing lifecycle status is not inferred from tmux state" $ do
        let table = renderWorkersTable [missingLifecycleRow]
        assertDoesNotContain "  LIVE" table
        assertDoesNotContain "  DEAD" table
    ]

retiredWindowRow :: Value
retiredWindowRow = workerRow "@17" "RETIRED(exit_code=0)" False

unroutedRow :: Value
unroutedRow = workerRow "" "NO-ROUTING-RECORDED" False

missingLifecycleRow :: Value
missingLifecycleRow =
  object
    [ "name" .= ("issue-715-opencode" :: Text),
      "role" .= ("dev" :: Text),
      "active_issue" .= ("715" :: Text),
      "issue_status" .= ("closed" :: Text),
      "chainlink_session_state" .= ("issue_closed" :: Text),
      "window_id" .= ("@18" :: Text),
      "pane_id" .= ("%18" :: Text),
      "pane_alive" .= True,
      "age_mins" .= (2 :: Int)
    ]

workerRow :: Text -> Text -> Bool -> Value
workerRow windowId lifecycleStatus alive =
  object
    [ "name" .= ("issue-715-opencode" :: Text),
      "role" .= ("dev" :: Text),
      "active_issue" .= ("715" :: Text),
      "issue_status" .= ("closed" :: Text),
      "chainlink_session_state" .= ("issue_closed" :: Text),
      "window_id" .= windowId,
      "pane_id" .= ("" :: Text),
      "pane_alive" .= alive,
      "age_mins" .= (2 :: Int),
      "lifecycle_status" .= lifecycleStatus
    ]

assertContains :: Text -> Text -> IO ()
assertContains needle haystack =
  assertBool ("expected output to contain " <> T.unpack needle) (needle `T.isInfixOf` haystack)

assertDoesNotContain :: Text -> Text -> IO ()
assertDoesNotContain needle haystack =
  assertBool ("expected output not to contain " <> T.unpack needle) (not (needle `T.isInfixOf` haystack))
