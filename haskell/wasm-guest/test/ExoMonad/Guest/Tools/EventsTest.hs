{-# LANGUAGE OverloadedStrings #-}

module ExoMonad.Guest.Tools.EventsTest (eventsTests) where

import Data.Aeson (Value (..), decode, encode, object, (.=))
import Data.Aeson.KeyMap qualified as KM
import Data.ByteString.Lazy.Char8 qualified as L8
import Data.List (isInfixOf)
import Data.Text qualified as T
import ExoMonad.Guest.Tools.Events
  ( NotifyParentArgs (..),
    NotifyStatus (..),
    notifyParentSchema,
  )
import Test.Tasty (TestTree, testGroup)
import Test.Tasty.HUnit (assertBool, testCase, (@?=))

eventsTests :: TestTree
eventsTests =
  testGroup
    "Typed blocked notify_parent contract"
    [ testCase "blocked handoff round-trips with stable JSON fields" $ do
        let value = validValue
        case (decode (encode value) :: Maybe NotifyParentArgs) of
          Nothing -> fail "valid blocked handoff did not parse"
          Just args -> decode (encode args) @?= Just args,
      testCase "unknown blocked cause is rejected" $
        (decode (encode (withBlockedCause "not-a-cause")) :: Maybe NotifyParentArgs) @?= Nothing,
      testCase "missing structured evidence is rejected" $
        (decode (encode (withoutEvidence validValue)) :: Maybe NotifyParentArgs) @?= Nothing,
      testCase "success with blocked fields is rejected" $
        (decode (encode (withStatus "success" validValue)) :: Maybe NotifyParentArgs) @?= Nothing,
      testCase "live schema exposes blocked status and evidence" $ do
        let schema = encode notifyParentSchema
        assertBool "schema must expose blocked" (isInfixOf "\"blocked\"" (L8.unpack schema))
        assertBool "schema must expose evidence" (isInfixOf "\"evidence\"" (L8.unpack schema)),
      testCase "status enum includes blocked" $
        decode (encode Blocked) @?= Just Blocked
    ]

validValue :: Value
validValue =
  object
    [ "status" .= ("blocked" :: String),
      "message" .= ("base CI is unstable" :: String),
      "blocked"
        .= object
          [ "cause" .= ("base_ci_unstable" :: String),
            "needs_human" .= True,
            "scope_attribution" .= ("base" :: String),
            "retryable" .= True,
            "recovery_action" .= ("rebase after CI repair" :: String),
            "evidence"
              .= object
                [ "base_sha" .= ("base-sha" :: String),
                  "head_sha" .= ("head-sha" :: String),
                  "failed_checks" .= (["ci/test"] :: [String]),
                  "evidence_summary" .= ("failure is reproducible on the base" :: String)
                ]
          ]
    ]

withBlockedCause :: String -> Value
withBlockedCause cause =
  case validValue of
    Object fields -> Object (KM.insert "blocked" (object ["cause" .= cause]) fields)
    _ -> validValue

withoutEvidence :: Value -> Value
withoutEvidence value =
  case value of
    Object fields -> Object (KM.insert "blocked" (object []) fields)
    _ -> value

withStatus :: String -> Value -> Value
withStatus status value =
  case value of
    Object fields -> Object (KM.insert "status" (String (T.pack status)) fields)
    _ -> value
