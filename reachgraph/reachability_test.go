package main

import "testing"

func TestImportPatternMatchesRequireCall(t *testing.T) {
	re := importPattern("npm", "chalk")
	cases := []string{
		`const chalk = require('chalk');`,
		`const chalk = require("chalk").default;`,
		`require('chalk/index.js')`,
	}
	for _, c := range cases {
		if !re.MatchString(c) {
			t.Errorf("expected match for %q", c)
		}
	}
}

func TestImportPatternMatchesESImports(t *testing.T) {
	re := importPattern("npm", "chalk")
	cases := []string{
		`import chalk from 'chalk';`,
		`import { red } from "chalk";`,
		`import 'chalk';`,
		`const chalk = await import('chalk');`,
	}
	for _, c := range cases {
		if !re.MatchString(c) {
			t.Errorf("expected match for %q", c)
		}
	}
}

// The exact bug this pattern is designed to avoid: a naive substring search
// for "chalk" would also match "chalk-template" or "my-chalk-wrapper",
// falsely reporting a completely different package as reachable.
func TestImportPatternDoesNotFalseMatchPrefixedOrSuffixedPackage(t *testing.T) {
	re := importPattern("npm", "chalk")
	cases := []string{
		`import x from 'chalk-template';`,
		`require('my-chalk-wrapper')`,
		`// this file talks about chalk colors but never imports it`,
	}
	for _, c := range cases {
		if re.MatchString(c) {
			t.Errorf("expected NO match for %q", c)
		}
	}
}

func TestImportPatternMatchesScopedPackage(t *testing.T) {
	re := importPattern("npm", "@babel/core")
	if !re.MatchString(`import { transform } from '@babel/core';`) {
		t.Error("expected match for scoped package import")
	}
}

func TestImportPatternPyPIMatchesImportAndFrom(t *testing.T) {
	re := importPattern("pypi", "requests")
	yes := []string{
		"import requests",
		"import requests.models",
		"from requests import Session",
		"from requests.models import Response",
		"    import requests  # indented, inside a function",
	}
	for _, c := range yes {
		if !re.MatchString(c) {
			t.Errorf("expected match for %q", c)
		}
	}
}

// Same false-match class as the npm case, but for Python's word-boundary
// rules: "requests" must not match inside "requests_oauthlib" or
// "requests-toolbelt".
func TestImportPatternPyPIDoesNotFalseMatchRelatedPackage(t *testing.T) {
	re := importPattern("pypi", "requests")
	no := []string{
		"import requests_oauthlib",
		"from requests_toolbelt import MultipartEncoder",
		"# this module talks about requests but never imports it",
	}
	for _, c := range no {
		if re.MatchString(c) {
			t.Errorf("expected NO match for %q", c)
		}
	}
}

func TestIsCandidateSourceFileNpmFiltersExtensionsAndBuildDirs(t *testing.T) {
	yes := []string{"src/index.js", "lib/util.ts", "components/App.tsx", "server.mjs"}
	no := []string{"dist/bundle.js", "node_modules/foo/index.js", "build/out.js", "README.md", "package.json", "coverage/lcov.js"}
	for _, p := range yes {
		if !isCandidateSourceFile("npm", p) {
			t.Errorf("expected %q to be a candidate source file", p)
		}
	}
	for _, p := range no {
		if isCandidateSourceFile("npm", p) {
			t.Errorf("expected %q to be excluded", p)
		}
	}
}

func TestIsCandidateSourceFilePyPIFiltersExtensionsAndVenvDirs(t *testing.T) {
	yes := []string{"app/main.py", "package/__init__.py", "tests/test_x.py"}
	no := []string{"venv/lib/foo.py", ".venv/bin/x.py", "site-packages/foo/bar.py", "__pycache__/mod.cpython-311.pyc", "README.md", "requirements.txt"}
	for _, p := range yes {
		if !isCandidateSourceFile("pypi", p) {
			t.Errorf("expected %q to be a candidate source file", p)
		}
	}
	for _, p := range no {
		if isCandidateSourceFile("pypi", p) {
			t.Errorf("expected %q to be excluded", p)
		}
	}
}

func TestAdjustScoreForCodeReachabilityConfirmedReachedKeepsScore(t *testing.T) {
	base := computeRiskScore("HIGH", 1)
	adjusted := adjustScoreForCodeReachability(base, reachabilityFinding{Checked: true, Reached: true, Evidence: "imported in src/index.js"})
	if adjusted.Value != base.Value {
		t.Fatalf("confirmed-reachable should not change the score: base=%d adjusted=%d", base.Value, adjusted.Value)
	}
}

func TestAdjustScoreForCodeReachabilityConfirmedNotReachedLowersScore(t *testing.T) {
	base := computeRiskScore("HIGH", 1)
	adjusted := adjustScoreForCodeReachability(base, reachabilityFinding{Checked: true, Reached: false, Evidence: "not imported in any of 40 scanned source files"})
	if adjusted.Value >= base.Value {
		t.Fatalf("confirmed-unreachable should lower the score: base=%d adjusted=%d", base.Value, adjusted.Value)
	}
}

func TestAdjustScoreForCodeReachabilityUncheckedLeavesScoreUntouched(t *testing.T) {
	base := computeRiskScore("HIGH", 1)
	adjusted := adjustScoreForCodeReachability(base, reachabilityFinding{Checked: false})
	if adjusted.Value != base.Value || len(adjusted.Factors) != len(base.Factors) {
		t.Fatalf("unchecked reachability must leave the score exactly as computed")
	}
}
