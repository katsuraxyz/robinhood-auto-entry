#!/usr/bin/env python3
"""
Kolam RPC dengan rotasi + fallback otomatis.

Urutan pemakaian:
  1. Semua URL di rpcs.txt (satu per baris) dipakai bergiliran round-robin
  2. Kalau sebuah RPC error, dia di-skip sementara (cooldown), pindah ke yang lain
  3. Kalau SEMUA RPC pribadi mati, otomatis jatuh ke RPC publik

Cara pakai:
  rpcs.txt   -> daftar RPC pribadi (Alchemy dll), satu URL per baris
               baris kosong dan yang mulai '#' diabaikan
  RH_RPC     -> kalau diisi, hanya URL ini yang dipakai (mengabaikan rpcs.txt)

Kunci API tidak pernah ditulis ke log. Yang dicetak hanya nama pendek
seperti "alchemy#3" atau "public".
"""

from __future__ import annotations

import itertools
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from web3 import Web3

PUBLIC_RPC = "https://rpc.mainnet.chain.robinhood.com"
RPCS_FILE = Path(os.getenv("RH_RPCS_FILE", Path(__file__).parent / "rpcs.txt"))
COOLDOWN = float(os.getenv("RH_RPC_COOLDOWN", "30"))   # detik istirahat kalau error
# Satu error sesaat bukan alasan membuang RPC. Baru di-cooldown setelah gagal
# beberapa kali berturut-turut, supaya tidak terjadi efek berantai yang
# mematikan semua RPC lalu memaksa semuanya jatuh ke RPC publik.
FAIL_THRESHOLD = int(os.getenv("RH_RPC_FAILS", "5"))


def short_name(url: str, idx: int | None = None) -> str:
    """Nama aman untuk log, tanpa membocorkan API key."""
    host = urlparse(url).netloc
    if "alchemy" in host:
        return f"alchemy#{idx}" if idx is not None else "alchemy"
    if host == urlparse(PUBLIC_RPC).netloc:
        return "public"
    return host.split(".")[0] or "rpc"


def load_rpc_urls() -> tuple[list[str], bool]:
    """Balikan (daftar_url_pribadi, ada_file). RH_RPC menang kalau diisi."""
    override = os.getenv("RH_RPC", "").strip()
    if override:
        return [override], False

    if not RPCS_FILE.exists():
        return [], False

    urls: list[str] = []
    for line in RPCS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(("http://", "https://")):
            continue
        if line not in urls:
            urls.append(line)
    return urls, True


class RpcPool:
    """Pilih RPC yang sehat secara bergiliran, dengan fallback ke publik."""

    def __init__(self, urls: list[str] | None = None, use_public_fallback: bool = True):
        if urls is None:
            urls, _ = load_rpc_urls()
        self.private = list(urls)
        self.use_public = use_public_fallback
        self.names = {u: short_name(u, i + 1) for i, u in enumerate(self.private)}
        if self.use_public and PUBLIC_RPC not in self.names:
            self.names[PUBLIC_RPC] = "public"
        self._cycle = itertools.cycle(self.private) if self.private else None
        self._bad: dict[str, float] = {}      # url -> waktu boleh dicoba lagi
        self._fails: dict[str, int] = {}      # url -> gagal berturut-turut
        self._lock = threading.Lock()
        self._local = threading.local()

    # ---------- pemilihan url ----------
    def _healthy_private(self) -> list[str]:
        now = time.time()
        return [u for u in self.private if self._bad.get(u, 0) <= now]

    def pick(self) -> str:
        """URL berikutnya yang dianggap sehat."""
        with self._lock:
            ok = self._healthy_private()
            if ok:
                # round-robin di antara yang sehat
                for _ in range(len(self.private) * 2):
                    u = next(self._cycle)
                    if u in ok:
                        return u
                return ok[0]
        if self.use_public:
            return PUBLIC_RPC
        # semua pribadi sedang cooldown dan tidak boleh fallback: pakai yg paling cepat pulih
        return min(self.private, key=lambda u: self._bad.get(u, 0)) if self.private else PUBLIC_RPC

    def mark_bad(self, url: str) -> None:
        if url == PUBLIC_RPC:
            return
        with self._lock:
            n = self._fails.get(url, 0) + 1
            self._fails[url] = n
            # jangan pernah membuang RPC terakhir yang masih sehat
            masih_sehat = [u for u in self.private
                           if self._bad.get(u, 0) <= time.time() and u != url]
            if n >= FAIL_THRESHOLD and masih_sehat:
                self._bad[url] = time.time() + COOLDOWN
                self._fails[url] = 0

    def mark_ok(self, url: str) -> None:
        with self._lock:
            self._bad.pop(url, None)
            self._fails.pop(url, None)

    def name(self, url: str) -> str:
        return self.names.get(url, short_name(url))

    # ---------- koneksi ----------
    def web3(self, url: str | None = None) -> tuple[Web3, str]:
        u = url or self.pick()
        return Web3(Web3.HTTPProvider(u, request_kwargs={"timeout": 30})), u

    def thread_web3(self) -> tuple[Web3, str]:
        """Ambil koneksi untuk request berikutnya.

        RPC dipilih round-robin SETIAP request, bukan ditempel per thread.
        Kalau menempel, beberapa RPC dihajar terus-menerus oleh thread yang sama
        dan kena rate limit, sementara RPC lain menganggur. Objek Web3 tetap
        dipakai ulang per URL supaya koneksi HTTP tidak dibuat ulang terus.
        """
        u = self.pick()
        cache = getattr(self._local, "cache", None)
        if cache is None:
            cache = self._local.cache = {}
        w3 = cache.get(u)
        if w3 is None:
            w3 = cache[u] = Web3(Web3.HTTPProvider(u, request_kwargs={"timeout": 30}))
        return w3, u

    def drop_thread_rpc(self) -> None:
        """Tidak perlu lagi: rotasi sudah terjadi tiap request."""
        return None

    # ---------- info ----------
    def summary(self) -> str:
        n = len(self.private)
        if not n:
            return "RPC: public saja (rpcs.txt tidak ada / kosong)"
        return (f"RPC: {n} pribadi (rotasi)"
                + (" + fallback public" if self.use_public else ""))

    def health_check(self, chain_id: int) -> list[tuple[str, bool, str]]:
        """Cek semua RPC. Balikan (nama, sehat, keterangan)."""
        out: list[tuple[str, bool, str]] = []
        targets = list(self.private) + ([PUBLIC_RPC] if self.use_public else [])
        for u in targets:
            nm = self.name(u)
            try:
                w3 = Web3(Web3.HTTPProvider(u, request_kwargs={"timeout": 15}))
                cid = w3.eth.chain_id
                blk = w3.eth.block_number
                if cid != chain_id:
                    out.append((nm, False, f"chain id salah: {cid}"))
                else:
                    out.append((nm, True, f"blok {blk}"))
            except Exception as e:  # noqa: BLE001
                out.append((nm, False, f"{type(e).__name__}"))
        return out
