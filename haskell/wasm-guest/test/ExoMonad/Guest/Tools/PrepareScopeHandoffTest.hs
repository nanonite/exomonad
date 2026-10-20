{-# LANGUAGE OverloadedStrings #-}

module ExoMonad.Guest.Tools.PrepareScopeHandoffTest (prepareScopeHandoffTests) where

import Control.Monad.Freer (run)
import Control.Monad.Freer.Coroutine (Status (Continue, Done), runC)
import Data.Aeson (Value (..), decode, encode, object, toJSON, (.=))
import Data.Aeson qualified as Aeson
import Data.Aeson.Key qualified as Key
import Data.Aeson.KeyMap qualified as KeyMap
import Data.ByteString qualified as BS
import Data.ByteString.Lazy qualified as BL
import Data.Text (Text)
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Data.Word (Word8)
import Effects.Agent qualified as PA
import Effects.Envelope qualified as Envelope
import ExoMonad.Guest.Tool.Suspend.Types (EffectRequest (..))
import ExoMonad.Guest.Tools.PrepareScopeHandoff
  ( PrepareScopeHandoffArgs (..),
    prepareScopeHandoffCore,
    prepareScopeHandoffResponseValue,
    prepareScopeHandoffSchema,
  )
import Proto3.Suite.Class (toLazyByteString)
import Test.Tasty (TestTree, testGroup)
import Test.Tasty.HUnit (assertEqual, testCase)

prepareScopeHandoffTests :: TestTree
prepareScopeHandoffTests =
  testGroup
    "prepare_scope_handoff"
    [ testCase "arguments decode and schema expose the explicit snapshot contract" $ do
        let argsValue =
              object
                [ "apply" .= False,
                  "handoff_id" .= ("handoff-1" :: Text),
                  "controller_id" .= ("root-controller" :: Text),
                  "parent_run_id" .= ("root-run" :: Text),
                  "parent_scope_id" .= ("root" :: Text),
                  "child_scope_id" .= ("architecture" :: Text),
                  "expected_branch" .= ("main" :: Text),
                  "expected_head_sha" .= ("head-before" :: Text),
                  "expected_source_fingerprint" .= ("fingerprint" :: Text)
                ]
            expected = PrepareScopeHandoffArgs False "handoff-1" "root-controller" "root-run" "root" "architecture" "main" (Just "head-before") (Just "fingerprint")
        assertEqual "arguments" (Just expected) (decode (encode argsValue))
        case prepareScopeHandoffSchema of
          props -> assertEqual "required fields include handoff coordinates" True (not (null props)),
      testCase "typed host receipt projects all fields and blocked readiness remains structured" $ do
        let response = successfulResponse
            projected = prepareScopeHandoffResponseValue response
        assertEqual "projection" (Just responseValue) (Just projected)
        let blocked = response {PA.prepareScopeHandoffResponseSuccess = False, PA.prepareScopeHandoffResponseReady = False, PA.prepareScopeHandoffResponseOutcome = "blocked", PA.prepareScopeHandoffResponseError = "hook refused"}
        assertEqual "blocked response is retained as data" (Just (toJSON ("blocked" :: Text))) (field "outcome" (prepareScopeHandoffResponseValue blocked))
        assertEqual "blocked readiness is false" (Just (toJSON False)) (field "ready" (prepareScopeHandoffResponseValue blocked)),
      testCase "core sends the typed agent effect and returns its authoritative response" $ do
        let result = runCoreWith successEnvelope
        assertEqual "host response" (Right responseValue) result
    ]

args :: PrepareScopeHandoffArgs
args = PrepareScopeHandoffArgs False "handoff-1" "root-controller" "root-run" "root" "architecture" "main" Nothing Nothing

successfulResponse :: PA.PrepareScopeHandoffResponse
successfulResponse =
  PA.PrepareScopeHandoffResponse
    { PA.prepareScopeHandoffResponseSuccess = True,
      PA.prepareScopeHandoffResponseError = "",
      PA.prepareScopeHandoffResponseOutcome = "ready",
      PA.prepareScopeHandoffResponseHandoffId = "handoff-1",
      PA.prepareScopeHandoffResponseControllerId = "root-controller",
      PA.prepareScopeHandoffResponseParentRunId = "root-run",
      PA.prepareScopeHandoffResponseParentScopeId = "root",
      PA.prepareScopeHandoffResponseChildScopeId = "architecture",
      PA.prepareScopeHandoffResponseSourceWorktree = "/tmp/repo",
      PA.prepareScopeHandoffResponseBranch = "main",
      PA.prepareScopeHandoffResponseHeadBefore = "before-sha",
      PA.prepareScopeHandoffResponseHeadAfter = "before-sha",
      PA.prepareScopeHandoffResponseCommitSha = "",
      PA.prepareScopeHandoffResponseSourceFingerprint = "source-fingerprint",
      PA.prepareScopeHandoffResponseChangedPaths = V.fromList ["4348414e47454c4f472e6d64", "646f63732f6e6f74652e6d64"],
      PA.prepareScopeHandoffResponseReady = True
    }

responseValue :: Value
responseValue =
  object
    [ "success" .= True,
      "error" .= ("" :: Text),
      "outcome" .= ("ready" :: Text),
      "handoff_id" .= ("handoff-1" :: Text),
      "controller_id" .= ("root-controller" :: Text),
      "parent_run_id" .= ("root-run" :: Text),
      "parent_scope_id" .= ("root" :: Text),
      "child_scope_id" .= ("architecture" :: Text),
      "source_worktree" .= ("/tmp/repo" :: Text),
      "branch" .= ("main" :: Text),
      "head_before" .= ("before-sha" :: Text),
      "head_after" .= ("before-sha" :: Text),
      "commit_sha" .= ("" :: Text),
      "source_fingerprint" .= ("source-fingerprint" :: Text),
      "changed_paths" .= (["4348414e47454c4f472e6d64", "646f63732f6e6f74652e6d64"] :: [Text]),
      "ready" .= True
    ]

successEnvelope :: Value
successEnvelope =
  toJSON
    ( BS.unpack
        ( BL.toStrict
            ( toLazyByteString
                ( Envelope.EffectResponse
                    ( Just
                        ( Envelope.EffectResponseResultPayload
                            (BL.toStrict (toLazyByteString successfulResponse))
                        )
                    )
                )
            )
        ) ::
        [Word8]
    )

runCoreWith :: Value -> Either Text Value
runCoreWith hostResponse =
  run $ runC (prepareScopeHandoffCore args) >>= handle
  where
    handle (Done result) = pure result
    handle (Continue request resume)
      | erType request == "agent.prepare_scope_handoff" = resume hostResponse >>= handle
      | otherwise = pure (Left "unexpected effect request")

field :: Key.Key -> Value -> Maybe Value
field key (Object values) = KeyMap.lookup key values
field _ _ = Nothing
