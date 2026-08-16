# Robinhood Chain Auto-Entry Bot

Bot auto-entry harian untuk WL Lottery di Robinhood Chain (chain ID 4663).
Mendukung wallet tanpa batas jumlah, satu funder, rotasi multi-RPC, dan flag
harian supaya tidak ada gas terbuang.

Kontrak: `0x3FB5C23cE237A63CCBF9c4FD0F6d4E4Cd25BE4F9`

## Fitur

- **Generate wallet otomatis** — `./rh new 400` bikin 400 wallet dalam ~1 detik
- **Satu funder** — kirim ETH sekali ke funder, dia yang membagi gas ke semua wallet
- **Flag 24 jam** — wallet yang sudah entry dilewati tanpa panggilan RPC sama sekali
- **Rotasi multi-RPC** — pakai banyak RPC bergiliran, auto-skip yang error,
  fallback ke RPC publik kalau semua RPC pribadi mati
- **Jadwal mengikuti kontrak** — reset harian dibaca dari `currentDay()`, bukan
  timer 24 jam kaku, jadi tidak pernah bergeser walau bot restart
- **Grup wallet** — jalankan sebagian saja dengan `--group` / `--limit` / `--offset`
- **Proteksi data** — backup otomatis, tulis atomik, verifikasi private key

## Fakta hasil verifikasi on-chain

Source kontrak tidak terverifikasi di explorer. Semua di bawah ini diturunkan
dari bytecode dan transaksi nyata.

| Item | Nilai |
|---|---|
| Fungsi entry | selector `0xcd960f2b`, tanpa argumen |
| msg.value | 0 (gratis, hanya bayar gas) |
| Gas entry | 83.229 (tx nyata) |
| Gas transfer | 21.155 — **21.000 ditolak** (`intrinsic gas too low`) |
| Error "sudah entry" | custom error `0x2ed7f582` |
| Rumus hari | `(block.timestamp - 50400) / 86400` |
| Daily reset | 14:00 UTC |

Batas: 1x entry per wallet per hari.

## Instalasi

```bash
git clone <repo-url> robinhood-auto-entry
cd robinhood-auto-entry

python3 -m venv .venv
./.venv/bin/pip install web3
```

RPC pribadi (opsional tapi sangat disarankan):

```bash
cp rpcs.txt.example rpcs.txt
# isi dengan RPC punyamu, satu URL per baris
chmod 600 rpcs.txt
./rh rpc          # tes semua RPC
```

Tanpa `rpcs.txt`, bot memakai RPC publik dengan paralel lebih rendah (8 vs 24).

## Pemakaian

```bash
./rh setup 400 batch1              # bikin funder + 400 wallet
./rh export ~/keys-backup.csv      # BACKUP, simpan keluar dari server
                                   # kirim ETH ke address funder
./rh funder                        # cek saldo + cukup berapa hari
./rh check                         # cek tanpa kirim tx
./rh once                          # 1 putaran nyata
./rh bg                            # jalan otomatis 24/7
./rh log                           # pantau
```

Tambah wallet kapan saja (yang lama tidak tertimpa):

```bash
./rh new 400 batch2
./rh bg                            # restart, otomatis pakai semua
```

Coba sebagian dulu:

```bash
./rh once --limit 50               # 50 wallet pertama
./rh once --group batch2           # grup tertentu
./rh once --limit 50 --offset 50   # wallet ke-51 s/d 100
```

## Daftar perintah

```
./rh setup N [grup]     bikin funder + N wallet
./rh new N [grup]       tambah N wallet
./rh check              cek, tanpa kirim tx
./rh once               1 putaran
./rh start              loop harian (foreground)
./rh bg                 jalan 24/7 via systemd
./rh stop               hentikan
./rh log                log realtime
./rh status             status service
./rh rpc                tes semua RPC
./rh funder             saldo funder + estimasi hari
./rh stats              ringkasan cepat, tanpa RPC
./rh groups             daftar grup
./rh list               saldo + status per wallet
./rh verify             cek semua pk cocok dgn address
./rh backup             salinan bertanggal
./rh export f.csv       export address + pk
./rh set-funder 0xPK    pakai funder yang sudah ada
./rh disable 0xADDR     nonaktifkan wallet
./rh enable 0xADDR      aktifkan lagi
./rh remove 0xADDR      hapus permanen (minta konfirmasi)
```

## Pengaturan

| Var | Default | Fungsi |
|---|---|---|
| `RH_DELAY_AFTER_RESET` | 3600 | jeda detik setelah reset sebelum kirim tx |
| `RH_WORKERS` | 24 / 8 | paralel (24 kalau ada rpcs.txt) |
| `RH_BATCH` | 25 | wallet per batch |
| `RH_TOPUP_ENTRIES` | 5 | top-up untuk berapa hari entry |
| `RH_GAS_MULT` | 1.5 | pengali gas price |
| `RH_TRANSFER_GAS` | 23000 | gas untuk transfer funder |
| `RH_FAST_THRESHOLD` | 300 | ambang wallet untuk mode cepat |
| `RH_RETRIES_429` | 8 | ulangan saat RPC rate limit |

## Biaya

Pada gas 0,021 gwei:

| | Per wallet |
|---|---|
| 1 entry | 0,0000018 ETH |
| 1 transfer | 0,0000005 ETH |

Sekitar **$1,28 per 50 wallet untuk 5 hari**.

| Wallet | Per hari |
|---|---|
| 100 | 0,00019 ETH |
| 400 | 0,00076 ETH |
| 800 | 0,00145 ETH |

## Kinerja

800 wallet, dengan 11 RPC Alchemy:

- Cek status: ~5 detik
- Funding 400 wallet: ~2 menit
- Entry 400 wallet: ~40 detik
- Putaran penuh saat semua sudah entry: instan (0 RPC)

## Keamanan

`wallets.json` menyimpan private key dalam teks biasa, `rpcs.txt` menyimpan API
key. Keduanya di-chmod 600 dan sudah masuk `.gitignore`. **Jangan commit.**

Pakai wallet burner. Jangan simpan aset bernilai di wallet-wallet ini.

Source kontrak belum diverifikasi, jadi nama fungsi aslinya tidak diketahui,
hanya selector dan perilakunya yang sudah dikonfirmasi on-chain. Owner kontrak
bisa mem-pause atau mengubah aturan kapan saja.

## Lisensi

MIT. Pakai atas risiko sendiri.
