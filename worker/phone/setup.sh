#!/data/data/com.termux/files/usr/bin/bash
# Raji Studio phone helper: one-time setup on an Android phone, inside Termux (installed from F-Droid).
#
#   curl -fsSL https://YOUR-SERVER/helper/setup.sh | bash -s -- https://YOUR-SERVER
#
# It asks for the account's helper key (dashboard: My channels -> Helper PC -> Show -> Copy key) and keeps it in
# ~/raji-helper/enroll_key.txt (only you can read it): the phone then joins by itself, nothing to approve. The key is
# typed at the prompt (hidden), never on the command line, so it is not in the shell history.
# No key (Enter): a customer's phone asks for the helper code and is approved with a pairing code, as before.
# Running it again is safe: it updates everything and restarts the helper.
set -u
SERVER="${1:-https://social-media-automation-production-f5543fa3.up.railway.app}"
SERVER="${SERVER%/}"
DIR="$HOME/raji-helper"
BOOT="$HOME/.termux/boot"

case "$SERVER" in
  https://*) ;;
  *) echo "The server address must start with https:// - copy the command again from the dashboard."; exit 1 ;;
esac

say() { printf '\n== %s\n' "$1"; }

mkdir -p "$DIR" "$BOOT"
KEY_RX='^rk[0-9]{1,9}[.][0-9a-f]{8}[.][A-Za-z0-9_-]{43}$'   # exactly the shape the server makes
KEY=""
if [ -r /dev/tty ]; then
  echo "Helper key (dashboard: My channels -> Helper PC -> Show -> Copy key). Paste it and press Enter"
  printf '(nothing is shown; just Enter if this phone has one already or you use a helper code): '
  read -r -s KEY </dev/tty || KEY=""
  echo
  KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
fi
if [ -n "$KEY" ]; then
  if ! [[ "$KEY" =~ $KEY_RX ]]; then
    echo "This helper key does not look right - copy it again from the dashboard (Show -> Copy key)."; exit 1
  fi
  (umask 077; printf '%s\n' "$KEY" > "$DIR/enroll_key.txt")
  chmod 600 "$DIR/enroll_key.txt"
fi
if [ -z "$KEY" ] && [ ! -s "$DIR/enroll_key.txt" ] && [ ! -s "$DIR/helper_code.txt" ] && [ -r /dev/tty ]; then
  printf 'Helper code (only for a customer'"'"'s phone; the owner just presses Enter): '
  CODE=""
  read -r CODE </dev/tty || CODE=""
  if [ -n "$CODE" ]; then printf '%s\n' "$CODE" > "$DIR/helper_code.txt"; chmod 600 "$DIR/helper_code.txt"; fi
fi

say "1/6 Installing Python, ffmpeg and node (a few minutes the first time)"
yes | pkg upgrade -y >/dev/null 2>&1 || true
pkg install -y python ffmpeg nodejs-lts curl termux-tools || { echo "pkg install failed - check the internet and run this again"; exit 1; }

say "2/6 Installing httpx and yt-dlp"
pip install --upgrade --quiet httpx yt-dlp yt-dlp-ejs \
  || pip install --upgrade --quiet --break-system-packages httpx yt-dlp yt-dlp-ejs \
  || { echo "pip install failed - run this again"; exit 1; }

say "3/6 Fetching the helper from $SERVER"
printf '%s\n' "$SERVER" > "$DIR/server.txt"
curl -fsSL --max-time 120 "$SERVER/helper/phone.py" -o "$DIR/phone.py" || { echo "Could not reach $SERVER"; exit 1; }

say "4/6 Writing the start script (it fetches the newest helper on every start)"
cat > "$DIR/start.sh" <<'EOS'
#!/data/data/com.termux/files/usr/bin/bash
# Keeps the phone helper running: newest phone.py on every start, yt-dlp updated when a run was long,
# restart after a crash (waiting longer after each crash in a row, at most 30 minutes).
DIR="$HOME/raji-helper"
cd "$DIR" || exit 1
termux-wake-lock 2>/dev/null
SERVER="$(head -n 1 server.txt)"
wait_s=30
last_pip=0
while true; do
  now=$(date +%s)
  if [ $((now - last_pip)) -gt 3600 ]; then   # a quick restart skips the (slow) yt-dlp update
    pip install --upgrade --quiet --timeout 20 --retries 1 yt-dlp yt-dlp-ejs >/dev/null 2>&1 \
      || pip install --upgrade --quiet --timeout 20 --retries 1 --break-system-packages yt-dlp yt-dlp-ejs >/dev/null 2>&1
    last_pip=$now
  fi
  if curl -fsSL --max-time 120 "$SERVER/helper/phone.py" -o phone.py.new \
     && python -c "compile(open('phone.py.new','rb').read(),'phone.py','exec')" 2>/dev/null; then
    mv -f phone.py.new phone.py
  else
    rm -f phone.py.new   # offline or a broken download: keep the helper that is already here
  fi
  started=$(date +%s)
  python phone.py "$SERVER" >/dev/null 2>last-error.txt   # its own log is helper.log (small, rotating)
  code=$?
  if [ "$code" = "5" ] || [ "$code" = "6" ]; then exit 0; fi   # 5: already running, 6: needs you (see helper.log)
  if [ "$code" = "4" ]; then last_pip=0; wait_s=30; sleep 2; continue; fi   # 4: asked for an update
  if [ $(( $(date +%s) - started )) -gt 3600 ]; then wait_s=30; fi        # it had run fine for a while
  sleep "$wait_s"                                                          # a crash: wait, longer each time
  wait_s=$((wait_s * 2)); if [ "$wait_s" -gt 1800 ]; then wait_s=1800; fi
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
  if [ -f "$DIR/helper.log" ] && grep -q "pairing code\|Waiting for jobs\|running for\|nothing to approve" "$DIR/helper.log"; then break; fi
done
echo
tail -n 5 "$DIR/helper.log" 2>/dev/null
CODE6="$(grep -o 'pairing code: [0-9]\{6\}' "$DIR/helper.log" 2>/dev/null | tail -n 1 | grep -o '[0-9]\{6\}')"
echo
if grep -q "nothing to approve" "$DIR/helper.log" 2>/dev/null; then
  echo "Connected by itself - nothing to approve. It appears in the dashboard under My channels -> Helper PC."
elif [ -n "$CODE6" ]; then
  echo "Now open the dashboard -> My channels -> Helper PC -> Approve, and type: $CODE6"
else
  echo "The helper is running. See $DIR/helper.log"
fi
echo "Keep the phone on the charger and on Wi-Fi. Turn battery optimisation OFF for Termux and Termux:Boot."
