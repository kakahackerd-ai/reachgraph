package main

import "testing"

func TestComputeMaintainerOverlapsFindsSharedMaintainer(t *testing.T) {
	pkgMaintainers := map[string][]string{
		"evil-pkg":  {"attacker99", "legit-dev"},
		"clean-pkg": {"attacker99"},
		"other-pkg": {"someone-else"},
	}
	findings := computeMaintainerOverlaps(pkgMaintainers, []string{"evil-pkg"})
	if len(findings) != 1 {
		t.Fatalf("expected 1 finding, got %d: %+v", len(findings), findings)
	}
	f := findings[0]
	if f.Package != "evil-pkg" || f.Maintainer != "attacker99" {
		t.Fatalf("unexpected finding: %+v", f)
	}
	if len(f.AlsoMaintains) != 1 || f.AlsoMaintains[0] != "clean-pkg" {
		t.Fatalf("expected AlsoMaintains=[clean-pkg], got %+v", f.AlsoMaintains)
	}
}

func TestComputeMaintainerOverlapsNoOverlapNoFinding(t *testing.T) {
	pkgMaintainers := map[string][]string{
		"evil-pkg":  {"attacker99"},
		"clean-pkg": {"someone-else"},
	}
	findings := computeMaintainerOverlaps(pkgMaintainers, []string{"evil-pkg"})
	if len(findings) != 0 {
		t.Fatalf("expected no findings when no maintainer overlaps, got %+v", findings)
	}
}

func TestComputeMaintainerOverlapsOnlyReportsFlaggedPackages(t *testing.T) {
	// clean-a and clean-b share a maintainer too, but neither is flagged —
	// that's not an actionable finding for this scan, so it must not appear.
	pkgMaintainers := map[string][]string{
		"evil-pkg": {"attacker99"},
		"clean-a":  {"shared-dev"},
		"clean-b":  {"shared-dev"},
	}
	findings := computeMaintainerOverlaps(pkgMaintainers, []string{"evil-pkg"})
	if len(findings) != 0 {
		t.Fatalf("expected no findings — the overlap involves no flagged package, got %+v", findings)
	}
}

func TestComputeMaintainerOverlapsMultipleSharedPackages(t *testing.T) {
	pkgMaintainers := map[string][]string{
		"evil-pkg": {"attacker99"},
		"pkg-a":    {"attacker99"},
		"pkg-b":    {"attacker99"},
	}
	findings := computeMaintainerOverlaps(pkgMaintainers, []string{"evil-pkg"})
	if len(findings) != 1 {
		t.Fatalf("expected 1 finding (one maintainer), got %d: %+v", len(findings), findings)
	}
	if len(findings[0].AlsoMaintains) != 2 {
		t.Fatalf("expected both pkg-a and pkg-b listed, got %+v", findings[0].AlsoMaintains)
	}
}
