#!/usr/bin/env python3
"""
Pengelola wallets.json untuk auto-entry Robinhood Chain.

Struktur wallets.json:
{
  "funder":  {"address": "0x...", "private_key": "0x..."},
  "wallets": [
      {"address": "0x...", "private_key": "0x...", "label": "w1", "enabled": true}
  ]
}

Perintah:
  python wallets.py init                 # bikin file kosong + funder baru
  python wallets.py new 10 [grup]        # tambah 10 wallet baru (opsional grup)
  python wallets.py import-funder 0xKEY  # pakai funder yang sudah ada
  python wallets.py import 0xKEY [label] # tambah wallet entry dari private key
  python wallets.py list                 # tampilkan saldo + status entry hari ini
  python wallets.py disable 0xADDR       # matikan satu wallet tanpa menghapus
  python wallets.py enable 0xADDR
  python wallets.py remove 0xADDR
  python wallets.py backup               # bikin salinan bertanggal sekarang
  python wallets.py export file.csv      # export address+pk ke CSV
  python wallets.py verify               # cek file utuh, pk cocok dgn address
  python wallets.py groups               # daftar grup + jumlah wallet
  python wallets.py stats                # ringkasan cepat, tanpa panggil RPC
  python wallets.py funder               # cek saldo funder saja (1 panggilan RPC)

PERINGATAN: file ini menyimpan private key dalam bentuk teks biasa.
File otomatis di-chmod 600. Jangan commit ke git, jangan taruh di folder share.
Pakai wallet burner saja.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from eth_account import Account
from web3 import Web3

WALLETS_FILE = Path(os.getenv("RH_WALLETS", Path(__file__).parent / "wallets.json"))
BACKUP_DIR = Path(os.getenv("RH_BACKUP_DIR", WALLETS_FILE.parent / "backups"))
from rpc_pool import RpcPool, PUBLIC_RPC


def _rpc() -> str:
    """RPC pertama yang sehat dari rpcs.txt, fallback ke publik."""
    return RpcPool().pick()


def _rpc_name() -> str:
    """Nama RPC untuk log, tanpa membocorkan API key."""
    pool = RpcPool()
    return pool.name(pool.pick())


RPC_URL = os.getenv("RH_RPC", PUBLIC_RPC)

ENTRY_SELECTOR = "0xcd960f2b"
ERR_ALREADY = "2ed7f582"
SEL_CURRENT_DAY = "0x5c9302c9"


def mask(a: str) -> str:
    return f"{a[:6]}…{a[-4:]}"


def load() -> dict:
    if not WALLETS_FILE.exists():
        return {"funder": None, "wallets": []}
    try:
        data = json.loads(WALLETS_FILE.read_text())
    except json.JSONDecodeError as e:
        # jangan pernah menimpa file rusak; arahkan ke backup
        raise SystemExit(
            f"{WALLETS_FILE} rusak / bukan JSON valid ({e}).\n"
            f"JANGAN jalankan perintah yang menulis file.\n"
            f"Cek salinan di {BACKUP_DIR} dan pulihkan manual."
        ) from e
    data.setdefault("funder", None)
    data.setdefault("wallets", [])
    return data


def make_backup(tag: str = "auto") -> Path | None:
    """Salin wallets.json ke folder backups sebelum ditulis ulang."""
    if not WALLETS_FILE.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dst = BACKUP_DIR / f"wallets-{stamp}-{tag}.json"
    shutil.copy2(WALLETS_FILE, dst)
    os.chmod(dst, 0o600)
    return dst


def save(data: dict) -> None:
    # selalu backup versi lama dulu, jadi tidak ada perubahan yang tak bisa dibatalkan
    bak = make_backup()

    payload = json.dumps(data, indent=2)
    # tulis atomik: file sementara -> fsync -> replace
    tmp = WALLETS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)

    # verifikasi hasil tulis sebelum menggantikan file asli
    check = json.loads(tmp.read_text())
    if len(check.get("wallets", [])) != len(data.get("wallets", [])):
        tmp.unlink(missing_ok=True)
        raise SystemExit("Verifikasi tulis gagal, file asli tidak diubah.")

    tmp.replace(WALLETS_FILE)
    os.chmod(WALLETS_FILE, 0o600)
    msg = f"Tersimpan di {WALLETS_FILE} (permission 600)"
    if bak:
        msg += f" | backup lama: {bak.name}"
    print(msg)


def norm_key(k: str) -> str:
    k = k.strip()
    if not k.startswith("0x"):
        k = "0x" + k
    if len(k) != 66:
        raise SystemExit(f"Private key tidak valid: panjang {len(k)}, harus 66 karakter.")
    Account.from_key(k)  # validasi
    return k


def pk_hex(acct) -> str:
    h = acct.key.hex()
    return h if h.startswith("0x") else "0x" + h


def make_wallet(label: str, group: str = "default") -> dict:
    acct = Account.from_key("0x" + secrets.token_hex(32))
    return {"address": acct.address, "private_key": pk_hex(acct),
            "label": label, "group": group, "enabled": True}


def cmd_init() -> None:
    if WALLETS_FILE.exists():
        data = load()
        raise SystemExit(
            f"{WALLETS_FILE} SUDAH ADA (funder + {len(data['wallets'])} wallet).\n"
            f"init dibatalkan supaya private key yang ada tidak tertimpa.\n"
            f"Kalau mau menambah wallet:      wallets.py new 10\n"
            f"Kalau memang mau mulai dari nol: backup dulu, lalu hapus file itu manual."
        )
    acct = Account.from_key("0x" + secrets.token_hex(32))
    data = {"funder": {"address": acct.address, "private_key": pk_hex(acct)}, "wallets": []}
    save(data)
    print(f"\nFunder baru dibuat: {acct.address}")
    print("Kirim ETH ke alamat funder itu untuk biaya gas semua wallet entry.")


def cmd_backup() -> None:
    if not WALLETS_FILE.exists():
        raise SystemExit(f"{WALLETS_FILE} belum ada.")
    dst = make_backup("manual")
    print(f"Backup dibuat: {dst}")
    n = len(list(BACKUP_DIR.glob("wallets-*.json")))
    print(f"Total {n} salinan di {BACKUP_DIR}")


def cmd_export(path: str) -> None:
    data = load()
    out = Path(path)
    lines = ["label,address,private_key,enabled"]
    f = data.get("funder")
    if f:
        lines.append(f"funder,{f['address']},{f['private_key']},true")
    for w in data["wallets"]:
        lines.append(f"{w['label']},{w['address']},{w['private_key']},"
                     f"{str(w.get('enabled', True)).lower()}")
    out.write_text("\n".join(lines) + "\n")
    os.chmod(out, 0o600)
    print(f"{len(data['wallets'])} wallet + funder diexport ke {out} (permission 600)")
    print("File ini berisi private key. Simpan offline, jangan taruh di cloud share.")


def cmd_verify() -> None:
    """Pastikan file utuh dan tiap private key benar-benar cocok dengan address."""
    data = load()
    problems = 0
    f = data.get("funder")
    if f:
        derived = Account.from_key(f["private_key"]).address
        ok = derived.lower() == f["address"].lower()
        print(f"funder {f['address']}  pk cocok: {ok}")
        problems += 0 if ok else 1
    else:
        print("funder: BELUM ADA")

    seen: dict[str, str] = {}
    for w in data["wallets"]:
        try:
            derived = Account.from_key(w["private_key"]).address
        except Exception as e:  # noqa: BLE001
            print(f"  {w.get('label')} {w.get('address')}: private key INVALID ({e})")
            problems += 1
            continue
        if derived.lower() != w["address"].lower():
            print(f"  {w['label']}: address TIDAK COCOK dengan pk "
                  f"(tertulis {w['address']}, seharusnya {derived})")
            problems += 1
        if derived.lower() in seen:
            print(f"  {w['label']}: DUPLIKAT dengan {seen[derived.lower()]}")
            problems += 1
        seen[derived.lower()] = w["label"]

    nbak = len(list(BACKUP_DIR.glob("wallets-*.json"))) if BACKUP_DIR.exists() else 0
    print(f"\n{len(data['wallets'])} wallet diperiksa, {problems} masalah. "
          f"{nbak} backup tersedia di {BACKUP_DIR}")
    if problems:
        raise SystemExit(1)
    print("Semua private key cocok dengan address-nya.")


def cmd_import_funder(key: str) -> None:
    data = load()
    k = norm_key(key)
    acct = Account.from_key(k)
    data["funder"] = {"address": acct.address, "private_key": k}
    save(data)
    print(f"Funder di-set: {acct.address}")


def cmd_new(n: int, group: str = "default") -> None:
    data = load()
    start = len(data["wallets"]) + 1
    existing = {w["address"].lower() for w in data["wallets"]}
    added = 0
    for i in range(n):
        w = make_wallet(f"w{start + i}", group)
        if w["address"].lower() in existing:
            continue
        data["wallets"].append(w)
        existing.add(w["address"].lower())
        added += 1
        if n <= 20:
            print(f"  + {w['label']}  {w['address']}  [{group}]")
    save(data)
    print(f"{added} wallet baru ditambahkan ke grup '{group}'. "
          f"Total sekarang: {len(data['wallets'])} wallet")
    print(f"Jalankan grup ini saja: auto_entry.py --group {group}")


def cmd_groups() -> None:
    data = load()
    counts: dict[str, list[int]] = {}
    for w in data["wallets"]:
        g = w.get("group", "default")
        c = counts.setdefault(g, [0, 0])
        c[0] += 1
        if w.get("enabled", True):
            c[1] += 1
    if not counts:
        print("Belum ada wallet. Tambah: wallets.py new 100 batch1")
        return
    print(f"{'grup':<16} {'total':>7} {'aktif':>7}")
    print("-" * 32)
    for g in sorted(counts):
        t, a = counts[g]
        print(f"{g:<16} {t:>7} {a:>7}")
    print("-" * 32)
    print(f"{'SEMUA':<16} {len(data['wallets']):>7} "
          f"{sum(c[1] for c in counts.values()):>7}")


def cmd_funder() -> None:
    """Cek saldo funder saja. Cepat, cuma 1 panggilan RPC, tidak tergantung
    jumlah wallet entry."""
    data = load()
    f = data.get("funder")
    if not f:
        raise SystemExit("Funder belum ada. Jalankan: ./rh setup 100")

    w3 = Web3(Web3.HTTPProvider(_rpc(), request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise SystemExit("Tidak bisa konek RPC. Cek: ./rh rpc")

    addr = Web3.to_checksum_address(f["address"])
    bal = w3.eth.get_balance(addr)
    gp = w3.eth.gas_price
    max_fee = max(1, int(gp * 1.25))
    entry_cost = 84_000 * max_fee
    xfer_cost = 21_000 * max_fee

    aktif = sum(1 for w in data["wallets"] if w.get("enabled", True))

    print(f"FUNDER  {addr}")
    print(f"Saldo   {bal/1e18:.8f} ETH  ({bal} wei)")
    print(f"Gas     {gp/1e9:.6f} gwei")
    print()
    print(f"Wallet entry aktif : {aktif}")
    print(f"Biaya 1 entry      : {entry_cost/1e18:.8f} ETH")
    print(f"Biaya 1 transfer   : {xfer_cost/1e18:.8f} ETH")

    if aktif:
        per_hari = aktif * entry_cost
        # transfer terjadi tiap RH_TOPUP_ENTRIES hari
        topup_n = int(os.getenv("RH_TOPUP_ENTRIES", "5"))
        per_hari_all = per_hari + aktif * xfer_cost / max(1, topup_n)
        print(f"Biaya per hari     : {per_hari_all/1e18:.8f} ETH "
              f"(untuk {aktif} wallet)")
        if per_hari_all > 0:
            hari = int(bal / per_hari_all)
            print()
            if hari >= 1:
                print(f"CUKUP UNTUK ~{hari} HARI dengan {aktif} wallet.")
            else:
                kurang = per_hari_all - bal
                print(f"SALDO KURANG untuk 1 hari penuh. "
                      f"Perlu tambah ~{kurang/1e18:.8f} ETH.")
            print(f"Saran isi 30 hari  : {per_hari_all*30/1e18:.6f} ETH")


def cmd_stats() -> None:
    """Ringkasan tanpa RPC, aman untuk ribuan wallet."""
    data = load()
    state_file = Path(os.getenv("RH_STATE", WALLETS_FILE.parent / "state.json"))
    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except json.JSONDecodeError:
            print("state.json rusak, hitungan flag dilewati.")
    print(f"File   : {WALLETS_FILE}")
    print(f"Funder : {data['funder']['address'] if data.get('funder') else 'BELUM ADA'}")
    print(f"Wallet : {len(data['wallets'])} total, "
          f"{sum(1 for w in data['wallets'] if w.get('enabled', True))} aktif")
    nbak = len(list(BACKUP_DIR.glob('wallets-*.json'))) if BACKUP_DIR.exists() else 0
    print(f"Backup : {nbak} salinan di {BACKUP_DIR}")
    if state:
        days = sorted({d for v in state.values() for d in v}, reverse=True)[:3]
        for d in days:
            n = sum(1 for v in state.values() if d in v)
            print(f"  day {d}: {n} wallet tercatat sudah entry")


def cmd_import(key: str, label: str | None) -> None:
    data = load()
    k = norm_key(key)
    acct = Account.from_key(k)
    if any(w["address"].lower() == acct.address.lower() for w in data["wallets"]):
        raise SystemExit(f"{acct.address} sudah ada di daftar.")
    data["wallets"].append({
        "address": acct.address, "private_key": k,
        "label": label or f"w{len(data['wallets']) + 1}", "enabled": True,
    })
    save(data)
    print(f"Ditambahkan: {acct.address}")


def _set_enabled(addr: str, val: bool) -> None:
    data = load()
    hit = False
    for w in data["wallets"]:
        if w["address"].lower() == addr.lower():
            w["enabled"] = val
            hit = True
    if not hit:
        raise SystemExit(f"{addr} tidak ditemukan.")
    save(data)
    print(f"{addr} -> enabled={val}")


def cmd_remove(addr: str) -> None:
    data = load()
    hit = [w for w in data["wallets"] if w["address"].lower() == addr.lower()]
    if not hit:
        raise SystemExit(f"{addr} tidak ditemukan.")

    w = hit[0]
    # remove itu permanen, jadi minta konfirmasi eksplisit dulu
    print(f"Akan MENGHAPUS PERMANEN dari wallets.json:")
    print(f"  {w['label']}  {w['address']}")
    print("Private key wallet ini akan hilang dari file utama.")
    print("Kalau cuma ingin menonaktifkan, pakai: wallets.py disable <address>")
    if input("Ketik 'HAPUS' untuk lanjut: ").strip() != "HAPUS":
        print("Dibatalkan, tidak ada yang diubah.")
        return

    make_backup("sebelum-remove")
    data["wallets"] = [x for x in data["wallets"] if x["address"].lower() != addr.lower()]
    save(data)
    print(f"{addr} dihapus (salinan lama tetap ada di {BACKUP_DIR}).")


def cmd_list() -> None:
    data = load()
    only = os.getenv("RH_GROUP")
    w3 = Web3(Web3.HTTPProvider(_rpc(), request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise SystemExit("Tidak bisa konek RPC. Cek: ./rh rpc")
    day = int.from_bytes(w3.eth.call({"to": Web3.to_checksum_address(
        "0x3FB5C23cE237A63CCBF9c4FD0F6d4E4Cd25BE4F9"), "data": SEL_CURRENT_DAY}), "big")

    f = data.get("funder")
    print(f"RPC {_rpc_name()}  |  day {day}\n")
    if f:
        bal = w3.eth.get_balance(Web3.to_checksum_address(f["address"]))
        print(f"FUNDER  {f['address']}  {bal/1e18:.8f} ETH")
    else:
        print("FUNDER  (belum di-set, jalankan: wallets.py init)")
    print()

    rows = [w for w in data["wallets"]
            if not only or w.get("group", "default") == only]
    if not rows:
        print("Belum ada wallet entry. Tambah dengan: wallets.py new 5")
        return
    if len(rows) > 60:
        print(f"{len(rows)} wallet. Menampilkan ringkasan saja "
              f"(pakai wallets.py stats untuk info cepat tanpa RPC).\n")

    total = 0
    n_sudah = n_belum = n_lain = 0
    show = len(rows) <= 60
    if show:
        print(f"{'label':<6} {'grup':<10} {'address':<44} {'saldo ETH':>13}  status")
        print("-" * 98)
    for i, w in enumerate(rows, 1):
        addr = Web3.to_checksum_address(w["address"])
        bal = w3.eth.get_balance(addr)
        total += bal
        try:
            w3.eth.call({"from": addr, "to": Web3.to_checksum_address(
                "0x3FB5C23cE237A63CCBF9c4FD0F6d4E4Cd25BE4F9"),
                "data": ENTRY_SELECTOR, "value": 0})
            status = "belum entry"
            n_belum += 1
        except Exception as e:  # noqa: BLE001
            if ERR_ALREADY in str(getattr(e, "data", e)).lower():
                status = "sudah entry"
                n_sudah += 1
            else:
                status = "revert lain"
                n_lain += 1
        if show:
            flag = "" if w.get("enabled", True) else "  [off]"
            print(f"{w['label']:<6} {w.get('group','default'):<10} {addr:<44} "
                  f"{bal/1e18:>13.8f}  {status}{flag}")
        elif i % 50 == 0:
            print(f"  ...dicek {i}/{len(rows)}")
    if show:
        print("-" * 98)
    print(f"\n{len(rows)} wallet, total saldo {total/1e18:.8f} ETH")
    print(f"sudah entry: {n_sudah}  |  belum entry: {n_belum}  |  revert lain: {n_lain}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    a = sys.argv[2:]
    if cmd == "init":
        cmd_init()
    elif cmd == "new":
        cmd_new(int(a[0]) if a else 1, a[1] if len(a) > 1 else "default")
    elif cmd == "import-funder":
        cmd_import_funder(a[0])
    elif cmd == "import":
        cmd_import(a[0], a[1] if len(a) > 1 else None)
    elif cmd == "list":
        cmd_list()
    elif cmd == "groups":
        cmd_groups()
    elif cmd == "stats":
        cmd_stats()
    elif cmd == "funder":
        cmd_funder()
    elif cmd == "backup":
        cmd_backup()
    elif cmd == "export":
        cmd_export(a[0] if a else "wallets-export.csv")
    elif cmd == "verify":
        cmd_verify()
    elif cmd == "disable":
        _set_enabled(a[0], False)
    elif cmd == "enable":
        _set_enabled(a[0], True)
    elif cmd == "remove":
        cmd_remove(a[0])
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
