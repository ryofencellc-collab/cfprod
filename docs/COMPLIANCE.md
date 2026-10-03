# ClipForge Autopilot — Legal & Monetization Spec

Researched October 2026. This is the rulebook the autopilot is built to follow.
Platform rules change; re-check the linked sources every few months.
This is not legal advice — have an IP lawyer review before scaling.

Channel plan: **history / newsreels**, posting to **YouTube (long-form + Shorts)**
and **TikTok**, with a **free, commercially-licensed TTS voice**.

---

## 1. Footage — only use what we can prove is free

| Source | Status | Rule in code |
|---|---|---|
| Universal Newsreels (1929–1967), archive.org `universal_newsreels` | MCA/Universal deeded rights to the US Government in 1974 (public domain). Some stories may contain underlying third-party rights (e.g. music). | Allowed. Replace/strip original audio. |
| archive.org items with `licenseurl` = Public Domain Mark, CC0, or old CC "publicdomain" | Public domain / dedication | Allowed |
| archive.org items with `licenseurl` = CC BY or CC BY-SA | Free with credit | Allowed, attribution auto-added to description |
| Prelinger Archives | Only ~65% public domain — "DO NOT ASSUME" | Allowed **only** if the individual item carries a PD/CC0 `licenseurl` |
| Anything NC (non-commercial) or ND (no-derivatives) | Not usable for monetized edits | Rejected |
| Items with no license metadata | Unknown | Rejected |
| US works published 1930 or earlier | Public domain in the US as of 1 Jan 2026 | Still requires metadata proof; date alone isn't enough |

Day-1 requirements:
- **Provenance record per clip**: source URL, identifier, license URL, collection, date fetched, and a saved copy of the metadata JSON. Used to dispute false Content ID claims.
- **Original audio removed or replaced**. Newsreel soundtracks can contain separately copyrighted music. We use our own narration + CC0 music only.
- **No famous characters/logos** as the focus (trademarks survive copyright expiry).
- **Credit line** in every description, e.g. "Footage: Universal Newsreel, 1941 (public domain), via Internet Archive / National Archives."

## 2. YouTube

**Monetization (YPP)** — current entry: 1,000 subs + 4,000 watch hours (12 mo) **or** 1,000 subs + 10M Shorts views (90 days).
**From 1 Feb 2027** new applicants need 1,000 subs + **8,000** watch hours or **20M** Shorts views, plus ongoing activity
(e.g. 2 long-form or 5 Shorts every 90 days). Shorts-feed views don't count toward watch hours → **long-form matters**.

**Content rules that kill monetization** (reviewed at channel level; one bad pattern can remove the whole channel from YPP):
- *Reused content*: footage "without any substantive modifications"; clips "edited together with little or no narrative".
  Allowed: "edited footage … where you add a storyline and commentary". → **every video must have original narration/storyline.**
- *Inauthentic / mass-produced* (July 2025, clarified July 2026): "similar or repetitive content with low educational value",
  "AI-generated content made with generic or unoriginal templates giving the impression of mass production".
  → **vary structure, hooks, length, and topic; no single fixed template; cap daily volume.**
- *Emotionally manipulative / distressing content made to farm views* is ineligible → no shock-bait framing of war/disaster footage.

**AI disclosure**: a generic AI voiceover is production assistance and does **not** require the "altered or synthetic" label.
Required only for realistic depictions of real people saying/doing things they didn't (voice clones, deepfakes).
→ We never clone real voices or fake historical speech. `status.containsSyntheticMedia` is set if any future step generates realistic imagery.

**API (automation)**:
- Automated uploads to **our own channel** are permitted by the developer policies.
- Projects created after 28 Jul 2020 upload **private-only until Google's compliance audit passes** → apply for audit on day 1.
- Set `status.selfDeclaredMadeForKids = false` explicitly on every upload.
- `videos.insert` costs quota from the "Video Uploads" bucket; respect quota and back off on errors.

**Content ID**: false claims on public-domain newsreels are common. Dispute with the provenance record, citing "public domain".
Only dispute when the record is clean — abusive disputes are penalized.

**Payouts**: AdSense account linked to the channel, US tax info (W-9) in AdSense, 2-step verification on the Google account.

## 3. TikTok

**Automation — the big constraint**: TikTok's Direct Post guidelines require the user to see a preview, **manually pick the
privacy level (no default)**, and expressly consent before upload. **Unattended auto-posting is not permitted.**
Unaudited apps post private-only and are limited to 5 users/24h.

→ Compliant design: use the **Upload (inbox/draft) API** (`/v2/post/publish/inbox/video/init/`, `video.upload` scope).
The video lands in the TikTok inbox as a draft; one tap in the TikTok app publishes it. Everything else stays automatic.

**Creator Rewards Program (monetization)**: 18+, 10,000 followers, 100,000 views in the last 30 days, **Personal account
(not Business)**, US/UK/DE/JP/KR/FR/MX/BR. Paid videos must be **over 1 minute**, original, and get 1,000+ qualified FYF views.
Payout rate is weighted by originality, watch time, search value and engagement.
→ TikTok cut must be **61–90 s**; keep the account Personal.

**Distribution**: reused/unoriginal content without creative edits is ineligible for the For You feed; visible third-party
watermarks are a named example → **no archive/third-party logos on screen; our own edit, narration and captions.**
AI-generated realistic scenes/people need the AI label — not applicable to narration over real footage.

## 4. Voice & music licenses

- **Voice**: Piper `en_US-libritts-high` — trained from scratch on LibriTTS (CC BY 4.0) → commercial use OK with attribution.
  **Do not use** `lessac`, `amy`, `ryan`, `hfc_*` or any voice fine-tuned from lessac (non-commercial datasets).
  Attribution line in descriptions: "Narration voice: Piper / LibriTTS (CC BY 4.0)."
- **Music**: only CC0 or explicitly commercial-OK tracks dropped into `music/`, each with its license file. No music is better than unlicensed music.

## 5. Guardrails the autopilot enforces

1. License gate — reject any source not matching section 1.
2. Originality gate — reject a video if narration is missing, too short, or the script repeats a recent one.
3. Volume caps — default 1 long-form/day + 2 Shorts/day + 1 TikTok draft/day, randomized posting times.
4. Fact safety — scripts are written only from the item's own archive description; no invented quotes, names or numbers.
5. Sensitive-topic filter — no sensational framing of deaths, disasters or atrocities.
6. Kill switch — one toggle stops all generation and posting.
7. Audit log — every post stores its provenance record, script, and platform response.

## 6. Day-1 checklist (owner actions)

- [ ] Create YouTube channel (brand account) + enable 2-step verification
- [ ] Google Cloud project → enable YouTube Data API v3 → OAuth client → **submit API compliance audit**
- [ ] AdSense account + W-9 (once eligible)
- [ ] TikTok **Personal** account; TikTok developer app with `video.upload` scope (+ privacy policy URL)
- [ ] Free Groq API key for script writing
- [ ] Railway volume mounted for database + media
- [ ] Optional but recommended: LLC + 1-hour IP-lawyer review of this document

## Sources
- YouTube monetization policies (reused / inauthentic content): https://support.google.com/youtube/answer/1311392
- YPP threshold changes (Feb 2027): https://support.google.com/youtube/answer/12843009
- YouTube API developer policies: https://developers.google.com/youtube/terms/developer-policies
- Videos: insert (private-until-audit, quota, status fields): https://developers.google.com/youtube/v3/docs/videos/insert
- Content ID disputes: https://support.google.com/youtube/answer/2797454
- TikTok Content Sharing (Direct Post) guidelines: https://developers.tiktok.com/docs/en/content-sharing-guidelines
- TikTok Upload (draft) API: https://developers.tiktok.com/docs/en/content-posting-api-reference-upload-video
- TikTok Creator Rewards: https://www.tiktok.com/creator-academy/article/creator-rewards-program
- TikTok integrity & authenticity: https://www.tiktok.com/community-guidelines/en/integrity-authenticity
- Universal Newsreels rights: https://en.wikipedia.org/wiki/Universal_Newsreel , https://www.newyorkalmanack.com/2022/10/universal-newsreels-in-the-national-archives/
- Prelinger rights: https://help.archive.org/help/prelinger-archive/
- Public Domain Day 2026: https://campuspress.yale.edu/copyrightconversations/public-domain-day-2026/
- Piper voice model cards: https://huggingface.co/rhasspy/piper-voices
