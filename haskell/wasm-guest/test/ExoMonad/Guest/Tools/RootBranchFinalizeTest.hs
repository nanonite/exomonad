{-# LANGUAGE OverloadedStrings #-}

module ExoMonad.Guest.Tools.RootBranchFinalizeTest (rootBranchFinalizeTests) where

import Control.Monad.Freer (run)
import Control.Monad.Freer.Coroutine (Status (Continue, Done), runC)
import Data.Aeson (Value, decode, encode, object, toJSON, (.=))
import Data.ByteString qualified as BS
import Data.ByteString.Lazy qualified as BL
import Data.Int (Int32)
import Data.Text (Text)
import Data.Text.Lazy qualified as TL
import Data.Word (Word8)
import Effects.Envelope qualified as Envelope
import Effects.Process qualified as Proc
import ExoMonad.Guest.Tool.Suspend.Types (EffectRequest (..))
import ExoMonad.Guest.Tools.RootBranchFinalize
  ( RootBranchFinalizeArgs (..),
    rootBranchFinalizeCore,
    rootBranchFinalizeMergeArgs,
  )
import Proto3.Suite.Class (toLazyByteString)
import Test.Tasty (TestTree, testGroup)
import Test.Tasty.HUnit (assertEqual, testCase)

rootBranchFinalizeTests :: TestTree
rootBranchFinalizeTests =
  testGroup
    "root branch finalization"
    [ testCase "arguments and fast-forward argv are stable" $ do
        let value = object ["branch" .= ("main" :: Text), "working_dir" .= ("repo" :: Text)]
            expected = RootBranchFinalizeArgs "main" (Just "repo")
        assertEqual "JSON arguments" (Just expected) (decode (encode value))
        assertEqual "fast-forward merge argv" ["merge", "--ff-only", "origin/main"] (rootBranchFinalizeMergeArgs "main"),
      testCase "clean fast-forward finalization returns matching heads" $ do
        assertEqual
          "verified root finalization"
          ( Right
              ( object
                  [ "branch" .= ("main" :: Text),
                    "local_head_sha" .= ("remote-head" :: Text),
                    "remote_head_sha" .= ("remote-head" :: Text),
                    "ancestry_proof" .= ("ancestor:local-head->remote-head" :: Text),
                    "fast_forward" .= True
                  ]
              )
          )
          (runRootWithResponses (RootBranchFinalizeArgs "main" Nothing) successResponses),
      testCase "dirty checkout fails before fetch or merge" $ do
        assertEqual
          "dirty root checkout"
          (Left "root finalization requires a clean checkout")
          (runRootWithResponses (RootBranchFinalizeArgs "main" Nothing) dirtyResponses),
      testCase "non-fast-forward root is rejected" $ do
        assertEqual
          "divergent root history"
          (Left "root branch is not fast-forwardable: git command failed: histories diverged")
          (runRootWithResponses (RootBranchFinalizeArgs "main" Nothing) nonFastForwardResponses)
    ]

successResponses :: [(Int32, Text, Text)]
successResponses =
  [ (0, "main", ""),
    (0, "", ""),
    (0, "local-head", ""),
    (0, "", ""),
    (0, "remote-head", ""),
    (0, "", ""),
    (0, "", ""),
    (0, "remote-head", "")
  ]

dirtyResponses :: [(Int32, Text, Text)]
dirtyResponses = [(0, "main", ""), (0, " M CHANGELOG.md", "")]

nonFastForwardResponses :: [(Int32, Text, Text)]
nonFastForwardResponses =
  [ (0, "main", ""),
    (0, "", ""),
    (0, "local-head", ""),
    (0, "", ""),
    (0, "remote-head", ""),
    (1, "", "histories diverged")
  ]

runRootWithResponses :: RootBranchFinalizeArgs -> [(Int32, Text, Text)] -> Either Text Value
runRootWithResponses args responses =
  run $ runC (rootBranchFinalizeCore args) >>= handleResponses 0 responses
  where
    handleResponses _ _ (Done result) = pure result
    handleResponses processCount remaining (Continue request resume)
      | erType request /= "process.run" =
          resume (encodedEffectResponse BS.empty) >>= handleResponses processCount remaining
      | otherwise =
          case remaining of
            [] -> pure (Left "unexpected process request")
            (exitCode, stdout, stderr) : rest ->
              resume (processResponse exitCode stdout stderr)
                >>= handleResponses (processCount + 1) rest

processResponse :: Int32 -> Text -> Text -> Value
processResponse exitCode stdout stderr =
  encodedEffectResponse
    ( BL.toStrict
        (toLazyByteString (Proc.RunResponse exitCode (TL.fromStrict stdout) (TL.fromStrict stderr)))
    )

encodedEffectResponse :: BS.ByteString -> Value
encodedEffectResponse payload =
  toJSON
    ( BS.unpack
        ( BL.toStrict
            (toLazyByteString (Envelope.EffectResponse (Just (Envelope.EffectResponseResultPayload payload))))
        ) ::
        [Word8]
    )
