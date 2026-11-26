import glob, os, re, subprocess
WT = r"D:\integ-final"
s = open(os.path.join(WT, "static", "index.html"), encoding="utf-8").read()
blocks = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", s, re.S)
tags = re.findall(r"<script[^>]*>", s)
print("script tags:", tags)
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dash_final.js")
open(out, "w", encoding="utf-8").write("\n".join(blocks))
print("blocks:", len(blocks), "chars:", sum(map(len, blocks)))
files = glob.glob(os.path.join(WT, "app", "*.py")) + glob.glob(os.path.join(WT, "static", "*.html"))
bad = [f for f in files if b"\x08" in open(f, "rb").read()]
print("files checked for 0x08:", len(files), "bad:", bad or "none")
tracked = subprocess.run(["git", "-C", WT, "ls-files", "-z"], capture_output=True).stdout.decode().split("\0")
mk = []
for rel in filter(None, tracked):
    p = os.path.join(WT, rel)
    try: data = open(p, "rb").read()
    except OSError as e: print("unreadable", rel, e); continue
    for i, line in enumerate(data.split(b"\n"), 1):
        if re.match(rb"^(<{7}|>{7}|\|{7})( |$|\r)", line) or re.match(rb"^={7}\r?$", line):
            mk.append((rel, i, line[:60]))
print("tracked files:", len(tracked) - 1, "merge-marker lines:", mk or "none")
