# ClipForge — Setup Guide

This gets both halves of the business running on Railway:

1. **Clipping service** — turn clients' long videos into captioned short clips, send them a preview link, collect leads from your website.
2. **Archive Autopilot** — turns public-domain newsreels into narrated YouTube Shorts, long-form YouTube videos and TikTok drafts, on a schedule.

Read `docs/COMPLIANCE.md` once. It explains the licensing and platform rules the autopilot is built around.

---

## 1. Deploy on Railway (15 minutes)

1. Push this repo to GitHub and create a Railway project from it. Railway builds the `Dockerfile` automatically.
2. **Add a volume** (Railway → your service → Settings → Volumes) mounted at `/data`.
   Without it, every redeploy deletes your clients, clips and videos.
3. **Variables** (Railway → Variables):

| Variable | Required | What it is |
|---|---|---|
| `DATA_DIR` | yes | `/data` (the volume path) |
| `ADMIN_PASSWORD` | yes | Password for the dashboard. Make it long. |
| `SECRET_KEY` | yes | Any long random string (signs login cookies). |
| `PUBLIC_BASE_URL` | yes | Your app's URL, e.g. `https://clipforge-production.up.railway.app` (no trailing slash). |
| `GROQ_API_KEY` | yes | Free key from https://console.groq.com — writes clip picks and narration. |
| `CONTACT_EMAIL` | optional | Email shown on the site and in outreach drafts. |
| `BUSINESS_NAME` | optional | Defaults to `ClipForge`. |
| `YOUTUBE_COOKIES` / `PROXY_URL` | optional | Only if YouTube blocks downloads of client videos. |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | optional | Backup AI providers if Groq is down. |

4. Deploy, then open `PUBLIC_BASE_URL`. You should see the public sales site.
   Team login is at `/login` and takes you to `/dashboard`.

> If you ever forget to set `ADMIN_PASSWORD`, the app generates one and prints it in the Railway logs. It never runs unprotected.

---

## 2. Clipping service — daily workflow

- **Get leads:** share your site link. The form on `/` drops leads into the dashboard (**Leads** in the top bar). Click **→ Make client** on a lead.
- **Make free samples:** select the client → **Demo** (3 × 20s clips) or paste a video link → **Process**.
  Face tracking keeps the speaker in frame in vertical clips; toggle it under options.
- **Review:** approve the good clips. Use **Edit** to trim or restyle captions; the clip re-renders from the original.
- **Send:** **📧 Draft email** writes the pitch with a private preview link to the approved clips already in it. Copy, paste, send.
- **Paying clients:** ask for original files (upload under **Upload file**) for full quality.

Edit prices on the public site in `static/landing.html` (search for `pricing`).

---

## 3. Archive Autopilot

Open `/autopilot`. The **Setup checklist** shows what's missing. Do these in order:

### 3a. YouTube (about 20 minutes, plus Google's audit wait)

1. Create the channel (a Brand Account is best) and turn on 2-Step Verification for the Google account.
2. Go to https://console.cloud.google.com → create a project → **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **OAuth consent screen**: External, app name "ClipForge", your email, add the scopes
   `youtube.upload` and `youtube.readonly`, add your Google account as a test user,
   privacy policy URL `PUBLIC_BASE_URL/privacy`, terms URL `PUBLIC_BASE_URL/terms`.
4. **Credentials → Create OAuth client ID** → Web application →
   Authorized redirect URI: `PUBLIC_BASE_URL/api/oauth/youtube/callback`.
5. Put the client ID and secret in Railway as `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET`, redeploy.
6. On `/autopilot` click **Connect YouTube** and pick your channel.
7. **Submit the API audit** (YouTube API Services audit and quota extension form:
   https://support.google.com/youtube/contact/yt_api_form). Until it's approved, Google locks every
   API upload to **private**. The dashboard tells you when this happens. Uploads still work, they just aren't public.
   While you wait, you can publish the private videos by hand in YouTube Studio.

### 3b. TikTok (about 20 minutes, plus TikTok's review)

1. Use a **Personal** TikTok account. The Creator Rewards Program doesn't accept Business accounts.
2. https://developers.tiktok.com → create an app → add **Login Kit** and **Content Posting API**.
   Scopes: `user.info.basic`, `video.upload`. Redirect URI: `PUBLIC_BASE_URL/api/oauth/tiktok/callback`.
   Privacy policy / terms URLs: `PUBLIC_BASE_URL/privacy`, `PUBLIC_BASE_URL/terms`. Submit for review.
3. Put `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET` in Railway, redeploy, click **Connect TikTok**.
4. How posting works: each video arrives in your **TikTok inbox as a draft**. Open TikTok, tap the
   notification, paste the caption (click **Copy caption** on the dashboard) and post.
   TikTok's rules don't allow fully automatic public posting through their API. This one tap is the compliant way.

### 3c. Pick a voice and test

1. Under **Settings**, try a few **Narrator voice** numbers with ▶ (0–903 are different LibriTTS speakers) and save the one you like.
2. Press **▶ Make an episode now**. In about 5–10 minutes you get a Short, a TikTok version and a widescreen chapter. Watch them.
3. When you're happy, flip the **Autopilot** switch ON.

### What the autopilot does on its own

- Finds newsreels from the same calendar week in past years ("this week in 1941"). Only footage whose own license
  metadata is public domain, CC0 or CC BY gets through; everything else is rejected and logged.
  Graphic or distressing topics are skipped.
- Writes original narration from the newsreel's own description and soundtrack (no invented facts),
  records it with the free LibriTTS voice, and drops the original audio.
- Renders a vertical Short (under 60s), a longer TikTok cut (over 61s, the minimum for Creator Rewards) and a widescreen chapter.
  Opening title cards with the studio's logo are cut automatically.
- Every few days, joins 5 chapters into a long-form video with intro, outro and YouTube chapter timestamps.
- Posts on a randomized schedule inside your posting window: by default 2 Shorts and 1 TikTok draft a day, and 2 long-form videos a week.
- Credits the source, license and voice in every description.
- Retries failed uploads, backs off when it hits a quota, and pauses for an hour after 3 failed renders in a row.
- The **Autopilot** switch is the kill switch: off means nothing is made or posted.

### If a video gets a copyright claim

Public-domain newsreels often get false Content ID claims. On the dashboard, click **Copyright-claim reply**
on that video. It gives you ready-to-paste dispute text with the license evidence. In YouTube Studio choose
**Dispute → Public domain**. Only dispute claims on footage the autopilot sourced. Abusing disputes is penalized.

### Music (optional)

Add CC0 tracks to `music/` with a `.license.txt` next to each (see `music/README.md`). With no music, videos use narration only, which is perfectly fine.

---

## 4. Monetization milestones

| Platform | Requirement |
|---|---|
| YouTube Partner Program | 1,000 subscribers + 4,000 long-form watch hours (12 months) **or** 10M Shorts views (90 days). From **Feb 1, 2027**: 8,000 hours or 20M Shorts views. |
| TikTok Creator Rewards | 18+, 10,000 followers, 100,000 views in 30 days, Personal account, videos over 1 minute. |

When eligible: link an AdSense account and add tax info (W-9) in AdSense for YouTube. Apply for Creator Rewards in the TikTok app.

---

## 5. Local development

```bash
pip install -r requirements-dev.txt
# fonts: copy fonts/*.ttf to ~/.fonts (or /usr/share/fonts) and run fc-cache
# voice: download en_US-libritts-high.onnx + .json into ./voices (URLs in the Dockerfile)
ADMIN_PASSWORD=dev python main.py      # http://localhost:8000
python -m pytest tests                 # test suite
```
