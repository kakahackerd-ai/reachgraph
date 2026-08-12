package main

import "testing"

func TestDamerauLevenshteinBasics(t *testing.T) {
	cases := []struct {
		a, b string
		want int
	}{
		{"express", "express", 0},
		{"expres", "express", 1},  // one deletion
		{"exprsss", "express", 1}, // one substitution
		{"exprses", "express", 1}, // one adjacent transposition
		{"", "abc", 3},
		{"abc", "", 3},
	}
	for _, c := range cases {
		if got := damerauLevenshtein(c.a, c.b); got != c.want {
			t.Errorf("damerauLevenshtein(%q, %q) = %d, want %d", c.a, c.b, got, c.want)
		}
	}
}

// Real typosquat patterns seen in the wild: a missing letter, a doubled
// letter, and a swapped pair — all one edit away from a well-known package.
func TestCheckTyposquatCatchesRealPatterns(t *testing.T) {
	names := []string{"expres", "reqeusts", "lodahs"}
	findings := checkTyposquat("npm", names)
	got := map[string]string{}
	for _, f := range findings {
		got[f.Package] = f.SimilarTo
	}
	if got["expres"] != "express" {
		t.Errorf("expected expres -> express, got %+v", got)
	}
	if got["lodahs"] != "lodash" {
		t.Errorf("expected lodahs -> lodash, got %+v", got)
	}
}

func TestCheckTyposquatDoesNotFlagThePopularPackageItself(t *testing.T) {
	findings := checkTyposquat("npm", []string{"express", "lodash", "react"})
	if len(findings) != 0 {
		t.Fatalf("expected no findings for exact matches to popular packages, got %+v", findings)
	}
}

// Real, legitimately-different short package names must not collide just
// because they're short — this is exactly why the threshold scales down
// for short names instead of using a flat distance-2 cutoff everywhere.
func TestCheckTyposquatDoesNotFlagUnrelatedShortNames(t *testing.T) {
	findings := checkTyposquat("npm", []string{"vitest", "yup-lite", "d3-scale"})
	for _, f := range findings {
		if f.Package == "vitest" {
			t.Errorf("did not expect vitest to be flagged as a typosquat, got %+v", f)
		}
	}
}

func TestCheckTyposquatPyPIEcosystem(t *testing.T) {
	findings := checkTyposquat("pypi", []string{"reqeusts", "numpy"})
	got := map[string]string{}
	for _, f := range findings {
		got[f.Package] = f.SimilarTo
	}
	if got["reqeusts"] != "requests" {
		t.Errorf("expected reqeusts -> requests, got %+v", got)
	}
	if _, flagged := got["numpy"]; flagged {
		t.Error("numpy is itself popular and should not be flagged")
	}
}

func TestCheckTyposquatDeduplicatesCaseInsensitively(t *testing.T) {
	findings := checkTyposquat("npm", []string{"Expres", "expres"})
	if len(findings) != 1 {
		t.Fatalf("expected case-insensitive dedup to produce 1 finding, got %d: %+v", len(findings), findings)
	}
}
