# Phone helper (Android)

An old Android phone at home can do the downloading for Raji Studio. It stays on the charger and on Wi-Fi.
It can work for every account, so new customers do not need a helper of their own.

What it does: it downloads the part of each YouTube video that a clip needs and sends it to the server. It also
reads the "most replayed" graph and the top comments of a video.
What it does not do: reels, voice-overs, AI scripts and automatic captions. The Windows helper PC still does
those. A clip from the phone goes up without automatic captions.

## What you need

- An Android phone (Android 7 or newer), on the charger, on home Wi-Fi.
- About 400 MB of free space.
- 15 minutes.

## Steps (one time)

1. **Install F-Droid.** On the phone, open f-droid.org in the browser, download F-Droid and install it.
   If Android asks, allow "install unknown apps" for the browser.
2. **In F-Droid, install two apps: "Termux" and "Termux:Boot".**
   Do not use the Play Store version of Termux. It is old and does not work.
3. **Open Termux:Boot once.** Just open it and close it. This lets the helper start by itself after a restart.
4. **Turn battery optimisation off** for Termux and for Termux:Boot:
   Settings → Apps → Termux → Battery → "Unrestricted" (or "Don't optimise"). Do the same for Termux:Boot.
   On Xiaomi, Oppo, Vivo, Realme and Samsung phones also turn on "Autostart" / "Allow background activity" for both.
5. **Copy the command.** In the dashboard open My channels → Helper PC → tab **Android phone** → press **Copy**.
   Send it to the phone (for example in WhatsApp to yourself) and copy it there.
6. **Open Termux, paste the command, press Enter.** It looks like this:

   ```
   curl -fsSL https://YOUR-SERVER/helper/setup.sh | bash -s -- https://YOUR-SERVER
   ```

   Copy it from My channels → Helper PC → **Show** (Android phone). It first asks for your **helper key**: press
   **Copy key** in the dashboard and paste it (nothing is shown while you paste). The key is never on the command
   line, so it is not in Termux's history. Then it takes a few minutes. If it asks another question, press Enter.
7. **Nothing to approve.** At the end Termux says "Connected by itself - nothing to approve" and the phone shows
   in the dashboard. (Pressed Enter instead of giving a key: it asks for a helper code and ends with a 6-digit pairing
   code: then press **Approve** on the new "Android phone" and type it.)
8. **Super Admin only: make it the central helper.** In the same list press **Make central**. From now on the
   phone also downloads for every customer who has no helper of their own, and for customers who switched on
   "Use the shared helper when my PC is off" while their PC is off. It only downloads and analyses for them.
   (A helper that is too old cannot be made central; start it once so it updates itself.)
9. **Leave the phone alone.** Keep it on the charger and on Wi-Fi. You can close the Termux window; a small
   Termux notification stays — that is the helper running.

## Every day

Nothing. The helper:
- starts by itself when the phone restarts (Termux:Boot),
- fetches the newest helper from the server every time it starts, and once a day updates yt-dlp,
- restarts by itself if it ever stops,
- waits patiently when the internet is gone and carries on when it is back.

## If something is wrong

- **Dashboard says the helper is offline:** check the phone is on, on Wi-Fi, and the Termux notification is there.
  If not, open Termux and type `bash ~/raji-helper/start.sh &` then press Enter.
- **See what it is doing:** in Termux type `tail -n 30 ~/raji-helper/helper.log`.
- **Stop it:** `bash ~/raji-helper/stop.sh`.
- **Set it up again from the start:** paste the command from step 6 again. It is safe.
- **Removed it in the dashboard by mistake:** it does not join again by itself (that is what Remove means); it
  waits with a pairing code — press **Approve** and type it.
- **Wi-Fi dropped / phone was asleep:** it looks at the network every 30 s and calls in as soon as it is back.

## Size

- The helper itself: one file of about 19 KB.
- httpx + yt-dlp (+ yt-dlp's small JavaScript part): about 26 MB once installed.
- Inside Termux, roughly (not measured on a phone): Python about 100 MB, ffmpeg about 100 MB, node about 60 MB.
- The apps Termux and Termux:Boot: roughly 100 MB together.
Videos are deleted from the phone as soon as they are sent.
