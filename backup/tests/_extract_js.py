"""Write the dashboard's <script> content to dash_c.js (for `node --check`) and check for stray bytes."""
import glob
import os
import re

WT = r"D:\integ-final"
s = open(os.path.join(WT, "static", "index.html"), encoding="utf-8").read()
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dash_c.js")
open(out, "w", encoding="utf-8").write("\n".join(re.findall(r"<script>(.*?)</script>", s, re.S)))
bad = [f for f in glob.glob(os.path.join(WT, "app", "*.py")) + [os.path.join(WT, "static", "index.html")]
       if b"\x08" in open(f, "rb").read()]
print("extracted; files with a backspace byte:", bad or "none")
for name in ("kr-q", "kr-go", "kr-out", "e-knote", "e-chips", "e-klegend", "e-krow", "e-kq", "e-kw", "e-meta", "e-kprev",
             "e-mtn", "e-mgn", "e-mt", "e-md", "e-mg"):
    n = len(re.findall(r'id="' + re.escape(name) + r'"', s))
    if n != 1:
        print("ID PROBLEM:", name, "appears", n, "times")
