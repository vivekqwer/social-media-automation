"""The folder of videos on this PC (D:\\youtube unless the dashboard says otherwise) -> the dashboard's "From my PC" list.

Every video in the folder — one per sub-folder (video.mp4 + thumbnail.jpg/png + description.txt + info.json), or a
loose .mp4 / .mov / .mkv — is reported to the server once, keyed by its sha256, after its size has not changed for
60 seconds. Only its details and a small picture are sent, never the video. The video itself is sent only when the
server asks for it ("pc_send", shortly before its upload), checked against the same sha256.
A moved or renamed file keeps its sha256 (it is not hashed again when size and time are unchanged), so it is the same
item; a deleted file is simply no longer in the list the server gets, which marks it "missing on the PC".
Nothing in the folder is ever changed, moved or deleted by the helper.
"""
import hashlib
import json
import os
import pathlib
import re
import subprocess
import time

VIDEO_EXT = (".mp4", ".mov", ".mkv")
SKIP_DIRS = {"tool", "tools", "bin", "tmp", "temp", "cache", "data", "logs", "node_modules", "venv", ".venv"}  # program folders
THUMB_NAMES = ("thumbnail.jpg", "thumbnail.jpeg", "thumbnail.png", "thumb.jpg", "thumb.jpeg", "thumb.png")
STABLE = 60  # seconds a new file's size (and time) must stay the same before it is reported
SCAN_EVERY = 30  # seconds between looks at the folder
FULL_EVERY = 900  # the whole list goes to the server at least this often (missing files)
HASH_BUDGET = 2 * 1024 ** 3  # bytes hashed in one look (a big first look is spread over a few rounds)
BATCH = 20  # videos per report
INFO_MAX = 256 * 1024
DESC_MAX = 20000
THUMB_MAX = 2 * 1024 * 1024  # YouTube's thumbnail limit


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _ffmpeg() -> str:
    try:
        from app.config import ffmpeg_bin

        return ffmpeg_bin()
    except Exception:  # noqa: BLE001
        return os.environ.get("FFMPEG_BIN") or "ffmpeg"


def probe(path) -> dict:
    """{duration, width, height} as far as ffmpeg can tell (0 when it cannot)."""
    try:
        r = subprocess.run([_ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace")
        err = r.stderr or ""
    except Exception:  # noqa: BLE001
        return {"duration": 0, "width": 0, "height": 0}
    out = {"duration": 0.0, "width": 0, "height": 0}
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err)
    if m:
        out["duration"] = round(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)), 2)
    m = re.search(r"Video:.*?[, ](\d{2,5})x(\d{2,5})[, \[]", err)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if re.search(r"rotation of -?(90|270)", err) or re.search(r"rotate\s*:\s*(90|270)", err):
            w, h = h, w
        out["width"], out["height"] = w, h
    return out


def frame_jpg(path, seconds: float, width: int = 480) -> bytes:
    """A small JPEG picture of the video (or of a large thumbnail file)."""
    import tempfile

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        out = pathlib.Path(d) / "f.jpg"
        cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"]
        if seconds:
            cmd += ["-ss", f"{seconds:.2f}"]
        cmd += ["-i", str(path), "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(out)]
        subprocess.run(cmd, capture_output=True, timeout=120)
        return out.read_bytes() if out.exists() else b""


def _read_text(p: pathlib.Path, cap: int) -> str:
    try:
        with open(p, "rb") as f:
            return f.read(cap).decode("utf-8-sig", errors="replace")
    except OSError:
        return ""


def read_info(folder: pathlib.Path | None):
    """(info.json as a dict or None, description.txt text). A broken info.json makes the video wait for a person."""
    if folder is None:
        return None, ""
    desc = _read_text(folder / "description.txt", DESC_MAX).strip() if (folder / "description.txt").is_file() else ""
    p = folder / "info.json"
    if not p.is_file():
        return None, desc
    try:
        if p.stat().st_size > INFO_MAX:
            raise ValueError("too large")
        raw = json.loads(_read_text(p, INFO_MAX) or "{}")
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        return raw, desc
    except (ValueError, OSError):
        return {"approve": "manual", "note": "info.json could not be read"}, desc


def find(root: pathlib.Path) -> list[dict]:
    """The videos in the folder: loose files at the top, and one per sub-folder (video.* first, else the largest)."""
    out = []
    try:
        names = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return out
    for p in names:
        n = p.name
        if n.startswith((".", "~$")) or n.lower().endswith((".part", ".tmp", ".crdownload")):
            continue
        try:
            if p.is_file() and p.suffix.lower() in VIDEO_EXT:
                out.append(_entry(root, p, None))
            elif p.is_dir() and not n.startswith("_") and n.lower() not in SKIP_DIRS:
                vids = [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXT and not f.name.startswith((".", "~$"))]
                if not vids:
                    continue
                named = [f for f in vids if f.stem.lower() == "video"]
                pick = named[0] if named else max(vids, key=lambda f: f.stat().st_size)
                out.append(_entry(root, pick, p))
        except OSError:
            continue
    return out


def _entry(root, path: pathlib.Path, folder: pathlib.Path | None) -> dict:
    st = path.stat()
    extra = []
    thumb = None
    if folder is not None:
        for name in ("info.json", "description.txt") + THUMB_NAMES:
            f = folder / name
            if f.is_file():
                fs = f.stat()
                extra.append([name, fs.st_size, fs.st_mtime_ns])
                if name in THUMB_NAMES and thumb is None:
                    thumb = f
    return {"rel": str(path.relative_to(root)), "path": path, "folder": folder.name if folder is not None else "",
            "folder_path": folder, "name": path.name, "size": st.st_size, "mtime": st.st_mtime_ns, "extra": extra,
            "thumb_path": thumb}


class Folder:
    """The watched folder and what this PC already told the server (kept in a small JSON file next to the helper's data)."""

    def __init__(self, state_file, log=print, now=time.time, prober=probe, framer=frame_jpg):
        self.state_file = pathlib.Path(state_file)
        self.log, self.now, self.probe, self.frame = log, now, prober, framer
        self.root: pathlib.Path | None = None
        self.last_scan, self.last_full, self.last_present, self.missing_said = 0.0, 0.0, None, False
        try:
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        for k in ("files", "shas", "sent"):
            self.state.setdefault(k, {})

    def save(self):
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_name(self.state_file.name + ".tmp")
            tmp.write_text(json.dumps(self.state), encoding="utf-8")
            os.replace(tmp, self.state_file)
        except OSError as e:
            self.log(f"PC folder: could not save its list ({str(e)[:80]})")

    def set_root(self, folder: str | None):
        root = pathlib.Path(folder).expanduser() if folder else None
        if (str(root) if root else "") != (str(self.root) if self.root else ""):
            if self.root is not None or root is not None:
                self.log(f"PC folder: watching {root}" if root else "PC folder: not watching a folder")
            self.root, self.last_scan, self.last_full, self.last_present = root, 0.0, 0.0, None
            if self.state.get("root") != (str(root) if root else ""):  # another folder: its files are told afresh
                self.state.update(root=str(root) if root else "", files={}, sent={})

    # ------------------------------------------------------------ looking at the folder
    def scan(self):
        """(stable entries with their sha256, every video with a known sha256 for the server's list)."""
        now, budget = self.now(), HASH_BUDGET
        files, shas = self.state["files"], self.state["shas"]
        found = {e["rel"]: e for e in find(self.root)}
        for rel in list(files):
            if rel not in found:
                files.pop(rel)
        stable = []
        for rel, e in found.items():
            sig = [e["size"], e["mtime"]]
            st = files.get(rel)
            if st is None or st.get("sig") != sig:
                known = shas.get(f"{e['size']}:{e['mtime']}")  # moved / renamed: the same file, no need to wait or hash
                st = files[rel] = {"sig": sig, "since": now - STABLE if known else now, "sha": known}
            if now - st["since"] < STABLE:
                continue
            if not st.get("sha"):
                if budget <= 0:
                    continue
                try:
                    sha = sha256(e["path"])
                    after = e["path"].stat()
                except OSError:
                    continue
                if [after.st_size, after.st_mtime_ns] != sig:  # changed while it was read: wait again
                    files[rel] = {"sig": [after.st_size, after.st_mtime_ns], "since": now, "sha": None}
                    continue
                budget -= e["size"]
                st["sha"] = sha
                shas[f"{e['size']}:{e['mtime']}"] = sha
            e["sha"] = st["sha"]
            stable.append(e)
        if len(shas) > 5000:  # forget the oldest remembered hashes
            for k in list(shas)[:len(shas) - 5000]:
                shas.pop(k)
        present = [{"sha": e["sha"], "rel": e["rel"], "name": e["name"], "folder": e["folder"]} for e in stable]
        return stable, present

    @staticmethod
    def info_sig(e) -> str:
        return json.dumps(e["extra"]) + e["rel"]

    def item(self, e) -> dict:
        info, desc = read_info(e["folder_path"])
        meta = self.probe(e["path"])
        return {"sha": e["sha"], "rel": e["rel"], "name": e["name"], "folder": e["folder"], "size": e["size"],
                "duration": meta.get("duration") or 0, "width": meta.get("width") or 0, "height": meta.get("height") or 0,
                "info": info, "description_txt": desc[:DESC_MAX], "thumb": True}

    def thumb(self, e) -> tuple[bytes, str]:
        """The folder's thumbnail (made smaller when it is over 2 MB), else a small frame of the video."""
        t = e.get("thumb_path")
        if t is not None:
            try:
                if t.stat().st_size <= THUMB_MAX:
                    return t.read_bytes(), "file"
                data = self.frame(t, 0, 1280)
                if data and len(data) <= THUMB_MAX:
                    return data, "file"
            except OSError:
                pass
        meta = {}
        try:
            meta = self.probe(e["path"])
        except Exception:  # noqa: BLE001
            pass
        at = 1.0 if (meta.get("duration") or 0) > 2 else 0.0
        return self.frame(e["path"], at, 480), "frame"

    def tick(self, server: str, headers: dict, http, force: bool = False) -> dict:
        """Look at the folder (every SCAN_EVERY s) and tell the server what is new / moved / gone. Returns a summary."""
        now = self.now()
        out = {"sent": 0, "thumbs": 0}
        if self.root is None or (not force and now - self.last_scan < SCAN_EVERY):
            return out
        self.last_scan = now
        if not self.root.is_dir():
            if not self.missing_said or now - self.last_full > FULL_EVERY:
                self.log(f"PC folder: {self.root} is not on this PC (create it, or change it in the dashboard)")
                self._post(server, headers, http, {"folder": str(self.root), "exists": False, "items": []})
                self.missing_said, self.last_full = True, now
            return out
        self.missing_said = False
        stable, present = self.scan()
        sent = self.state["sent"]
        new = [e for e in stable if sent.get(e["sha"]) != self.info_sig(e)]
        rels = sorted(p["sha"] + "|" + p["rel"] for p in present)
        if not new and rels == self.last_present and now - self.last_full < FULL_EVERY:
            self.save()
            return out
        batches = [new[i:i + BATCH] for i in range(0, len(new), BATCH)] or [[]]
        by_sha = {e["sha"]: e for e in stable}
        for i, part in enumerate(batches):
            body = {"folder": str(self.root), "exists": True, "items": [self.item(e) for e in part]}
            if i == len(batches) - 1:
                body["present"] = present
            r = self._post(server, headers, http, body)
            for e in part:
                sent[e["sha"]] = self.info_sig(e)
            out["sent"] += len(part)
            for sha in (r or {}).get("thumbs") or []:
                iid = ((r or {}).get("ids") or {}).get(sha)
                e = by_sha.get(sha)
                if not iid or e is None:
                    continue
                try:
                    data, kind = self.thumb(e)
                    if data:
                        http.post(f"{server}/api/worker/pc-thumb/{int(iid)}?kind={kind}", content=data, timeout=60,
                                  headers={**headers, "content-type": "application/octet-stream"})
                        out["thumbs"] += 1
                except Exception as ex:  # noqa: BLE001  (a picture is not worth stopping the list for)
                    self.log(f"PC folder: picture of {e['name']} not sent ({str(ex)[:80]})")
        self.last_present, self.last_full = rels, now
        self.save()
        if out["sent"]:
            self.log(f"PC folder: told the dashboard about {out['sent']} video(s) in {self.root}")
        return out

    def _post(self, server, headers, http, body) -> dict:
        r = http.post(f"{server}/api/worker/pc-items", headers=headers, json=body, timeout=120)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            return {}

    # ------------------------------------------------------------ the video itself, when the server asks
    def path_for(self, sha: str, rel: str = "") -> pathlib.Path | None:
        """The file in the folder with this sha256 (checked), wherever it is now."""
        if self.root is None or not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
            return None
        cands = []
        if rel and ".." not in pathlib.PurePath(rel).parts and not pathlib.PurePath(rel).is_absolute():
            cands.append(self.root / rel)
        cands += [self.root / r for r, st in self.state["files"].items() if st.get("sha") == sha]
        for p in cands:
            try:
                if p.is_file() and p.resolve().is_relative_to(self.root.resolve()) and sha256(p) == sha:
                    return p
            except OSError:
                continue
        return None

    def send_file(self, job: dict, server: str, headers: dict, http):
        sha = str(job.get("sha") or "").lower()
        p = self.path_for(sha, str(job.get("rel") or ""))
        if p is None and self.root is not None and self.root.is_dir():
            self.scan()  # moved since the last look: look again (a moved file keeps its size and time, so no new hash)
            p = self.path_for(sha)
        if p is None:
            raise RuntimeError("the video is not in the folder on this PC any more (moved out, renamed outside it, or deleted)")
        iid = int(job["item_id"])
        for i in range(3):
            try:
                with open(p, "rb") as fh:
                    r = http.post(f"{server}/api/worker/pc-file/{iid}", content=fh, timeout=1800,
                                  headers={**headers, "content-type": "application/octet-stream",
                                           "content-length": str(p.stat().st_size)})
                if r.status_code == 409:
                    break  # no longer needed (or refused): the server says why on the clip
                r.raise_for_status()
                break
            except Exception as e:  # noqa: BLE001
                code = getattr(getattr(e, "response", None), "status_code", 500)
                if code < 500 or i == 2:
                    raise
                time.sleep(10 * (i + 1))
        self.log(f"PC folder: sent {p.name} for its upload -> {r.status_code}")
        return r.status_code
