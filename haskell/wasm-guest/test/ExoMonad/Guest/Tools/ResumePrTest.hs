{-# LANGUAGE OverloadedStrings #-}

module ExoMonad.Guest.Tools.ResumePrTest (resumePrTests) where

import Data.Aeson (Value (Object), decode)
import Data.Aeson.KeyMap qualified as KeyMap
import Data.Text qualified as T
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
                  rpaTask = "Address the requested review changes and update the existing PR",
                  rpaReadFirst = Nothing,
                  rpaSteps = Nothing,
                  rpaVerify = Nothing,
                  rpaBoundary = Nothing,
                  rpaContext = Nothing,
                  rpaDoneCriteria = Nothing
                }
        (decode "{\"pr_number\":104,\"task\":\"Address the requested review changes and update the existing PR\"}" :: Maybe ResumePrArgs)
          @=? Just args,
      testCase "schema exposes the PR task and handoff fields" $ do
        let properties = case KeyMap.lookup "properties" resumePrSchema of
              Just (Object value) -> value
              _ -> mempty
        KeyMap.member "pr_number" properties @?= True
        KeyMap.member "task" properties @?= True
        KeyMap.member "branch_name" properties @?= False
        KeyMap.member "agent_type" properties @?= False
        mapM_
          (\field -> KeyMap.member field properties @?= True)
          ["read_first", "steps", "verify", "boundary", "context", "done_criteria"],
      testCase "structured handoff fields render into the resumed task" $
        do
          let args =
                ResumePrArgs
                  { rpaPrNumber = 104,
                    rpaTask = "Repair the reviewed PR",
                    rpaReadFirst = Just ["src/lib.rs"],
                    rpaSteps = Just ["Fix the root cause"],
                    rpaVerify = Just ["cargo test"],
                    rpaBoundary = Just ["Do not create a sibling branch"],
                    rpaContext = Just "ROOT CAUSE: stale state",
                    rpaDoneCriteria = Just ["Push the fix and report results"]
                  }
              rendered = renderResumePrTask args
          if all
            (\needle -> needle `T.isInfixOf` rendered)
            ["## READ FIRST", "ROOT CAUSE: stale state", "cargo test"]
            then pure ()
            else fail "structured handoff rendering changed",
      testCase
        "tool name is resume_pr"
        $ toolName @ResumePr @=? "resume_pr"
    ]
