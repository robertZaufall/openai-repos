# openai-repos

`openai-repos` is a small static catalog of active OpenAI GitHub
repositories. It groups public OpenAI repositories into rough purpose clusters
and keeps GitHub metadata fresh with a scheduled update script.

Included repositories match this filter:

- GitHub org: `openai`
- More than 200 stars
- Non-fork and non-archived
- At least one default-branch commit within the previous three calendar months

The page is tuned to surface repositories with both recent traction and broad
audience: the lead table blends recent commits, freshness, stars, and forks,
while each purpose cluster is sorted by stars. Rows also include content
signals from each repository's README and tree, such as README sections, docs,
examples, tests, package manifests, notebooks, Docker files, OpenAPI specs, and
workflow files.

## Open locally

Open `index.html` directly in a browser, or run a local preview server:

```bash
python3 -m http.server 8000
```

Then visit `http://localhost:8000`.

## Update repository metadata

```bash
python3 update_stats.py
```

The script fetches stars, forks, total commits, recent commits, last commit
dates, primary language, topics, descriptions, README headings, and repository
tree content signals from GitHub. It rewrites the fresh-traction table, the
cluster tables, and `stats_history.json`.

For local runs, authenticate with the GitHub CLI or set `GITHUB_TOKEN`:

```bash
gh auth login
python3 update_stats.py
```

## Main files

- `index.html` - generated static catalog page.
- `update_stats.py` - GitHub metadata fetcher and HTML generator.
- `stats_history.json` - stored snapshots used for 30-day star and fork deltas.
- `favicon.svg` - OpenAI-themed favicon.
- `.github/workflows/static.yml` - deploys the static site to GitHub Pages.
- `.github/workflows/update-stats.yml` - scheduled catalog refresh.
