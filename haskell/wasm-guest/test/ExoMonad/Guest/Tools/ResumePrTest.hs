{-# LANGUAGE OverloadedStrings #-}

module ExoMonad.Guest.Tools.ResumePrTest (resumePrTests) where

import Data.Aeson (Value (Object), decode)
import Data.Aeson.KeyMap qualified as KeyMap
import ExoMonad.Guest.Tool.Class (MCPTool (toolName))
import ExoMonad.Guest.Tools.ResumePr
import Test.Tasty (TestTree, testGroup)
import Test.Tasty.HUnit (testCase, (@=?), (@?=))

resumePrTests :: TestTree
resumePrTests =
  testGroup
    "resume_pr schema and argument contract"
    [ testCase "JSON arguments round-trip" $ do
        let args =
              ResumePrArgs
                { rpaPrNumber = 104,
                  rpaExpectedHeadSha = "abc123",
                  rpaTask = "Complete repair handoff for the existing PR"
                }
        (decode "{\"pr_number\":104,\"expected_head_sha\":\"abc123\",\"task\":\"Complete repair handoff for the existing PR\"}" :: Maybe ResumePrArgs)
          @=? Just args,
      testCase "schema exposes only host-approved resume fields" $ do
        let properties = case KeyMap.lookup "properties" resumePrSchema of
              Just (Object value) -> value
              _ -> mempty
        KeyMap.member "pr_number" properties @?= True
        KeyMap.member "expected_head_sha" properties @?= True
        KeyMap.member "task" properties @?= True
        mapM_
          (\field -> KeyMap.member field properties @?= False)
          ["read_first", "steps", "verify", "boundary", "context", "done_criteria", "model", "agent_type", "branch_name"],
      testCase "complete handoff remains the exact resumed task" $
        let args =
              ResumePrArgs
                { rpaPrNumber = 104,
                  rpaExpectedHeadSha = "abc123",
                  rpaTask = "ROOT CAUSE: stale state; VERIFY: cargo test; BOUNDARY: existing branch only"
                }
         in renderResumePrTask args @=? rpaTask args,
      testCase "tool name is resume_pr" $ toolName @ResumePr @=? "resume_pr"
    ]
