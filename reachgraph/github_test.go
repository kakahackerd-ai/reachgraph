package main

import "testing"

func TestExtractDependenciesPrefersLockfileVersion(t *testing.T) {
	pkg := packageJSON{Dependencies: map[string]string{"express": "^4.17.0"}}
	lock := npmLockfile{Packages: map[string]struct {
		Version string `json:"version"`
	}{
		"node_modules/express": {Version: "4.19.2"},
	}}

	deps := extractDependencies(pkg, lock, true)
	if len(deps) != 1 {
		t.Fatalf("expected 1 dependency, got %d", len(deps))
	}
	d := deps[0]
	if !d.FromLock || d.Version != "4.19.2" {
		t.Fatalf("expected lockfile-exact version 4.19.2, got %+v", d)
	}
}

// Real bug found while testing against lodash/lodash: its committed
// package-lock.json is lockfileVersion 1, which uses a flat "dependencies"
// map instead of the "packages" map lockfileVersion 2/3 uses. A lockfile
// being present must not silently fall through to the range-approximation
// path just because it's in the older format.
func TestExtractDependenciesReadsLockfileVersion1Format(t *testing.T) {
	pkg := packageJSON{Dependencies: map[string]string{"lodash": "^3.0.0"}}
	lock := npmLockfile{Dependencies: map[string]struct {
		Version string `json:"version"`
	}{
		"lodash": {Version: "3.10.1"},
	}}

	deps := extractDependencies(pkg, lock, true)
	if len(deps) != 1 || !deps[0].FromLock || deps[0].Version != "3.10.1" {
		t.Fatalf("expected lockfileVersion-1-style resolution to 3.10.1, got %+v", deps)
	}
}

func TestExtractDependenciesFallsBackToStrippedRangeWithoutLockfile(t *testing.T) {
	pkg := packageJSON{Dependencies: map[string]string{"express": "^4.17.0"}}
	deps := extractDependencies(pkg, npmLockfile{}, false)
	if len(deps) != 1 {
		t.Fatalf("expected 1 dependency, got %d", len(deps))
	}
	d := deps[0]
	if d.FromLock {
		t.Fatalf("did not expect FromLock with no lockfile")
	}
	if d.Version != "4.17.0" {
		t.Fatalf("expected stripped range 4.17.0, got %q", d.Version)
	}
	if d.NeedsLatest {
		t.Fatalf("a resolvable stripped version should not need latest-tag fallback")
	}
}

func TestExtractDependenciesUnresolvableRangeNeedsLatest(t *testing.T) {
	for _, rng := range []string{"*", "x", ""} {
		pkg := packageJSON{Dependencies: map[string]string{"whatever": rng}}
		deps := extractDependencies(pkg, npmLockfile{}, false)
		if len(deps) != 1 || !deps[0].NeedsLatest {
			t.Fatalf("range %q: expected NeedsLatest=true, got %+v", rng, deps)
		}
	}
}

func TestExtractDependenciesMarksDevDependencies(t *testing.T) {
	pkg := packageJSON{
		Dependencies:    map[string]string{"express": "4.19.2"},
		DevDependencies: map[string]string{"jest": "29.0.0"},
	}
	deps := extractDependencies(pkg, npmLockfile{}, false)
	if len(deps) != 2 {
		t.Fatalf("expected 2 dependencies, got %d", len(deps))
	}
	var sawDev, sawProd bool
	for _, d := range deps {
		if d.Name == "jest" && d.Dev {
			sawDev = true
		}
		if d.Name == "express" && !d.Dev {
			sawProd = true
		}
	}
	if !sawDev || !sawProd {
		t.Fatalf("expected one dev and one prod dependency correctly flagged, got %+v", deps)
	}
}

func TestExtractDependenciesDeterministicOrder(t *testing.T) {
	pkg := packageJSON{Dependencies: map[string]string{"zeta": "1.0.0", "alpha": "1.0.0", "mid": "1.0.0"}}
	deps := extractDependencies(pkg, npmLockfile{}, false)
	want := []string{"alpha", "mid", "zeta"}
	for i, w := range want {
		if deps[i].Name != w {
			t.Fatalf("expected sorted order %v, got %v", want, deps)
		}
	}
}
