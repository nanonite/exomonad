{-# LANGUAGE OverloadedStrings #-}

module ExoMonad.Guest.Tools.UnlinkedChangelogTest (unlinkedChangelogTests) where

import Control.Monad.Freer (run)
import Control.Monad.Freer.Coroutine (Status (Continue, Done), runC)
import Data.Aeson (Value (..), toJSON)
import Data.Aeson.Key qualified as Key
import Data.Aeson.KeyMap qualified as KM
import Data.ByteString qualified as BS
import Data.ByteString.Lazy qualified as BL
import Data.Int (Int32)
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Word (Word8)
import Effects.Envelope qualified as Envelope
import Effects.Process qualified as Proc
import ExoMonad.Guest.Tool.Suspend.Types (EffectRequest (..))
import ExoMonad.Guest.Tools.UnlinkedChangelog
  ( UnlinkedChangelogContextArgs (..),
    UnlinkedChangelogEntryArgs (..),
    entryCommitArgs,
    extractUnreleased,
    insertChangelogEntry,
    intentTrailer,
    unlinkedChangelogContextCore,
    unlinkedChangelogEntryCore,
    validateEntryText,
  )
import Proto3.Suite.Class (toLazyByteString)
import Test.Tasty (TestTree, testGroup)
import Test.Tasty.HUnit (assertBool, assertEqual, testCase)

unlinkedChangelogTests :: TestTree
unlinkedChangelogTests =
  testGroup
    "unlinked changelog boundaries"
    [ testCase "extracts the [Unreleased] body up to the next release" $
        assertEqual
          "unreleased body"
          (Just "### Fixed\n- Fix a thing (#1)")
          (extractUnreleased sample),
      testCase "a CHANGELOG without [Unreleased] has no section" $
        assertEqual "missing" Nothing (extractUnreleased "# Changelog\n\n## [0.1.0]\n- x\n"),
      testCase "prepends to an existing section" $
        assertEqual
          "prepended"
          (Right "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- Fix the new thing\n- Fix a thing (#1)\n\n## [0.1.0] - 2026-01-01\n\n### Added\n- First\n")
          (insertChangelogEntry "Fixed" "Fix the new thing" sample),
      testCase "creates a missing section before a later canonical section" $
        assertEqual
          "added before fixed"
          (Right "# Changelog\n\n## [Unreleased]\n\n### Added\n- Add the new thing\n\n### Fixed\n- Fix a thing (#1)\n\n## [0.1.0] - 2026-01-01\n\n### Added\n- First\n")
          (insertChangelogEntry "Added" "Add the new thing" sample),
      testCase "appends a missing section that sorts last" $
        assertEqual
          "security after fixed"
          (Right "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- Fix a thing (#1)\n\n### Security\n- Patch the hole\n\n## [0.1.0] - 2026-01-01\n\n### Added\n- First\n")
          (insertChangelogEntry "Security" "Patch the hole" sample),
      testCase "fills an empty [Unreleased] section" $
        assertEqual
          "first entry"
          (Right "# Changelog\n\n## [Unreleased]\n\n### Changed\n- Change it\n\n## [0.1.0]\n- x\n")
          (insertChangelogEntry "Changed" "Change it" "# Changelog\n\n## [Unreleased]\n\n## [0.1.0]\n- x\n"),
      testCase "refuses a CHANGELOG without [Unreleased]" $
        assertEqual
          "no unreleased"
          (Left "CHANGELOG.md has no ## [Unreleased] section")
          (insertChangelogEntry "Added" "x" "# Changelog\n\n## [0.1.0]\n- x\n"),
      testCase "refuses unknown sections and malformed entries" $ do
        assertBool "unknown section" (isLeft (insertChangelogEntry "Misc" "x" sample))
        assertBool "multi-line" (isLeft (validateEntryText "a\nb"))
        assertBool "list marker" (isLeft (validateEntryText "- a"))
        assertBool "heading" (isLeft (validateEntryText "# a"))
        assertBool "padded" (isLeft (validateEntryText " a"))
        assertBool "too long" (isLeft (validateEntryText (T.replicate 301 "a")))
        assertBool "valid" (validateEntryText "Add a thing (#12)" == Right ()),
      testCase "the entry commit carries the intent trailer" $
        assertBool
          "trailer"
          (intentTrailer "intent-1" `elem` entryCommitArgs "slice-a" "intent-1"),
      testCase "writes, commits, and pushes a new entry" $ do
        let result =
              runEntry
                [ ok "main",
                  ok "base",
                  ok sample,
                  ok "",
                  ok "",
                  ok "entry",
                  ok "base\trefs/heads/main",
                  ok "",
                  ok "entry\trefs/heads/main"
                ]
        assertEqual "outcome" (Just "entry_added") (field "outcome" result)
        assertEqual "commit" (Just "entry") (field "commit_sha" result)
        assertEqual "remote" (Just "entry") (field "observed_remote_head" result),
      testCase "a rejected push resets the parent and reports entry_push_failed" $ do
        let result =
              runEntry
                [ ok "main",
                  ok "base",
                  ok sample,
                  ok "",
                  ok "",
                  ok "entry",
                  ok "other\trefs/heads/main",
                  failed "stale info",
                  ok ""
                ]
        assertEqual "outcome" (Just "entry_push_failed") (field "outcome" result)
        assertEqual "no commit" Nothing (field "commit_sha" result),
      testCase "a failed commit restores CHANGELOG.md and reports entry_write_failed" $ do
        let result = runEntry [ok "main", ok "base", ok sample, ok "", failed "nothing to commit", ok ""]
        assertEqual "outcome" (Just "entry_write_failed") (field "outcome" result),
      testCase "a parent that moved is a write failure, not a guess" $ do
        let result = runEntry [ok "main", ok "elsewhere", ok "not-base", ok "msg"]
        assertEqual "outcome" (Just "entry_write_failed") (field "outcome" result),
      testCase "an already pushed entry for this intent is reported without another push" $ do
        let result =
              runEntry
                [ ok "main",
                  ok "entry",
                  ok "base",
                  ok ("chore: record changelog for merged slice slice-a\n\n" <> intentTrailer "intent-1"),
                  ok "entry\trefs/heads/main"
                ]
        assertEqual "outcome" (Just "entry_added") (field "outcome" result)
        assertEqual "commit" (Just "entry") (field "commit_sha" result),
      testCase "invalid entry arguments are rejected before any Git call" $
        assertBool
          "left"
          (isLeft (runEntryWith entryArgs {uceSection = "Misc"} [])),
      testCase "context reports the unreleased section, paths, and changelog edits" $ do
        let result =
              runContext
                [ ok "main",
                  ok "base",
                  ok "CHANGELOG.md",
                  ok sample,
                  ok "src/a.hs\nCHANGELOG.md"
                ]
        assertEqual "present" (Just (Bool True)) (fieldValue "changelog_present" result)
        assertEqual "section" (Just "### Fixed\n- Fix a thing (#1)") (field "unreleased_section" result)
        assertEqual "edited" (Just (Bool True)) (fieldValue "pr_edited_changelog" result)
        assertEqual
          "paths"
          (Just (toJSON ["src/a.hs" :: Text, "CHANGELOG.md"]))
          (fieldValue "changed_paths" result),
      testCase "context without a CHANGELOG reports it absent" $ do
        let result = runContext [ok "main", ok "base", ok "", ok "src/a.hs"]
        assertEqual "absent" (Just (Bool False)) (fieldValue "changelog_present" result)
        assertEqual "no section" (Just Null) (fieldValue "unreleased_section" result),
      testCase "context refuses a parent that moved after synchronization" $
        assertBool "left" (isLeft (runContext [ok "main", ok "moved"]))
    ]

sample :: Text
sample =
  "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- Fix a thing (#1)\n\n## [0.1.0] - 2026-01-01\n\n### Added\n- First\n"

entryArgs :: UnlinkedChangelogEntryArgs
entryArgs =
  UnlinkedChangelogEntryArgs "slice-a" "org/repo" "main" "base" "intent-1" "Fixed" "Fix the new thing" Nothing

contextArgs :: UnlinkedChangelogContextArgs
contextArgs = UnlinkedChangelogContextArgs "slice-a" "main" "base" 43 (Just "merge") Nothing

runEntry :: [Value] -> Either Text Value
runEntry = runEntryWith entryArgs

runEntryWith :: UnlinkedChangelogEntryArgs -> [Value] -> Either Text Value
runEntryWith args responses =
  run $ runC (unlinkedChangelogEntryCore args) >>= handle 0
  where
    handle _ (Done result) = pure result
    handle processCount (Continue request resume) =
      resume (responseFor responses processCount request)
        >>= handle (if erType request == "process.run" then processCount + 1 else processCount)

runContext :: [Value] -> Either Text Value
runContext responses =
  run $ runC (unlinkedChangelogContextCore contextArgs) >>= handle 0
  where
    handle _ (Done result) = pure result
    handle processCount (Continue request resume) =
      resume (responseFor responses processCount request)
        >>= handle (if erType request == "process.run" then processCount + 1 else processCount)

responseFor :: [Value] -> Int -> EffectRequest -> Value
responseFor responses processCount request
  | erType request /= "process.run" = encodedEffectResponse BS.empty
  | processCount < length responses = responses !! processCount
  | otherwise = processResponse 1 "" "unexpected process request"

field :: Text -> Either Text Value -> Maybe Text
field key result = case fieldValue key result of
  Just (String value) -> Just value
  _ -> Nothing

fieldValue :: Text -> Either Text Value -> Maybe Value
fieldValue key (Right (Object fields)) = KM.lookup (Key.fromText key) fields
fieldValue _ _ = Nothing

isLeft :: Either a b -> Bool
isLeft (Left _) = True
isLeft _ = False

ok :: Text -> Value
ok stdout = processResponse 0 stdout ""

failed :: Text -> Value
failed = processResponse 1 ""

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
            ( toLazyByteString
                (Envelope.EffectResponse (Just (Envelope.EffectResponseResultPayload payload)))
            )
        ) ::
        [Word8]
    )
