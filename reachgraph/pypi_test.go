package main

import "testing"

func findDep(deps []resolvedDependency, name string) (resolvedDependency, bool) {
	for _, d := range deps {
		if d.Name == name {
			return d, true
		}
	}
	return resolvedDependency{}, false
}

func TestExtractPyPIDependenciesExactPin(t *testing.T) {
	deps := extractPyPIDependencies([]byte("Flask==3.0.3\n"))
	d, ok := findDep(deps, "flask") // PEP 503 normalized to lowercase
	if !ok {
		t.Fatal("expected flask to be parsed")
	}
	if d.Version != "3.0.3" || d.NeedsLatest {
		t.Fatalf("expected exact pin 3.0.3, got %+v", d)
	}
}

func TestExtractPyPIDependenciesRangeNeedsLatest(t *testing.T) {
	deps := extractPyPIDependencies([]byte("requests>=2.25.0,<3.0\n"))
	d, ok := findDep(deps, "requests")
	if !ok {
		t.Fatal("expected requests to be parsed")
	}
	if !d.NeedsLatest || d.Version != "" {
		t.Fatalf("expected a range specifier to need latest-resolution, got %+v", d)
	}
}

func TestExtractPyPIDependenciesBareNameNeedsLatest(t *testing.T) {
	deps := extractPyPIDependencies([]byte("numpy\n"))
	d, ok := findDep(deps, "numpy")
	if !ok {
		t.Fatal("expected numpy to be parsed")
	}
	if !d.NeedsLatest {
		t.Fatal("expected a bare package name to need latest-resolution")
	}
}

func TestExtractPyPIDependenciesSkipsCommentsBlankLinesAndOptions(t *testing.T) {
	content := []byte(`
# a comment
-e git+https://github.com/example/pkg.git
-r base.txt
--index-url https://example.com/simple

click~=8.1
`)
	deps := extractPyPIDependencies(content)
	if len(deps) != 1 || deps[0].Name != "click" {
		t.Fatalf("expected exactly one parsed dependency (click), got %+v", deps)
	}
}

func TestExtractPyPIDependenciesStripsExtrasAndEnvironmentMarkers(t *testing.T) {
	deps := extractPyPIDependencies([]byte(`requests[security]==2.25.0; python_version >= "3.6"` + "\n"))
	d, ok := findDep(deps, "requests")
	if !ok {
		t.Fatalf("expected requests to be parsed despite extras/markers, got %+v", deps)
	}
	if d.Version != "2.25.0" {
		t.Fatalf("expected exact version 2.25.0 after stripping extras/markers, got %+v", d)
	}
}

func TestExtractPyPIDependenciesStripsInlineComment(t *testing.T) {
	deps := extractPyPIDependencies([]byte("flask==3.0.3  # web framework\n"))
	d, ok := findDep(deps, "flask")
	if !ok || d.Version != "3.0.3" {
		t.Fatalf("expected flask==3.0.3 despite inline comment, got %+v ok=%v", d, ok)
	}
}

func TestNormalizePyPINamePEP503(t *testing.T) {
	cases := map[string]string{
		"Flask":             "flask",
		"python-dateutil":   "python-dateutil",
		"python_dateutil":   "python-dateutil",
		"Python...Dateutil": "python-dateutil",
	}
	for in, want := range cases {
		if got := normalizePyPIName(in); got != want {
			t.Errorf("normalizePyPIName(%q) = %q, want %q", in, got, want)
		}
	}
}
