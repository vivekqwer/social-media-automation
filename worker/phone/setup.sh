#!/data/data/com.termux/files/usr/bin/bash
# Raji Studio phone helper: one-time setup on an Android phone, inside Termux (installed from F-Droid).
#
#   curl -fsSL https://YOUR-SERVER/helper/setup.sh | bash -s -- https://YOUR-SERVER [HELPER_CODE]
#
# HELPER_CODE is only for a customer's own phone (My channels -> Helper PC shows it); the Super Admin's phone needs none.
# Running it again is safe: it updates everything and restarts the helper.
set -u
SERVER="${1:-https://social-media-automation-production-f5543fa3.up.railway.app}"
SERVER="${SERVER%/}"
CODE="${2:-}"
DIR="$HOME/raji-helper"
BOOT="$HOME/.termux/boot"

say() { printf '\n== %s\n' "$1"; }

say "1/6 Installing Python, ffmpeg and node (a few minutes the first time)"
yes | pkg upgrade -y >/dev/null 2>&1 || true
pkg install -y python ffmpeg nodejs-lts curl termux-tools || { echo "pkg install failed - check the internet and run this again"; exit 1; }

say "2/6 Installing httpx and yt-dlp"
pip install --upgrade --quiet httpx yt-dlp yt-dlp-ejs \
  || pip install --upgrade --quiet --break-system-packages httpx yt-dlp yt-dlp-ejs \
  || { echo "pip install failed - run this again"; exit 1; }

say "3/6 Fetching the helper from $SERVER"
mkdir -p "$DIR" "$BOOT"
printf '%s\n' "$SERVER" > "$DIR/server.txt"
if [ -n "$CODE" ]; then printf '%s\n' "$CODE" > "$DIR/helper_code.txt"; fi
curl -fsSL --max-time 120 "$SERVER/helper/phone.py" -o "$DIR/phone.py" || { echo "Could not reach $SERVER"; exit 1; }

say "4/6 Writing the start script (it fetches the newest helper on every start)"
cat > "$DIR/start.sh" <<'EOS'
#!/data/data/com.termux/files/usr/bin/bash
# Keeps the phone helper running: newest phone.py and yt-dlp on every start, restart after a crash.
DIR="$HOME/raji-helper"
cd "$DIR" || exit 1
termux-wake-lock 2>/dev/null
SERVER="$(head -n 1 server.txt)"
while true; do
  pip install --upgrade --quiet --timeout 20 --retries 1 yt-dlp yt-dlp-ejs >/dev/null 2>&1 \
    || pip install --upgrade --quiet --timeout 20 --retries 1 --break-system-packages yt-dlp yt-dlp-ejs >/dev/null 2>&1
  if curl -fsSL --max-time 120 "$SERVER/helper/phone.py" -o phone.py.new \
     && python -c "compile(open('phone.py.new','rb').read(),'phone.py','exec')" 2>/dev/null; then
    mv -f phone.py.new phone.py
  else
    rm -f phone.py.new   # offline or a broken download: keep the helper that is already here
  fi
  python phone.py "$SERVER" >/dev/null 2>last-error.txt   # its own log is helper.log (small, rotating)
  code=$?
  if [ "$code" = "5" ] || [ "$code" = "6" ]; then exit 0; fi   # 5: already running, 6: needs you (see helper.log)
  if [ "$code" = "4" ]; then sleep 2; else sleep 30; fi       # 4: asked for an update; anything else: a crash
done
EOS
chmod +x "$DIR/start.sh"
cat > "$DIR/stop.sh" <<'EOS'
#!/data/data/com.termux/files/usr/bin/bash
pkill -f raji-helper/start.sh; pkill -f "phone.py"; termux-wake-unlock 2>/dev/null; echo "Phone helper stopped."
EOS
chmod +x "$DIR/stop.sh"

say "5/6 Start by itself when the phone starts (needs the Termux:Boot app, opened once)"
cat > "$BOOT/start-helper.sh" <<'EOS'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
nohup bash "$HOME/raji-helper/start.sh" >/dev/null 2>&1 &
EOS
chmod +x "$BOOT/start-helper.sh"

say "6/6 Starting the helper"
termux-wake-lock 2>/dev/null || true
pkill -f raji-helper/start.sh 2>/dev/null; pkill -f "phone.py" 2>/dev/null; sleep 1
nohup bash "$DIR/start.sh" >/dev/null 2>&1 &
for i in $(seq 1 60); do
  sleep 2
  if [ -f "$DIR/helper.log" ] && grep -q "pairing code\|Waiting for jobs\|running for" "$DIR/helper.log"; then break; fi
done
echo
tail -n 5 "$DIR/helper.log" 2>/dev/null
CODE6="$(grep -o 'pairing code: [0-9]\{6\}' "$DIR/helper.log" 2>/dev/null | tail -n 1 | grep -o '[0-9]\{6\}')"
echo
if [ -n "$CODE6" ]; then
  echo "Now open the dashboard -> My channels -> Helper PC -> Approve, and type: $CODE6"
else
  echo "The helper is running. See $DIR/helper.log"
fi
echo "Keep the phone on the charger and on Wi-Fi. Turn battery optimisation OFF for Termux and Termux:Boot."
