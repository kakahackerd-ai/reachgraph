"""Reverse-dependency ("who depends on this package") data source for Flow 1.

Neither npm nor PyPI expose a public reverse-dependency API. deps.dev's v3alpha
API has a `:dependents` method, but it only returns counts
(dependentCount/directDependentCount/indirectDependentCount) -- verified by
hand, `GET /v3alpha/systems/npm/packages/lodash/versions/4.17.21:dependents`
returns `{"dependentCount":22552,...}` with no dependent names. The only free,
no-API-key source of actual dependent names is GitHub's public
`network/dependents` page -- see github_scrape.py for what that involves.
"""
