module Main where

import ExoMonad.Guest.Tools.Chainlink.PureTest (pureTests)
import Test.Tasty (defaultMain, testGroup)

main :: IO ()
main = defaultMain $ testGroup "Chainlink" [pureTests]
