package main

import "strings"

// This file answers one of Track 02's specific questions directly: "Are
// there likely typosquat packages nearby?" It's deliberately NOT built on
// embedding/semantic similarity — the track's own thesis is that this class
// of problem is a graph/string-distance problem, not a semantic one, and a
// name like "expres" being one deletion away from "express" is exactly the
// kind of thing edit distance catches precisely and embeddings catch only
// approximately.
//
// popularNpmPackages / popularPyPIPackages are a curated reference list of
// long-standing, broadly-known packages in each ecosystem — the realistic
// targets an attacker would typosquat, not an exhaustive registry dump.
// There is no single "top N" endpoint either registry publishes for bulk
// retrieval, so this is maintained by hand rather than fabricated from a
// nonexistent API.

var popularNpmPackages = []string{
	"react", "react-dom", "lodash", "express", "axios", "chalk", "commander",
	"request", "moment", "webpack", "eslint", "jest", "typescript", "vue",
	"next", "redux", "jquery", "bootstrap", "socket.io", "mongoose",
	"sequelize", "nodemailer", "dotenv", "cors", "body-parser", "morgan",
	"underscore", "async", "bluebird", "rxjs", "classnames", "prop-types",
	"styled-components", "graphql", "apollo-client", "puppeteer", "cheerio",
	"uuid", "js-yaml", "yargs", "minimist", "glob", "rimraf", "mkdirp",
	"semver", "chokidar", "debug", "colors", "figlet", "inquirer", "ora",
	"yup", "zod", "joi", "ajv", "winston", "pino", "helmet", "passport",
	"jsonwebtoken", "bcrypt", "multer", "sharp", "nodemon", "concurrently",
	"cross-env", "husky", "lint-staged", "prettier", "rollup", "vite",
	"esbuild", "parcel", "gulp", "grunt", "d3", "three", "chart.js",
	"date-fns", "dayjs", "immer", "zustand", "formik", "react-router",
	"react-query", "next-auth", "tailwindcss", "postcss", "autoprefixer",
	"sass", "typeorm", "prisma", "knex", "pg", "mysql2", "redis", "ioredis",
	"aws-sdk", "googleapis", "stripe", "twilio",
}

var popularPyPIPackages = []string{
	"requests", "numpy", "pandas", "flask", "django", "boto3", "click",
	"pytest", "setuptools", "urllib3", "six", "pyyaml", "cryptography",
	"certifi", "idna", "python-dateutil", "jinja2", "markupsafe", "packaging",
	"wheel", "attrs", "pluggy", "colorama", "typing-extensions", "protobuf",
	"grpcio", "sqlalchemy", "pydantic", "fastapi", "uvicorn", "starlette",
	"httpx", "aiohttp", "beautifulsoup4", "lxml", "pillow", "matplotlib",
	"scipy", "scikit-learn", "tensorflow", "torch", "transformers", "openai",
	"langchain", "celery", "redis", "psycopg2", "pymongo", "gunicorn",
	"tornado", "twisted", "paramiko", "cffi", "pycryptodome", "pyjwt",
	"oauthlib", "google-auth", "botocore", "s3transfer", "sphinx", "black",
	"flake8", "mypy", "tox", "coverage", "mock", "faker", "pytz",
}

type typosquatFinding struct {
	Package   string `json:"package"`
	SimilarTo string `json:"similarTo"`
	Distance  int    `json:"distance"`
}

func normalizeForTyposquat(name string) string { return strings.ToLower(name) }

// typosquatThreshold scales with name length: short names ("d3", "six")
// have very little room before an edit distance of 2 makes them coincide
// with an unrelated short name, so they need a tighter bar than long ones.
func typosquatThreshold(name string) int {
	if len(name) <= 4 {
		return 1
	}
	return 2
}

// checkTyposquat flags any name that sits within edit distance of a
// well-known package without being an exact match to it.
func checkTyposquat(ecosystem string, names []string) []typosquatFinding {
	popular := popularNpmPackages
	if ecosystem == "pypi" {
		popular = popularPyPIPackages
	}
	popularSet := make(map[string]bool, len(popular))
	for _, p := range popular {
		popularSet[p] = true
	}

	var findings []typosquatFinding
	seen := map[string]bool{}
	for _, raw := range names {
		n := normalizeForTyposquat(raw)
		if seen[n] || popularSet[n] {
			continue // already reported, or it IS the popular package
		}
		seen[n] = true

		bestDist := -1
		bestMatch := ""
		for _, p := range popular {
			d := damerauLevenshtein(n, p)
			if d <= typosquatThreshold(p) && (bestDist == -1 || d < bestDist) {
				bestDist = d
				bestMatch = p
			}
		}
		if bestDist >= 0 {
			findings = append(findings, typosquatFinding{Package: raw, SimilarTo: bestMatch, Distance: bestDist})
		}
	}
	return findings
}

// damerauLevenshtein computes edit distance allowing insertion, deletion,
// substitution, and adjacent-character transposition (optimal string
// alignment variant) — the transposition case matters here specifically
// because "epxress"/"exprses"-style swapped-letter typos are one of the
// most common real typosquat patterns, and plain Levenshtein charges two
// edits for what a human typo made in one slip.
func damerauLevenshtein(a, b string) int {
	ra, rb := []rune(a), []rune(b)
	la, lb := len(ra), len(rb)
	if la == 0 {
		return lb
	}
	if lb == 0 {
		return la
	}

	d := make([][]int, la+1)
	for i := range d {
		d[i] = make([]int, lb+1)
		d[i][0] = i
	}
	for j := 0; j <= lb; j++ {
		d[0][j] = j
	}

	for i := 1; i <= la; i++ {
		for j := 1; j <= lb; j++ {
			cost := 1
			if ra[i-1] == rb[j-1] {
				cost = 0
			}
			del := d[i-1][j] + 1
			ins := d[i][j-1] + 1
			sub := d[i-1][j-1] + cost
			best := del
			if ins < best {
				best = ins
			}
			if sub < best {
				best = sub
			}
			if i > 1 && j > 1 && ra[i-1] == rb[j-2] && ra[i-2] == rb[j-1] {
				if trans := d[i-2][j-2] + cost; trans < best {
					best = trans
				}
			}
			d[i][j] = best
		}
	}
	return d[la][lb]
}
