{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}

module ExoMonad.Guest.Tools.ResumeBlockedLeafTest (resumeBlockedLeafTests) where

import Data.Aeson (Value (Object), decode)
import Data.Aeson.KeyMap qualified as KeyMap
import ExoMonad.Guest.Tool.Class (MCPTool (toolName))
import ExoMonad.Guest.Tools.ResumeBlockedLeaf
import Test.Tasty (TestTree, testGroup)
import Test.Tasty.HUnit (testCase, (@=?), (@?=))

resumeBlockedLeafTests :: TestTree
resumeBlockedLeafTests =
  testGroup
    "resume_blocked_leaf schema and argument contract"
    [ testCase "JSON arguments round-trip" $ do
        let args =
              ResumeBlockedLeafArgs
                { rblChainlinkIssueId = 949,
                  rblExpectedInvocationId = "invocation-1",
                  rblExpectedBranch = "main.leaf",
                  rblExpectedWorktreeFingerprint = "sha256:abc",
                  rblTask = "Continue the blocked assignment",
                  rblHumanApproved = True
                }
        (decode "{\"chainlink_issue_id\":949,\"expected_invocation_id\":\"invocation-1\",\"expected_branch\":\"main.leaf\",\"expected_worktree_fingerprint\":\"sha256:abc\",\"task\":\"Continue the blocked assignment\",\"human_approved\":true}" :: Maybe ResumeBlockedLeafArgs)
          @=? Just args,
      testCase "schema exposes only host proof fields" $ do
        let properties = case KeyMap.lookup "properties" resumeBlockedLeafSchema of
              Just (Object value) -> value
              _ -> mempty
        mapM_
          (\field -> KeyMap.member field properties @?= True)
          [ "chainlink_issue_id",
            "expected_invocation_id",
            "expected_branch",
            "expected_worktree_fingerprint",
            "task",
            "human_approved"
          ]
        KeyMap.member "agent_type" properties @?= False
        KeyMap.member "branch_name" properties @?= False,
      testCase "tool name is resume_blocked_leaf" $
        toolName @ResumeBlockedLeaf @=? "resume_blocked_leaf"
    ]
