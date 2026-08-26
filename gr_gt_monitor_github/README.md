# GR GT Monitor (Cloud version)

Runs entirely on GitHub's servers on a schedule. Nothing installs on your machine, nothing needs your browser open. A dashboard hosted for free shows the results, updated automatically every 2 hours.

## What you get vs the local HTML tool

This replaces the manual URL scanning and Reddit/YouTube search from the local tool with fully automatic, unattended pulls from Google News, Reddit, YouTube, and any forum RSS feeds you add. It does **not** replace the bookmarklet, login walled and JavaScript heavy forums still need that, since only your logged in browser can see that content. Use both together: this handles everything automatic, the bookmarklet still handles the stubborn forums.

## 1. Create the repository

Go to github.com, sign in (or create a free account), click the "+" in the top right, choose "New repository." Name it something like `gr-gt-monitor`. Set it to **Private** if you don't want the scan schedule visible to others (the dashboard can still be made public separately in step 4). Click "Create repository."

## 2. Upload these files

On your new repo's page, click "Add file" > "Upload files." Drag in this entire folder structure, keeping the folders intact:
```
.github/workflows/scan.yml
scripts/scan.py
docs/index.html
docs/data.json
data/forum_feeds.txt
```
GitHub's upload box accepts dragging a whole folder, it preserves the paths. Commit the upload.

## 3. Add your YouTube API key as a secret

Go to your repo's Settings tab > Secrets and variables > Actions > "New repository secret." Name it `YOUTUBE_API_KEY` and paste in the key you already have. This keeps it out of the actual code, only the scheduled job can read it.

## 4. Turn on GitHub Pages

Still in Settings, go to Pages. Under "Source," choose the `main` branch and the `/docs` folder, then Save. GitHub gives you a URL like `https://yourusername.github.io/gr-gt-monitor/`, that's your live dashboard. Bookmark it.

## 5. Turn on the schedule

Go to the Actions tab. GitHub sometimes disables workflows on first upload for safety, if you see a banner about enabling them, click it. Then click "GR GT Monitor Scan" in the left sidebar, and "Run workflow" to trigger it manually once and confirm it works. After that, it runs on its own every 2 hours, no action needed.

## 6. Add forum RSS feeds (optional)

Edit `data/forum_feeds.txt` directly on GitHub's website (click the file, click the pencil icon to edit), add one feed URL per line. Many forums (especially Discourse and XenForo based ones) publish these at predictable paths like `/latest.rss` or `/feed`, check a forum's page source for `<link type="application/rss+xml">` or just try common paths.

## Changing the schedule

The `0 */2 * * *` line in `scan.yml` means "every 2 hours." Edit that file on GitHub's website if you want it more or less frequent, standard cron syntax.

## Cost

Free. GitHub Actions gives generous free minutes monthly, this workflow takes under a minute to run, a couple hundred runs a month costs nothing.
