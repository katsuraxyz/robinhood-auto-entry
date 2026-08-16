#!/usr/bin/env python3
"""
Auto-entry bot untuk kontrak 0x3FB5C23cE237A63CCBF9c4FD0F6d4E4Cd25BE4F9
di Robinhood Chain (chain ID 4663). Funder + wallet tanpa batas jumlah.

Alur tiap putaran:
  1. Baca currentDay() dan paused()
  2. FLAG CHECK: wallet yang sudah entry hari ini dilewati TANPA panggil RPC
  3. Sisanya dicek paralel (simulasi eth_call) untuk pastikan benar belum entry
  4. Funder kirim ETH gas ke wallet yang saldonya kurang
  5. Wallet entry, diproses per batch
  6. Tidur sampai rollover berikutnya (14:00 UTC / 21:00 WIB), ulangi

Fakta yang sudah diverifikasi langsung on-chain (bukan asumsi):
  - Fungsi entry  : selector 0xcd960f2b, TANPA argumen, msg.value = 0
  - Event         : topic0 0x6e41...4cfb, topic1 = day, topic2 = sender
  - Sudah entry   : revert custom error 0x2ed7f582 (1x per wallet per hari)
  - Batas hari    : day = (block.timestamp - 50400) // 86400 -> rollover 14:00 UTC
  - Gas entry     : ~83k (tx nyata) ≈ 0.0000023 ETH
  - Gas transfer  : 21k ≈ 0.0000006 ETH

Soal flag 24 jam:
state.json mencatat {address: {day: tx_hash}}. Kalau wallet sudah tercatat untuk
day yang sedang berjalan, bot langsung skip tanpa satu pun panggilan RPC dan
tanpa mengirim transaksi. Jadi tidak ada gas terbuang, walau bot direstart
berkali-kali dalam hari yang sama.
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from web3 import Web3
from web3.exceptions import ContractLogicError

from rpc_pool import PUBLIC_RPC, RpcPool, load_rpc_urls

# ----------------------------------------------------------------------------
# Konstanta hasil verifikasi on-chain
# ----------------------------------------------------------------------------
RPC_URL = os.getenv("RH_RPC", PUBLIC_RPC)   # dipakai kalau tidak ada rpcs.txt
CHAIN_ID = 4663
CONTRACT = Web3.to_checksum_address("0x3FB5C23cE237A63CCBF9c4FD0F6d4E4Cd25BE4F9")

ENTRY_SELECTOR = "0xcd960f2b"
ERR_ALREADY_ENTERED = "2ed7f582"
SEL_CURRENT_DAY = "0x5c9302c9"
SEL_PAUSED = "0x5c975abb"

DAY_OFFSET = 50400
DAY_SECONDS = 86400
# Jeda setelah daily reset (14:00 UTC) sebelum bot mulai kirim tx.
# Default 1 jam: menghindari lonjakan gas dan padatnya jaringan tepat saat reset.
DELAY_AFTER_RESET = int(os.getenv("RH_DELAY_AFTER_RESET", "3600"))

ENTRY_GAS = 84_000
# Batas gas yang dikirim di tx entry. estimate_gas = 84.142, tx nyata pakai
# 83.229, jadi 110.000 aman. Gas sisa tidak dibakar, tidak jadi biaya.
ENTRY_GAS_LIMIT = int(os.getenv("RH_ENTRY_GAS", "110000"))
# Chain ini menolak transfer dengan gas 21.000 ("intrinsic gas too low").
# estimate_gas mengembalikan 21.320, tx nyata terpakai 21.106.
TRANSFER_GAS = int(os.getenv("RH_TRANSFER_GAS", "23000"))
# 1.5x, bukan 1.25x: baseFee bergerak tiap blok dan 1.25x sempat kalah
# ("max fee per gas less than block base fee").
GAS_MULT = float(os.getenv("RH_GAS_MULT", "1.5"))

TOPUP_ENTRIES = int(os.getenv("RH_TOPUP_ENTRIES", "5"))
# Default menyesuaikan jenis RPC: 24 kalau ada RPC pribadi (Alchemy), 8 kalau
# cuma RPC publik. Hasil tes: Alchemy 10 ms/wallet tanpa error di 16-32 worker,
# sedangkan publik mulai menolak request di atas 8 worker.
_HAS_PRIVATE = bool(load_rpc_urls()[0]) and not os.getenv("RH_RPC")
WORKERS = int(os.getenv("RH_WORKERS", "24" if _HAS_PRIVATE else "8"))
BATCH_SIZE = int(os.getenv("RH_BATCH", "25"))        # wallet per batch saat entry
RETRIES = int(os.getenv("RH_RETRIES", "3"))          # ulangan kalau RPC rate-limit
# Rate limit (HTTP 429) itu normal kalau banyak wallet: tiap kunci Alchemy punya
# kuota sendiri. Yang benar bukan mengurangi paralel, tapi mundur sebentar lalu
# ulangi lewat RPC lain. Ulangan khusus 429 dibuat lebih banyak dan lebih sabar.
RETRIES_429 = int(os.getenv("RH_RETRIES_429", "8"))
DELAY_MIN = float(os.getenv("RH_DELAY_MIN", "2"))
DELAY_MAX = float(os.getenv("RH_DELAY_MAX", "6"))
BATCH_PAUSE = float(os.getenv("RH_BATCH_PAUSE", "15"))

WALLETS_FILE = Path(os.getenv("RH_WALLETS", Path(__file__).parent / "wallets.json"))
STATE_FILE = Path(os.getenv("RH_STATE", Path(__file__).parent / "state.json"))


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def mask(a: str) -> str:
    return f"{a[:6]}…{a[-4:]}"


def day_from_ts(ts: int) -> int:
    return (ts - DAY_OFFSET) // DAY_SECONDS


def next_rollover_ts(day: int) -> int:
    return (day + 1) * DAY_SECONDS + DAY_OFFSET


def _is_rate_limit(e: Exception) -> bool:
    """Deteksi HTTP 429 / kuota habis. Ini bukan kesalahan permanen."""
    s = str(e).lower()
    return ("429" in s or "too many requests" in s
            or "rate limit" in s or "capacity" in s)


def hhmm(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}j {m}m" if h else f"{m}m {s}d"


@dataclass
class Wallet:
    address: str
    key: str = field(repr=False)
    label: str = ""
    group: str = "default"

    def __str__(self) -> str:
        return f"{self.label or mask(self.address)}"


class AutoEntry:
    def __init__(self, w3: Web3, funder: Wallet | None, wallets: list[Wallet],
                 pool: RpcPool | None = None):
        self.w3 = w3
        self.funder = funder
        self.wallets = wallets
        self.pool = pool or RpcPool([])
        self._lock = threading.Lock()
        self.state = self._load_state()

    # ---------------- state / flag 24 jam ----------------
    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except json.JSONDecodeError:
                bad = STATE_FILE.with_suffix(".corrupt")
                STATE_FILE.replace(bad)
                log(f"state.json rusak, dipindah ke {bad.name}, mulai dari kosong.")
        return {}

    def _save_state(self) -> None:
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            f.write(json.dumps(self.state, indent=2))
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(STATE_FILE)

    def mark_done(self, w: Wallet, day: int, tx: str) -> None:
        """Tandai wallet sudah entry untuk hari ini. Ini flag 24 jam-nya."""
        with self._lock:
            self.state.setdefault(w.address, {})[str(day)] = tx
            self._save_state()

    def is_flagged(self, w: Wallet, day: int) -> bool:
        """True kalau sudah tercatat entry hari ini -> skip tanpa RPC."""
        return str(day) in self.state.get(w.address, {})

    def prune_state(self, day: int, keep_days: int = 7) -> None:
        """Buang catatan lama supaya state.json tidak membengkak."""
        cut = day - keep_days
        changed = False
        for addr, days in list(self.state.items()):
            for d in list(days):
                if d.isdigit() and int(d) < cut:
                    del days[d]
                    changed = True
            if not days:
                del self.state[addr]
                changed = True
        if changed:
            self._save_state()

    # ---------------- reads ----------------
    def current_day(self) -> int:
        return int.from_bytes(self.w3.eth.call({"to": CONTRACT, "data": SEL_CURRENT_DAY}), "big")

    def is_paused(self) -> bool:
        return int.from_bytes(self.w3.eth.call({"to": CONTRACT, "data": SEL_PAUSED}), "big") != 0

    def fees(self, max_age: float = 20.0) -> tuple[int, int]:
        """Harga gas, di-cache 20 detik.

        Tanpa cache, fungsi ini dipanggil sekali per wallet dan jadi salah satu
        sumber 429 terbesar saat ribuan wallet diproses bersamaan.
        """
        now = time.time()
        cached = getattr(self, "_fee_cache", None)
        if cached and now - cached[0] < max_age:
            return cached[1], cached[2]
        c, _ = self.pool.thread_web3() if self.pool.private else (self.w3, None)
        base = c.eth.gas_price
        max_fee = max(1, int(base * GAS_MULT))
        prio = max(1, min(max_fee, int(base * 0.1)))
        self._fee_cache = (now, max_fee, prio)
        return max_fee, prio

    def can_enter(self, w: Wallet, w3: Web3 | None = None) -> tuple[bool, str]:
        """Simulasi entry. Revert = jawaban pasti; error jaringan = ganti RPC, ulangi."""
        attempt = 0
        budget = max(RETRIES, RETRIES_429)
        while attempt < budget:
            attempt += 1
            if w3 is not None:
                c, url = w3, None
            else:
                c, url = self.pool.thread_web3() if self.pool.private else (self.w3, None)
            try:
                c.eth.call({"from": w.address, "to": CONTRACT,
                            "data": ENTRY_SELECTOR, "value": 0})
                if url:
                    self.pool.mark_ok(url)
                return True, "ok"
            except ContractLogicError as e:
                data = str(getattr(e, "data", "") or e).lower()
                return (False, "already") if ERR_ALREADY_ENTERED in data else (False, "revert")
            except Exception as e:  # noqa: BLE001
                limited = _is_rate_limit(e)
                if url:
                    # 429 bukan tanda RPC rusak, jadi jangan di-cooldown.
                    if not limited:
                        self.pool.mark_bad(url)
                    self.pool.drop_thread_rpc()
                if not limited and attempt >= RETRIES:
                    break
                if attempt < budget:
                    time.sleep(min(4.0, 0.4 * attempt) + random.random() * 0.4)
        return False, "rpc-error"

    def check_many(self, wallets: list[Wallet]) -> tuple[list[Wallet], list[Wallet], int]:
        """Cek status paralel. Balikan (perlu_entry, sudah_entry, jumlah_error).

        Wallet yang statusnya tidak bisa dipastikan (RPC error) DIMASUKKAN ke
        daftar perlu_entry. Alasannya: satu-satunya kerugian kalau ternyata dia
        sudah entry adalah transaksi yang revert. Sedangkan kalau dilewati,
        wallet itu kehilangan entry hari itu sepenuhnya. Yang penting tx berhasil.
        """
        need: list[Wallet] = []
        already: list[Wallet] = []
        errors = 0

        def work(w: Wallet):
            return w, *self.can_enter(w)

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for fut in as_completed([ex.submit(work, w) for w in wallets]):
                w, ok, reason = fut.result()
                if ok:
                    need.append(w)
                elif reason == "already":
                    already.append(w)
                else:
                    # status tidak pasti -> tetap dicoba entry, jangan dibuang
                    errors += 1
                    need.append(w)
        order = {w.address: i for i, w in enumerate(wallets)}
        need.sort(key=lambda x: order[x.address])
        return need, already, errors

    def balances_many(self, wallets: list[Wallet]) -> dict[str, int]:
        out: dict[str, int] = {}

        def work(w: Wallet):
            attempt = 0
            budget = max(RETRIES, RETRIES_429)
            while attempt < budget:
                attempt += 1
                c, url = self.pool.thread_web3() if self.pool.private else (self.w3, None)
                try:
                    bal = c.eth.get_balance(w.address)
                    if url:
                        self.pool.mark_ok(url)
                    return w.address, bal
                except Exception as e:  # noqa: BLE001
                    limited = _is_rate_limit(e)
                    if url:
                        if not limited:
                            self.pool.mark_bad(url)
                        self.pool.drop_thread_rpc()
                    if not limited and attempt >= RETRIES:
                        break
                    if attempt < budget:
                        time.sleep(min(4.0, 0.4 * attempt) + random.random() * 0.4)
            return w.address, -1

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for fut in as_completed([ex.submit(work, w) for w in wallets]):
                a, b = fut.result()
                out[a] = b
        return out

    # ---------------- funding ----------------
    def _rpc(self) -> Web3:
        """Koneksi untuk request berikutnya, ikut rotasi 11 RPC."""
        c, _ = self.pool.thread_web3() if self.pool.private else (self.w3, None)
        return c

    def _kirim_ulet(self, signed_raw, batas: int = 0):
        """Kirim raw tx; kalau 429 ganti RPC lalu ulangi. Balikan tx hash."""
        batas = batas or RETRIES_429
        last = None
        for attempt in range(1, batas + 1):
            c = self._rpc()
            try:
                return c.eth.send_raw_transaction(signed_raw)
            except Exception as e:  # noqa: BLE001
                last = e
                s = str(e).lower()
                if "already known" in s or "nonce too low" in s:
                    return None          # sudah masuk mempool
                if not _is_rate_limit(e):
                    raise
                self.pool.drop_thread_rpc()
                time.sleep(min(3.0, 0.3 * attempt) + random.random() * 0.3)
        raise last if last else RuntimeError("gagal kirim tx")

    def fund_wallets(self, targets: list[Wallet]) -> None:
        if not self.funder:
            log("Tidak ada funder, tahap funding dilewati.")
            return

        max_fee, prio = self.fees()
        need_one = ENTRY_GAS * max_fee
        topup = need_one * TOPUP_ENTRIES

        # Pakai TRANSFER_GAS (default 23.000). estimate_gas hanya dipakai kalau
        # chain ternyata minta LEBIH dari itu, supaya tx tidak ditolak.
        # Gas yang tidak terpakai tidak dibakar, jadi kelebihan tidak jadi biaya.
        xfer_gas = TRANSFER_GAS
        try:
            probe = self._rpc().eth.estimate_gas({
                "from": self.funder.address,
                "to": targets[0].address, "value": 1})
            if probe > xfer_gas:
                xfer_gas = probe
                log(f"  catatan: chain minta {probe} gas untuk transfer, "
                    f"lebih dari setelan {TRANSFER_GAS}")
        except Exception:  # noqa: BLE001
            pass

        bals = self.balances_many(targets)
        # PENTING: saldo yang gagal dibaca (-1) dianggap PERLU top-up, bukan dilewati.
        # Kalau dilewati, wallet itu tidak pernah dapat gas lalu gagal entry diam-diam.
        # Salah kirim transfer cuma buang ~0.0000006 ETH; gagal entry hilang 1 hari.
        short = []
        for w in targets:
            b = bals.get(w.address, -1)
            if b < 0:
                short.append((w, 0))          # anggap kosong, aman
            elif b < need_one:
                short.append((w, b))
        if not short:
            log("Semua wallet sudah punya gas cukup, tidak perlu top-up.")
            return

        fbal = self._rpc().eth.get_balance(self.funder.address)
        total = sum(topup - b for _, b in short) + len(short) * TRANSFER_GAS * max_fee
        log(f"Funder {mask(self.funder.address)}: {fbal/1e18:.8f} ETH | "
            f"{len(short)} wallet perlu top-up (~{total/1e18:.8f} ETH)")
        if fbal < total:
            log("  Saldo funder tidak cukup untuk semua, akan diisi sebanyak yang bisa.")

        nonce = self._rpc().eth.get_transaction_count(self.funder.address)
        sent = 0
        # Kirim semua transfer dulu tanpa menunggu receipt satu per satu.
        # Nonce dari satu funder harus urut, jadi pengirimannya tetap berurutan,
        # tapi menunggu konfirmasi cukup sekali di akhir. Untuk 5000 wallet ini
        # memotong waktu funding dari berjam-jam menjadi beberapa menit.
        fbal = self._rpc().eth.get_balance(self.funder.address)
        pending: list[tuple[Wallet, str]] = []
        fee_cost = xfer_gas * max_fee
        gagal_kirim = 0

        for i, (w, bal) in enumerate(short, 1):
            amount = topup - bal
            if fbal < amount + fee_cost:
                amount = max(0, need_one - bal)     # turunkan ke minimal 1 entry
                if amount <= 0 or fbal < amount + fee_cost:
                    log(f"  [{i}/{len(short)}] dana funder habis, "
                        f"{len(short) - i + 1} wallet sisanya dilewati.")
                    break
            try:
                tx = {"chainId": CHAIN_ID, "from": self.funder.address, "to": w.address,
                      "value": amount, "nonce": nonce, "gas": xfer_gas,
                      "maxFeePerGas": max_fee, "maxPriorityFeePerGas": prio}
                signed = self.w3.eth.account.sign_transaction(tx, self.funder.key)
                h = self._kirim_ulet(signed.raw_transaction)
                if h is not None:
                    pending.append((w, h))
                nonce += 1
                fbal -= amount + fee_cost           # kurangi perkiraan saldo lokal
                if i % 250 == 0:
                    log(f"  terkirim {i}/{len(short)}...")
            except Exception as e:  # noqa: BLE001
                # Satu transfer gagal TIDAK boleh menggagalkan seluruh putaran.
                msg = str(e)[:150]
                gagal_kirim += 1
                if gagal_kirim <= 5:
                    log(f"  [{i}] {w}: transfer gagal -> {msg}")
                elif gagal_kirim == 6:
                    log("  (error transfer berikutnya tidak dicetak lagi)")
                time.sleep(1.5)
                try:
                    nonce = self._rpc().eth.get_transaction_count(self.funder.address)
                    fbal = self._rpc().eth.get_balance(self.funder.address)
                except Exception:  # noqa: BLE001
                    pass

        # tunggu konfirmasi: cukup cek yang terakhir, sisanya diverifikasi lewat saldo
        if pending:
            log(f"  {len(pending)} transfer terkirim, menunggu konfirmasi...")
            try:
                self._rpc().eth.wait_for_transaction_receipt(pending[-1][1], timeout=300)
            except Exception:  # noqa: BLE001
                log("  peringatan: konfirmasi terakhir timeout, lanjut cek saldo.")
            bals2 = self.balances_many([w for w, _ in pending])
            sent = sum(1 for w, _ in pending if bals2.get(w.address, 0) >= need_one)
        log(f"Funding selesai: {sent}/{len(short)} wallet terisi."
            + (f" ({gagal_kirim} transfer gagal)" if gagal_kirim else ""))

    # ---------------- entry ----------------
    def send_entry(self, w: Wallet, day: int, wait: bool = True) -> bool:
        # Semua panggilan di sini WAJIB lewat pool, bukan self.w3.
        # Kalau pakai self.w3, seluruh thread menembak satu RPC yang sama dan
        # langsung kena 429 (pernah terjadi: 201 dari 350 wallet gagal).
        c, curl = self.pool.thread_web3() if self.pool.private else (self.w3, None)

        # Status sudah diverifikasi di check_many sebelum masuk sini, jadi tidak
        # perlu simulasi ulang. Kalau ternyata sudah entry, tx-nya akan revert
        # dan itu terdeteksi di sini juga (biaya ~0.000002 ETH), jauh lebih murah
        # daripada 1 request simulasi ekstra x ribuan wallet yang memicu 429.

        max_fee, prio = self.fees()
        # ENTRY_GAS sudah cukup (tx nyata pakai 83.229, estimate 84.142), jadi
        # estimate_gas per wallet dilewati: itu 1 request ekstra x ribuan wallet.
        gas = ENTRY_GAS_LIMIT

        # Saldo dibaca hanya untuk mencegah tx yang pasti gagal. Kalau pembacaan
        # gagal, JANGAN batalkan entry-nya: lebih baik coba dan gagal daripada
        # melewatkan wallet yang sebenarnya punya gas.
        try:
            bal = c.eth.get_balance(w.address)
            if bal < gas * max_fee:
                log(f"    {w}: gas kurang ({bal/1e18:.8f} ETH), skip")
                return False
        except Exception:  # noqa: BLE001
            pass

        tx = {"chainId": CHAIN_ID, "from": w.address, "to": CONTRACT, "value": 0,
              "data": ENTRY_SELECTOR,
              "nonce": c.eth.get_transaction_count(w.address),
              "gas": gas, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": prio}
        signed = c.eth.account.sign_transaction(tx, w.key)

        # Pengiriman tx: kalau kena 429, ganti RPC lalu coba lagi. Jangan sampai
        # entry hilang cuma karena satu endpoint sedang sibuk.
        h = self._kirim_ulet(signed.raw_transaction)
        if h is None:
            # sudah ada di mempool / nonce terpakai: hitung berhasil
            self.mark_done(w, day, "sudah-di-mempool")
            return True

        hx = h.hex() if str(h.hex()).startswith("0x") else "0x" + h.hex()
        if not wait:
            # Flag dipasang begitu tx diterima mempool. Kalau ternyata gagal,
            # putaran berikutnya akan mendeteksi lewat simulasi on-chain.
            self.mark_done(w, day, hx)
            return True
        r = c.eth.wait_for_transaction_receipt(h, timeout=180)
        if r["status"] == 1:
            self.mark_done(w, day, hx)          # <- flag 24 jam dipasang di sini
            return True
        log(f"    {w}: entry revert on-chain {hx}")
        return False

    # ---------------- putaran ----------------
    def run_round(self) -> int:
        t0 = time.time()
        day = self.current_day()
        self.prune_state(day)

        if self.is_paused():
            log(f"Kontrak paused, putaran dilewati (day {day}).")
            return day

        total = len(self.wallets)
        # LANGKAH 1: flag check, nol RPC
        flagged = [w for w in self.wallets if self.is_flagged(w, day)]
        candidates = [w for w in self.wallets if not self.is_flagged(w, day)]
        log(f"Day {day} | {total} wallet: {len(flagged)} sudah di-flag (skip tanpa RPC), "
            f"{len(candidates)} perlu dicek")

        if not candidates:
            log("Semua wallet sudah entry hari ini. Tidak ada gas terpakai.")
            return day

        # LANGKAH 2: verifikasi on-chain, paralel
        log(f"Cek status {len(candidates)} wallet ({WORKERS} paralel)...")
        need, already, errors = self.check_many(candidates)
        # yang ternyata sudah entry langsung di-flag, jadi tidak dicek ulang hari ini
        for w in already:
            self.mark_done(w, day, "terdeteksi-sudah-entry")
        log(f"Hasil: {len(need)} perlu entry, {len(already)} sudah entry, {errors} error "
            f"({time.time()-t0:.0f}s)")
        if not need:
            return day

        # LANGKAH 3: funding
        self.fund_wallets(need)

        # LANGKAH 4: entry per batch
        # Tiap wallet punya nonce sendiri, jadi entry boleh dikirim paralel.
        # Untuk jumlah besar, menunggu receipt satu per satu terlalu lambat
        # (5000 wallet x ~3 detik = 4 jam hanya untuk menunggu).
        fast = len(need) >= int(os.getenv("RH_FAST_THRESHOLD", "300"))
        batches = [need[i:i + BATCH_SIZE] for i in range(0, len(need), BATCH_SIZE)]
        log(f"Mulai entry: {len(need)} wallet dalam {len(batches)} batch "
            f"(@{BATCH_SIZE})" + (f" | mode cepat: {WORKERS} paralel" if fast else ""))
        sukses = gagal = 0
        for bi, batch in enumerate(batches, 1):
            if fast:
                def kirim(w: Wallet):
                    try:
                        return self.send_entry(w, day, wait=False)
                    except Exception as e:  # noqa: BLE001
                        log(f"    {w}: gagal -> {str(e)[:120]}")
                        return False
                with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                    for r in ex.map(kirim, batch):
                        sukses += 1 if r else 0
                        gagal += 0 if r else 1
            else:
                for w in batch:
                    try:
                        if self.send_entry(w, day):
                            sukses += 1
                        else:
                            gagal += 1
                    except Exception as e:  # noqa: BLE001
                        gagal += 1
                        log(f"    {w}: gagal -> {str(e)[:150]}")
                    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
            log(f"  batch {bi}/{len(batches)} selesai | sukses {sukses} gagal {gagal} "
                f"| jalan {hhmm(time.time()-t0)}")
            if bi < len(batches):
                time.sleep(1 if fast else BATCH_PAUSE)

        log(f"Putaran selesai: {sukses} entry sukses, {gagal} gagal, "
            f"total waktu {hhmm(time.time()-t0)}")
        return day

    def loop(self) -> None:
        """Jalan terus, bangun tepat setelah daily reset 14:00 UTC.

        Waktu tidur dihitung dari currentDay() kontrak, bukan 24 jam kaku,
        supaya tidak pernah bergeser walau bot pernah restart atau sempat
        error di tengah hari.
        """
        # Kalau bot baru dinyalakan dan reset baru saja lewat, tunggu dulu
        # sampai jeda 1 jam terpenuhi. Tanpa ini, restart tepat setelah reset
        # akan langsung menembak tx di jam tersibuk.
        try:
            d0 = self.current_day()
            mulai = d0 * DAY_SECONDS + DAY_OFFSET + DELAY_AFTER_RESET
            kurang = mulai - int(time.time())
            if kurang > 0:
                m = datetime.fromtimestamp(mulai, timezone.utc)
                log(f"Reset baru lewat. Tunggu {kurang//60} menit lagi "
                    f"(mulai {m:%H:%M} UTC / {m + timedelta(hours=7):%H:%M} WIB).")
                log("  Mau langsung jalan tanpa tunggu? pakai: ./rh once")
                time.sleep(kurang)
        except Exception:  # noqa: BLE001
            pass

        while True:
            try:
                day = self.run_round()
                target = next_rollover_ts(day)
            except Exception as e:  # noqa: BLE001
                log(f"Putaran error ({type(e).__name__}: {e}), ulang 5 menit.")
                time.sleep(300)
                continue

            # Tunggu DELAY_AFTER_RESET setelah reset, baru kirim tx.
            # Jitter kecil ditambahkan supaya semua wallet tidak berangkat
            # di detik yang sama persis.
            wake = target + DELAY_AFTER_RESET + random.randint(0, 300)
            sleep_for = max(30, wake - int(time.time()))
            tgt = datetime.fromtimestamp(target, timezone.utc)
            wk = datetime.fromtimestamp(wake, timezone.utc)
            sisa = int(sleep_for)
            log(f"Daily reset {tgt:%H:%M} UTC ({tgt + timedelta(hours=7):%H:%M} WIB) | "
                f"mulai tx {wk:%H:%M} UTC ({wk + timedelta(hours=7):%H:%M} WIB, "
                f"+{DELAY_AFTER_RESET//3600}j setelah reset)")
            log(f"  bangun dalam {sisa//3600:02d}:{(sisa%3600)//60:02d}:{sisa%60:02d}")

            # Tidur dipecah jadi potongan pendek supaya proses tetap responsif
            # dan bisa dihentikan kapan saja. Jeda 1 jam tetap dihormati: bot
            # TIDAK bangun lebih awal walau hari kontrak sudah berganti.
            deadline = time.time() + sleep_for
            while True:
                sisa_tidur = deadline - time.time()
                if sisa_tidur <= 0:
                    break
                time.sleep(min(300, max(1, sisa_tidur)))


# ----------------------------------------------------------------------------
def load_wallets(groups: set[str] | None, limit: int | None,
                 offset: int) -> tuple[Wallet | None, list[Wallet]]:
    if not WALLETS_FILE.exists():
        log(f"{WALLETS_FILE} tidak ada. Bikin dulu:")
        log("  ./rh setup 100        (bikin funder + 100 wallet sekaligus)")
        raise SystemExit(1)
    if WALLETS_FILE.stat().st_mode & 0o077:
        log(f"WARNING: {WALLETS_FILE} bisa dibaca user lain. chmod 600 {WALLETS_FILE}")

    try:
        data = json.loads(WALLETS_FILE.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f"{WALLETS_FILE} rusak ({e}). Pulihkan dari folder backups/.") from e

    f = data.get("funder")
    funder = Wallet(Web3.to_checksum_address(f["address"]), f["private_key"], "funder") if f else None

    ws: list[Wallet] = []
    for w in data.get("wallets", []):
        if not w.get("enabled", True):
            continue
        g = w.get("group", "default")
        if groups and g not in groups:
            continue
        ws.append(Wallet(Web3.to_checksum_address(w["address"]), w["private_key"],
                         w.get("label", ""), g))
    ws = ws[offset:]
    if limit is not None:
        ws = ws[:limit]
    return funder, ws


def arg_val(name: str, default=None):
    for i, a in enumerate(sys.argv):
        if a == name and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def main() -> int:
    once = "--once" in sys.argv
    dry = "--dry-run" in sys.argv
    no_fund = "--no-fund" in sys.argv
    rpc_only = "--rpc" in sys.argv          # cuma cek kesehatan RPC lalu keluar

    g = arg_val("--group")
    groups = {x.strip() for x in g.split(",")} if g else None
    limit = arg_val("--limit")
    limit = int(limit) if limit else None
    offset = int(arg_val("--offset", 0) or 0)

    pool = RpcPool()
    if rpc_only:
        log(pool.summary())
        sehat = 0
        for nm, ok, info in pool.health_check(CHAIN_ID):
            log(f"  {'OK  ' if ok else 'MATI'} {nm:<12} {info}")
            sehat += 1 if ok else 0
        log(f"{sehat} RPC sehat.")
        return 0 if sehat else 1

    # koneksi utama: ambil RPC pertama yang menjawab dengan chain id benar.
    # Kena 429 di sini bukan tanda RPC mati, cuma sedang sibuk -> jangan dibuang.
    w3 = None
    for _ in range(max(4, len(pool.private) + 2)):
        cand, url = pool.web3()
        try:
            if cand.eth.chain_id == CHAIN_ID:
                w3 = cand
                log(f"{pool.summary()} | koneksi utama: {pool.name(url)}")
                break
        except Exception as e:  # noqa: BLE001
            if not _is_rate_limit(e):
                pool.mark_bad(url)
            time.sleep(0.4)
    if w3 is None:
        log("Semua RPC gagal dihubungi (termasuk publik). Cek dengan: ./rh rpc")
        return 1
    if w3.eth.chain_id != CHAIN_ID:
        log(f"Chain ID salah: {w3.eth.chain_id}, harus {CHAIN_ID}. Berhenti.")
        return 1
    if w3.eth.get_code(CONTRACT) in (b"", b"0x"):
        log("Tidak ada bytecode di alamat kontrak. Berhenti.")
        return 1

    funder, wallets = load_wallets(groups, limit, offset)
    if not wallets:
        log("Tidak ada wallet aktif yang cocok. Cek --group / --limit, "
            "atau tambah: python wallets.py new 100")
        return 1

    log(f"Robinhood Chain (chain {w3.eth.chain_id}) blok {w3.eth.block_number}")
    log(f"Funder {mask(funder.address) if funder else 'TIDAK ADA'} | "
        f"{len(wallets)} wallet aktif"
        + (f" | grup: {','.join(sorted(groups))}" if groups else "")
        + f" | {WORKERS} paralel, batch {BATCH_SIZE}")

    bot = AutoEntry(w3, None if no_fund else funder, wallets, pool)
    day = bot.current_day()
    log(f"currentDay()={day} paused={bot.is_paused()} "
        f"rollover={datetime.fromtimestamp(next_rollover_ts(day), timezone.utc):%Y-%m-%d %H:%M}Z")

    if dry:
        max_fee, _ = bot.fees()
        if funder:
            log(f"Funder saldo {w3.eth.get_balance(funder.address)/1e18:.8f} ETH")
        flagged = sum(1 for w in wallets if bot.is_flagged(w, day))
        cand = [w for w in wallets if not bot.is_flagged(w, day)]
        log(f"Flag hari ini: {flagged} sudah entry (skip tanpa RPC), {len(cand)} perlu dicek")
        if cand:
            need, already, errors = bot.check_many(cand)
            bals = bot.balances_many(cand)
            kurang = sum(1 for w in cand if 0 <= bals.get(w.address, 0) < ENTRY_GAS * max_fee)
            log(f"Perlu entry {len(need)} | sudah entry {len(already)} | error {errors}")
            log(f"Perlu top-up gas: {kurang} wallet")
            log(f"Biaya 1 entry ~{ENTRY_GAS*max_fee/1e18:.8f} ETH | "
                f"estimasi total {len(need)*ENTRY_GAS*max_fee/1e18:.6f} ETH")
        return 0

    if once:
        bot.run_round()
        return 0
    bot.loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
