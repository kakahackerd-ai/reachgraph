package main

import "testing"

func TestExtractCodeStructureJS(t *testing.T) {
	content := `
import { debounce } from 'lodash';
const express = require('express');

export function createServer(port) {
  return null;
}

class RequestHandler {
  handle() {}
}

const middleware = (req, res, next) => {
  next();
};
`
	symbols, imports := extractCodeStructure("npm", "src/server.js", content)

	wantSymbols := map[string]string{"createServer": "function", "RequestHandler": "class", "middleware": "function"}
	if len(symbols) != len(wantSymbols) {
		t.Fatalf("expected %d symbols, got %d: %+v", len(wantSymbols), len(symbols), symbols)
	}
	for _, s := range symbols {
		if wantSymbols[s.Name] != s.Kind {
			t.Errorf("unexpected symbol %+v", s)
		}
	}

	wantImports := map[string]bool{"lodash": true, "express": true}
	if len(imports) != len(wantImports) {
		t.Fatalf("expected %d imports, got %d: %+v", len(wantImports), len(imports), imports)
	}
	for _, imp := range imports {
		if !wantImports[imp.Module] {
			t.Errorf("unexpected import %+v", imp)
		}
	}
}

func TestExtractCodeStructurePython(t *testing.T) {
	content := `
import requests
from collections import OrderedDict

def fetch_data(url):
    return requests.get(url)

class ApiClient:
    def __init__(self):
        pass
`
	symbols, imports := extractCodeStructure("pypi", "app/client.py", content)

	wantSymbols := map[string]string{"fetch_data": "function", "ApiClient": "class", "__init__": "function"}
	if len(symbols) != len(wantSymbols) {
		t.Fatalf("expected %d symbols, got %d: %+v", len(wantSymbols), len(symbols), symbols)
	}

	wantImports := map[string]bool{"requests": true, "collections": true}
	if len(imports) != len(wantImports) {
		t.Fatalf("expected %d imports, got %d: %+v", len(wantImports), len(imports), imports)
	}
}

func TestExtractCodeStructureDeduplicatesImports(t *testing.T) {
	content := `
import requests
import requests
from requests import Session
`
	_, imports := extractCodeStructure("pypi", "x.py", content)
	if len(imports) != 1 {
		t.Fatalf("expected deduped single import, got %+v", imports)
	}
}

func TestExtractCodeStructureEmptyFileNoSymbols(t *testing.T) {
	symbols, imports := extractCodeStructure("npm", "README.md", "# hello\nno code here\n")
	if len(symbols) != 0 || len(imports) != 0 {
		t.Fatalf("expected no symbols/imports for prose content, got %+v %+v", symbols, imports)
	}
}
