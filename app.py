import os
import json
import queue
import threading
import time
import traceback

import customtkinter as ctk
import tkinter as tk
from tkinter import ttk, messagebox

import config as cfg_mod
from browser import DriveBrowser
from lister import list_shared_items
from downloader import build_session, download, sanitize_filename, is_video_name
from video_download import download_video, discover


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Gdrive Clonner - Shared with Me")
        self.geometry("1080x700")
        ctk.set_appearance_mode("dark")

        self.cfg = cfg_mod.load_config()
        self.browser = None
        self.items = []
        self._job_bytes = 0
        self._job_total = 0
        self._job_start = 0.0
        self._jobs_done = 0
        self._jobs_total = 0
        self._speed = 0.0
        self._last_sample_t = 0.0
        self._last_sample_bytes = 0
        self._active = {}
        self._agg_speed = 0.0
        self._last_agg_t = 0.0
        self._last_agg_bytes = 0
        self._downloading = False
        self._abort = threading.Event()
        self._headless = bool(self.cfg.get("headless", False))
        self._node = {}
        self._loaded = set()

        self.cmd_queue = queue.Queue()
        self.result_queue = queue.Queue()
        try:
            import video_download as vd
            vd.set_notify(lambda m: self.result_queue.put(("log", m)))
        except Exception:
            pass
        self._worker_stop = False
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

        self._build_ui()
        self.after(150, self._poll_results)

    def _build_ui(self):
        toolbar = ctk.CTkFrame(self)
        toolbar.pack(fill="x", padx=10, pady=(10, 4))

        ctk.CTkButton(toolbar, text="Buka Browser & Login", command=self.cmd_login).pack(side="left", padx=4)
        ctk.CTkButton(toolbar, text="Muat List Shared", command=self.cmd_list).pack(side="left", padx=4)
        ctk.CTkButton(toolbar, text="Refresh", command=self.cmd_list).pack(side="left", padx=4)
        ctk.CTkButton(toolbar, text="Pilih Semua", command=self.cmd_select_all).pack(side="left", padx=4)
        ctk.CTkButton(toolbar, text="Buka Preview", command=self.cmd_preview).pack(side="left", padx=4)

        ctk.CTkButton(toolbar, text="Mulai Download", fg_color="#2a7", hover_color="#1f6",
                      command=self.cmd_clone).pack(side="right", padx=4)

        opts = ctk.CTkFrame(self)
        opts.pack(fill="x", padx=10, pady=(0, 4))

        self.recursive_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(opts, text="Rekursif folder", variable=self.recursive_var).pack(side="left", padx=4)

        ctk.CTkLabel(opts, text="Kualitas video:").pack(side="left", padx=(12, 2))
        self.quality_var = tk.StringVar(value="720p")
        ctk.CTkOptionMenu(opts, variable=self.quality_var, width=110,
                          values=["720p", "360p", "1080p"]).pack(side="left", padx=4)

        self.headless_var = ctk.BooleanVar(value=self._headless)
        ctk.CTkCheckBox(opts, text="Headless (tanpa jendela)", variable=self.headless_var,
                        command=self._on_headless_toggle).pack(side="left", padx=(12, 4))

        ctk.CTkLabel(opts, text="File paralel:").pack(side="left", padx=(12, 2))
        self.parallel_var = tk.StringVar(value=str(self.cfg.get("parallel_files", 4)))
        ctk.CTkOptionMenu(opts, variable=self.parallel_var, width=64,
                          values=["1", "2", "3", "4", "6", "8", "12"]).pack(side="left", padx=4)

        self.skip_var = ctk.BooleanVar(value=bool(self.cfg.get("skip_existing", True)))
        ctk.CTkCheckBox(opts, text="Lewati yang sudah ada", variable=self.skip_var).pack(side="left", padx=(12, 4))

        body = ctk.CTkFrame(self)
        body.pack(fill="both", expand=True, padx=10, pady=4)

        cols = ("name", "type", "size")
        self.tree = ttk.Treeview(body, columns=cols, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Status")
        self.tree.heading("name", text="Nama")
        self.tree.heading("type", text="Tipe")
        self.tree.heading("size", text="Ukuran")
        self.tree.column("#0", width=84, stretch=False)
        self.tree.column("name", width=440)
        self.tree.column("type", width=120, stretch=False)
        self.tree.column("size", width=90, stretch=False, anchor="e")
        self.tree.tag_configure("folder", foreground="#7fb3ff")
        self.tree.tag_configure("video", foreground="#ffb26b")
        yscroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open)

        prog = ctk.CTkFrame(self)
        prog.pack(fill="x", padx=10, pady=(4, 0))

        self.overall_var = tk.StringVar(value="Menunggu...")
        ctk.CTkLabel(prog, textvariable=self.overall_var, width=150, anchor="w").pack(side="left", padx=6)
        self.overall_bar = ctk.CTkProgressBar(prog, width=240)
        self.overall_bar.set(0)
        self.overall_bar.pack(side="left", padx=6)

        self.item_var = tk.StringVar(value="")
        ctk.CTkLabel(prog, textvariable=self.item_var, anchor="w").pack(side="left", padx=(16, 6), fill="x", expand=True)

        prog2 = ctk.CTkFrame(self)
        prog2.pack(fill="x", padx=10, pady=(2, 0))
        self.bytes_var = tk.StringVar(value="")
        ctk.CTkLabel(prog2, textvariable=self.bytes_var, width=240, anchor="w").pack(side="left", padx=6)
        self.item_bar = ctk.CTkProgressBar(prog2)
        self.item_bar.set(0)
        self.item_bar.pack(side="left", padx=6, fill="x", expand=True)

        log_frame = ctk.CTkFrame(self)
        log_frame.pack(fill="x", padx=10, pady=(4, 0))
        log_top = ctk.CTkFrame(log_frame, fg_color="transparent")
        log_top.pack(fill="x", pady=(4, 2))
        ctk.CTkLabel(log_top, text="Debug Log").pack(side="left", padx=6)
        ctk.CTkButton(log_top, text="Copy Semua", width=100, command=self._copy_log).pack(side="right", padx=6)
        self.log_box = ctk.CTkTextbox(log_frame, height=110, wrap="none")
        self.log_box.configure(state="disabled")
        self.log_box.pack(fill="x", padx=6, pady=(0, 6))

        bottom = ctk.CTkFrame(self)
        bottom.pack(fill="x", padx=10, pady=(4, 10))
        self.status_var = tk.StringVar(value="Siap")
        ctk.CTkLabel(bottom, textvariable=self.status_var).pack(side="left", padx=6)

    def _poll_results(self):
        try:
            while True:
                event, data = self.result_queue.get_nowait()
                self._dispatch(event, data)
        except queue.Empty:
            pass
        self.after(150, self._poll_results)

    def _log(self, msg):
        ts = time.strftime("[%H:%M:%S] ")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", ts + str(msg) + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _copy_log(self):
        text = self.log_box.get("1.0", "end").rstrip("\n")
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_var.set("Log disalin ke clipboard.")

    def _dispatch(self, event, data):
        if event == "status":
            self.status_var.set(data)
            self._log(data)
        elif event == "log":
            self._log(data)
        elif event == "queue":
            self._on_queue(data)
        elif event == "file_start":
            self._on_file_start(data)
        elif event == "file_progress":
            self._on_file_progress(data)
        elif event == "file_done":
            self._on_file_done(data)
        elif event == "items":
            self._populate_items(data)
        elif event == "folder_children":
            self._on_folder_children(data)
        elif event == "folder_error":
            self._on_folder_error(data)
        elif event == "progress":
            self._set_item_status(data.get("id"), data.get("status", ""))
            if data.get("reason"):
                self._log("%s -> %s (%s)" % (data.get("name", data.get("id")),
                                             data.get("status", ""), data.get("reason", "")))
            else:
                self._log("%s -> %s" % (data.get("name", data.get("id")), data.get("status", "")))
        elif event == "error":
            self.status_var.set("ERROR: %s" % data.splitlines()[-1] if data else "ERROR")
            self._log("ERROR\n" + data)
            messagebox.showerror("Error", data)

    def _fmt_bytes(self, n):
        if n is None:
            return "?"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return ("%.1f %s" % (n, unit)) if unit != "B" else ("%d B" % n)
            n /= 1024.0

    def _fmt_eta(self, seconds):
        seconds = int(seconds)
        if seconds < 0:
            seconds = 0
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h:
            return "%d:%02d:%02d" % (h, m, s)
        return "%d:%02d" % (m, s)

    def _on_queue(self, data):
        self._jobs_total = int(data.get("total", 0))
        self._parallel = int(data.get("parallel", 1))
        self._jobs_done = 0
        self._active = {}
        self._agg_speed = 0.0
        self._last_agg_t = time.time()
        self._last_agg_bytes = 0
        self._downloading = True
        self.overall_bar.set(0)
        self.item_bar.set(0)
        self.item_var.set("")
        self.bytes_var.set("Menyiapkan %d file..." % self._jobs_total)
        self.overall_var.set("File 0/%d" % self._jobs_total)

    def _on_file_start(self, data):
        fid = data.get("id", "")
        self._active[fid] = {"name": data.get("name", ""), "done": 0, "total": 0}
        self._log("↓ " + (data.get("name", "") or fid))
        self._redraw_progress()

    def _on_file_progress(self, data):
        e = self._active.get(data.get("id"))
        if not e:
            return
        e["done"] = int(data.get("done", 0))
        if data.get("total"):
            e["total"] = int(data["total"])
        self._redraw_progress()

    def _on_file_done(self, data):
        fid = data.get("id", "")
        self._active.pop(fid, None)
        self._jobs_done += 1
        self._set_item_status(fid, data.get("status", ""))
        name = data.get("name", "")
        status = data.get("status", "")
        reason = data.get("reason", "")
        nbytes = data.get("bytes")
        elapsed = data.get("elapsed")

        if status.startswith("ok") and nbytes:
            speed = nbytes / elapsed if elapsed else 0
            self._log("%s -> %s (%s, %.1fs, %s/s)" % (
                name, status, self._fmt_bytes(nbytes), elapsed or 0, self._fmt_bytes(speed)))
        elif reason:
            self._log("%s -> %s (%s)" % (name, status, reason))
        else:
            self._log("%s -> %s" % (name, status))

        self._redraw_progress()
        if not self._active and self._jobs_done >= self._jobs_total:
            self._downloading = False
            self.item_bar.set(1)
            self.bytes_var.set("Selesai")
            self.status_var.set("Selesai: %d file diproses." % self._jobs_done)

    def _redraw_progress(self):
        if not self._downloading:
            return
        done = sum(e["done"] for e in self._active.values())
        known = sum(e["total"] for e in self._active.values() if e["total"] > 0)

        now = time.time()
        dt = now - self._last_agg_t
        d = done - self._last_agg_bytes
        if dt >= 0.2 and d >= 0:
            inst = d / dt
            self._agg_speed = inst if self._agg_speed <= 0 else (self._agg_speed * 0.6 + inst * 0.4)
            self._last_agg_t = now
            self._last_agg_bytes = done

        n = len(self._active)
        self.overall_var.set("File %d/%d · %d aktif" % (self._jobs_done, self._jobs_total, n))
        self.overall_bar.set(self._jobs_done / self._jobs_total if self._jobs_total else 0)

        names = [e["name"] for e in self._active.values()]
        shown = ", ".join(names[:3]) + (", …" if len(names) > 3 else "")
        self.item_var.set(shown if shown else "Menunggu...")

        speed_txt = self._fmt_bytes(self._agg_speed) + "/s" if self._agg_speed > 0 else "-"
        if known:
            self.item_bar.set(done / known if known else 0)
            self.bytes_var.set("%s / %s · %s" % (self._fmt_bytes(done), self._fmt_bytes(known), speed_txt))
        else:
            self.item_bar.set(0)
            self.bytes_var.set("%s diunduh · %s" % (self._fmt_bytes(done), speed_txt))

    def _populate_items(self, items):
        self.items = items
        self._node = {}
        self._loaded = set()
        for row in self.tree.get_children():
            self.tree.delete(row)
        for it in items:
            self._insert_node("", it)
        self.status_var.set("%d items" % len(items))

    def _insert_node(self, parent, it):
        self._node[it["id"]] = {"name": it["name"], "is_folder": it["is_folder"], "parent": parent or None}
        if it["is_folder"]:
            tag, itype = "folder", "Folder"
        elif is_video_name(it["name"]):
            tag, itype = "video", "Video"
        else:
            tag, itype = "", "File"
        self.tree.insert(parent, "end", iid=it["id"], text="", tags=(tag,),
                         values=(it["name"], itype, ""))
        if it["is_folder"]:
            self.tree.insert(it["id"], "end", iid=it["id"] + "::dummy", text="",
                            values=("…", "", ""))

    def _on_tree_open(self, event):
        iid = self.tree.focus()
        if not iid:
            return
        dummy = iid + "::dummy"
        if self.tree.exists(dummy):
            self.tree.delete(dummy)
        if iid in self._loaded:
            return
        self._loaded.add(iid)
        self.result_queue.put(("status", "Memuat isi folder..."))
        self.cmd_queue.put(("list_folder", iid))

    def _on_folder_children(self, data):
        pid = data["id"]
        children = data.get("children", [])
        if not self.tree.exists(pid):
            self._loaded.discard(pid)
            return
        for c in children:
            if c["id"] == pid:
                continue
            if c["id"] in self._node:
                continue
            self._insert_node(pid, c)

    def _on_folder_error(self, data):
        self._loaded.discard(data.get("id"))
        self.result_queue.put(("error", "Gagal membuka folder: %s" % data.get("error", "")))

    def _set_item_status(self, item_id, status):
        if self.tree.exists(item_id):
            self.tree.item(item_id, text=status)

    def _selected_ids(self):
        return [i for i in self.tree.selection() if self.tree.exists(i)]

    def _item_by_id(self, item_id):
        n = self._node.get(item_id)
        if n:
            return {"id": item_id, "name": n["name"], "is_folder": n["is_folder"]}
        for it in self.items:
            if it["id"] == item_id:
                return it
        return None

    def cmd_login(self):
        self.cmd_queue.put(("start", None))

    def cmd_list(self):
        self.cmd_queue.put(("list", None))

    def cmd_select_all(self):
        if self.tree.get_children():
            self.tree.selection_set(self.tree.get_children())

    def cmd_preview(self):
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("Info", "Pilih item dulu.")
            return
        item = self._item_by_id(ids[0])
        self.cmd_queue.put(("preview", item))

    def cmd_clone(self):
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("Info", "Pilih item dulu.")
            return
        recursive = self.recursive_var.get()
        quality = self.quality_var.get()
        parallel = int(self.parallel_var.get())
        skip_existing = self.skip_var.get()
        selected = [self._item_by_id(i) for i in ids]
        selected = [x for x in selected if x]
        self.cmd_queue.put(("clone", {"items": selected, "recursive": recursive,
                                      "quality": quality, "parallel": parallel,
                                      "skip_existing": skip_existing}))

    def _worker_loop(self):
        while not self._worker_stop:
            try:
                cmd = self.cmd_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if cmd[0] == "exit":
                break
            try:
                self._run(cmd)
            except Exception:
                self.result_queue.put(("error", traceback.format_exc()))

    def _on_headless_toggle(self):
        self._headless = self.headless_var.get()
        self.cfg["headless"] = self._headless
        cfg_mod.save_config(self.cfg)

    def _ensure_browser(self):
        if self.browser is None:
            self.result_queue.put(("status", "Membuka browser (%s)..." % ("headless" if self._headless else "tampil")))
            self.browser = DriveBrowser(self.cfg.get("profile_dir") or cfg_mod.PROFILE_DIR,
                                        self._headless,
                                        launch_mode=self.cfg.get("launch_mode", "cdp"),
                                        cdp_port=int(self.cfg.get("cdp_port", 9222)))
            self.browser.start()

    def _run(self, cmd):
        name, payload = cmd
        if name == "start":
            self._ensure_browser()
            self.browser.open_shared_with_me()
            url = self.browser.page.url
            if "accounts.google.com" in url:
                self.result_queue.put(("status", "Login dulu di browser, lalu klik 'Muat List Shared'."))
            else:
                self.result_queue.put(("status", "Browser siap dan sudah login."))
        elif name == "list":
            self._ensure_browser()
            self.result_queue.put(("status", "Memuat shared with me..."))
            items = list_shared_items(self.browser)
            self.result_queue.put(("items", items))
        elif name == "preview":
            self._ensure_browser()
            self.result_queue.put(("status", "Membuka preview..."))
            self.browser.open_file(payload["id"])
            self.browser.bring_to_front()
            self.result_queue.put(("status", "Preview terbuka."))
        elif name == "list_folder":
            self._ensure_browser()
            try:
                children = self._list_folder(payload)
            except Exception as exc:
                self.result_queue.put(("folder_error", {"id": payload, "error": str(exc)}))
            else:
                self.result_queue.put(("folder_children", {"id": payload, "children": children}))
        elif name == "clone":
            self._clone(payload)

    def _make_file_progress(self, file_id):
        state = {"t": 0.0}

        def cb(done, total):
            now = time.time()
            if now - state["t"] < 0.3 and done != total:
                return
            state["t"] = now
            self.result_queue.put(("file_progress", {"id": file_id, "done": done, "total": total}))
        return cb

    def _final_name(self, name):
        n = sanitize_filename(name)
        if is_video_name(name):
            if not n.lower().endswith(".mp4"):
                root, ext = os.path.splitext(n)
                n = (root if ext else n) + ".mp4"
        return n

    def _target(self, f):
        return os.path.join(f["dest"], self._final_name(f["name"]))

    def _manifest_path(self):
        base = self.cfg.get("download_dir") or cfg_mod.DOWNLOAD_DIR
        return os.path.join(base, ".cloner_manifest.json")

    def _load_manifest(self):
        try:
            with open(self._manifest_path(), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save_manifest(self, manifest):
        try:
            with open(self._manifest_path(), "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _clone(self, payload):
        self._abort.clear()
        import video_download as vd
        vd.ABORT.clear()
        items = payload["items"]
        recursive = payload.get("recursive", True)
        quality = payload.get("quality", "720p")
        parallel = int(payload.get("parallel", 4))
        parallel = max(1, min(parallel, 16))
        skip_existing = bool(payload.get("skip_existing", True))
        dest = self.cfg.get("download_dir") or cfg_mod.DOWNLOAD_DIR
        os.makedirs(dest, exist_ok=True)
        cookies = self.browser.cookies()
        connections = int(self.cfg.get("stream_connections", 8))
        manifest = self._load_manifest()

        self.result_queue.put(("status", "Memindai struktur folder..."))
        files = []
        visited = set()
        self._flatten(items, dest, recursive, files, visited)

        total = len(files)
        self.result_queue.put(("queue", {"total": total, "parallel": parallel}))
        if total == 0:
            self.result_queue.put(("status", "Tidak ada file untuk diunduh."))
            return

        video_file = next((f for f in files if is_video_name(f["name"])), None)
        if video_file:
            try:
                import video_download as vd
                if not vd._state["endpoint"]:
                    self.result_queue.put(("status", "Menemukan endpoint stream..."))
                    vd.discover(self.browser, video_file["id"])
            except Exception as exc:
                self.result_queue.put(("status", "Discover stream gagal: %s" % str(exc)[:120]))

        nxt = [0]
        okc = [0]
        skipc = [0]
        failc = [0]
        lock = threading.Lock()

        def worker():
            sess = build_session(cookies)
            while True:
                if self._abort.is_set():
                    return
                with lock:
                    i = nxt[0]
                    nxt[0] += 1
                if i >= total:
                    return
                f = files[i]
                target = self._target(f)
                if skip_existing and os.path.exists(target) and os.path.getsize(target) > 0:
                    with lock:
                        skipc[0] += 1
                    self.result_queue.put(("file_done", {"id": f["id"], "name": f["name"],
                                                        "status": "skip", "reason": "sudah ada"}))
                    continue
                t0 = time.time()
                self.result_queue.put(("file_start", {"id": f["id"], "name": f["name"]}))
                pcb = self._make_file_progress(f["id"])
                try:
                    out = self._download_file(f, sess, cookies, quality, connections, pcb)
                except Exception as exc:
                    out = {"status": "error", "reason": str(exc)}
                info = {"id": f["id"], "name": f["name"],
                        "status": out["status"], "reason": out.get("reason", "")}
                if out["status"].startswith("ok"):
                    with lock:
                        okc[0] += 1
                    p = out.get("path")
                    if p and os.path.exists(p):
                        rel = os.path.relpath(p, dest)
                        manifest[rel] = os.path.getsize(p)
                        info["bytes"] = os.path.getsize(p)
                        info["elapsed"] = time.time() - t0
                else:
                    with lock:
                        failc[0] += 1
                self.result_queue.put(("file_done", info))

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(parallel)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self._save_manifest(manifest)
        self.result_queue.put(("log", "━━━━━━━━━━━━━━━━━━━━"))
        self.result_queue.put(("log", "Ringkasan: %d berhasil, %d dilewati, %d gagal (dari %d)." % (
            okc[0], skipc[0], failc[0], total)))
        self.result_queue.put(("status", "Selesai: %d berhasil, %d dilewati, %d gagal." % (
            okc[0], skipc[0], failc[0])))

    def _flatten(self, items, dest, recursive, files, visited):
        for item in items:
            iid = item["id"]
            if iid in visited:
                continue
            if item.get("is_folder"):
                if not recursive:
                    continue
                visited.add(iid)
                sub = os.path.join(dest, sanitize_filename(item["name"]))
                os.makedirs(sub, exist_ok=True)
                self.result_queue.put(("log", "📁 " + item["name"] + " (memindai...)"))
                try:
                    children = self._list_folder(iid)
                except Exception as exc:
                    self.result_queue.put(("progress", {"id": iid, "name": item["name"],
                                                        "status": "gagal buka folder", "reason": str(exc)}))
                    continue
                self._flatten([c for c in children if c["id"] != iid], sub, recursive, files, visited)
            else:
                files.append({"id": iid, "name": item["name"], "dest": dest})

    def _download_file(self, f, session, cookies, quality, connections, cb):
        res = download(session, f["id"], f["name"], f["dest"], progress_cb=cb)
        if res["status"] == "downloaded":
            return {"status": "ok", "path": res.get("path")}
        if res["status"] == "not_downloadable":
            reason = res.get("reason", "")
            if is_video_name(f["name"]):
                try:
                    path = download_video(f["id"], f["name"], f["dest"], cookies,
                                          quality=quality, connections=connections, progress_cb=cb)
                    return {"status": "ok (stream)", "path": path}
                except Exception as stream_exc:
                    return {"status": "gagal", "reason": "%s | stream: %s" % (reason, str(stream_exc)[:140])}
            return {"status": "tidak bisa", "reason": reason}
        return {"status": "error", "reason": res.get("reason", "")}

    def _list_folder(self, folder_id):
        url = "https://drive.google.com/drive/folders/%s" % folder_id
        self.browser.page.goto(url, wait_until="domcontentloaded", timeout=90000)
        self.browser.page.wait_for_timeout(2500)
        from lister import scroll_shared_list, scrape_items
        scroll_shared_list(self.browser)
        return scrape_items(self.browser)

    def _cleanup_partials(self):
        base = self.cfg.get("download_dir") or cfg_mod.DOWNLOAD_DIR
        removed = 0
        for root, _dirs, files in os.walk(base):
            for fn in files:
                low = fn.lower()
                if ".part" in low or low.endswith(".vid.mp4") or low.endswith(".aud.m4a"):
                    try:
                        os.remove(os.path.join(root, fn))
                        removed += 1
                    except Exception:
                        pass
        return removed

    def on_close(self):
        self._worker_stop = True
        self._abort.set()
        import video_download as vd
        vd.ABORT.set()
        self.cmd_queue.put(("exit", None))
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=5.0)
        self._cleanup_partials()
        if self.browser is not None:
            self.browser.close()
        self.destroy()


def main():
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()