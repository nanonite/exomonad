module Main where

import ExoMonad.Guest.Tools.Chainlink.PureTest (pureTests)
import ExoMonad.Guest.Tools.EventsTest (eventsTests)
import ExoMonad.Guest.Tools.PollWorkersTest (pollWorkersTests)
import ExoMonad.Guest.Tools.ResumePrTest (resumePrTests)
import Test.Tasty (defaultMain, testGroup)

main :: IO ()
main = defaultMain $ testGroup "WASM guest" [pureTests, eventsTests, resumePrTests, pollWorkersTests]
