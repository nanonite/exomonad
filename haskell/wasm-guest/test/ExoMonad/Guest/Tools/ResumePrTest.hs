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
        let args = ResumePrArgs 104 "Address the requested review changes and update the existing PR"
        (decode "{\"pr_number\":104,\"task\":\"Address the requested review changes and update the existing PR\"}" :: Maybe ResumePrArgs)
          @=? Just args,
      testCase "schema requires only PR number and task" $ do
        let properties = case KeyMap.lookup "properties" resumePrSchema of
              Just (Object value) -> value
              _ -> mempty
        KeyMap.member "pr_number" properties @?= True
        KeyMap.member "task" properties @?= True
        KeyMap.member "branch_name" properties @?= False
        KeyMap.member "agent_type" properties @?= False,
      testCase "tool name is resume_pr" $
        toolName @ResumePr @=? "resume_pr"
    ]
