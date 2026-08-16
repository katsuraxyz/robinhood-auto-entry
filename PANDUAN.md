# Panduan Auto-Entry — Wallet Tanpa Batas

Kontrak `0x3FB5C23cE237A63CCBF9c4FD0F6d4E4Cd25BE4F9` · Robinhood Chain (4663)

Tiga jawaban langsung:

1. **Mau 200/300/400/berapa pun?** `wallets.py new 400`. Tidak ada batas di kode.
2. **Sudah run 100, mau nambah lagi?** `wallets.py new 200 batch2`. Wallet lama
   TIDAK tertimpa, nomornya lanjut (w101, w102, ...).
3. **Flag 24 jam?** Wallet yang sudah entry hari ini dilewati **tanpa satu pun
   panggilan RPC dan tanpa kirim transaksi**. Nol gas terbuang.

---

## Cara nambah wallet kapan saja

```bash
./.venv/bin/python wallets.py new 100 batch1    # hari ini: 100
./.venv/bin/python wallets.py new 200 batch2    # besok: +200, total 300
./.venv/bin/python wallets.py new 400 batch3    # lusa:  +400, total 700
./.venv/bin/python wallets.py groups            # lihat semua grup
```

Contoh output `groups`:

```
grup               total   aktif
--------------------------------
batch1               100     100
batch2               200     200
batch3               100     100
--------------------------------
SEMUA                400     400
```

Grup itu opsional. Kalau tidak diisi, semua masuk grup `default`.

## Menjalankan sebagian wallet

```bash
auto_entry.py                              # semua wallet aktif
auto_entry.py --group batch2               # cuma grup batch2
auto_entry.py --group batch1,batch3        # dua grup sekaligus
auto_entry.py --limit 50                   # 50 wallet pertama
auto_entry.py --limit 100 --offset 200     # wallet ke-201 sampai 300
```

Berguna kalau mau uji grup baru dulu sebelum digabung ke jadwal utama.

## Cara kerja flag 24 jam

`state.json` menyimpan `{address: {day: tx_hash}}`. `day` dihitung dari rumus
kontrak: `(block.timestamp - 50400) / 86400`, jadi satu "hari" kontrak = 24 jam
penuh, ganti tiap 14:00 UTC / 21:00 WIB.

Urutan tiap putaran:

1. **Flag check** — wallet yang sudah tercatat untuk hari berjalan langsung
   dilewati. Nol RPC, nol gas.
2. **Verifikasi paralel** — sisanya disimulasi `eth_call`. Yang balasannya
   `0x2ed7f582` (sudah entry) langsung di-flag juga.
3. **Funding** — funder kirim gas hanya ke wallet yang saldonya kurang.
4. **Entry** — dikirim per batch.

Hasil tes nyata dengan 400 wallet, 300 di antaranya sudah di-flag:

```
flag check 400 wallet: 0.06 ms, panggilan RPC = 0
  -> 300 skip tanpa RPC, 100 perlu dicek
Setelah restart, masih ke-flag: 300/300
Flag utk day besok: 0
```

Artinya: bot direstart 100x dalam hari yang sama tetap tidak akan mengirim
transaksi ulang. Dan flag otomatis lepas sendiri saat hari kontrak berganti,
tidak perlu dihapus manual.

Kalau kamu entry manual lewat web, bot tetap tahu, karena langkah 2 memeriksa
kondisi sebenarnya di on-chain, bukan cuma percaya file lokal.

## Perkiraan waktu dan biaya

Dengan default (8 paralel, batch 25, delay 2-6 detik):

| Wallet | Waktu 1 putaran | Biaya/hari |
|---|---|---|
| 100 | ~15 menit | 0,00023 ETH |
| 200 | ~30 menit | 0,00046 ETH |
| 400 | ~60 menit | 0,00092 ETH |
| 1000 | ~2,5 jam | 0,0023 ETH |

Kalau semua wallet sudah entry, putaran berikutnya selesai dalam hitungan
milidetik karena semua kena flag.

Jendela hariannya 24 jam, jadi bahkan 1000 wallet masih sangat longgar.

## Kenapa default 8 paralel, bukan 16

Saya ukur langsung ke RPC publik:

| Worker | Waktu 100 wallet | Error |
|---|---|---|
| 8 | 5,9 detik | 1% |
| 12 | 7,7 detik | 7% |
| 16 | 9,6 detik | 52% |

Di atas 8 worker, RPC mulai menolak request. Lebih banyak worker justru lebih
lambat DAN lebih banyak error. Bot juga punya retry otomatis (3x dengan jeda
bertambah) untuk error jaringan. Penting: revert kontrak tidak pernah di-retry,
karena itu jawaban pasti, bukan gangguan jaringan.

Kalau kamu punya RPC pribadi yang lebih kuat:

```bash
RH_WORKERS=24 ./.venv/bin/python auto_entry.py
```

## Setup dari nol

```bash
mkdir -p ~/robinhood-auto-entry && cd ~/robinhood-auto-entry
python3 -m venv .venv
./.venv/bin/pip install web3

./.venv/bin/python wallets.py init            # bikin funder, CATAT address-nya
./.venv/bin/python wallets.py new 400 batch1  # bikin 400 wallet (~1 detik)
./.venv/bin/python wallets.py verify          # pastikan semua pk cocok
./.venv/bin/python wallets.py export ~/keys.csv   # backup KELUAR dari server

# kirim ETH ke address funder (0,01 ETH cukup ~10 hari utk 400 wallet)

./.venv/bin/python auto_entry.py --dry-run    # cek, tanpa kirim tx
./.venv/bin/python auto_entry.py --once       # 1 putaran nyata
./.venv/bin/python auto_entry.py              # loop harian otomatis
```

Systemd untuk 24/7:

```bash
sudo cp robinhood-auto-entry.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now robinhood-auto-entry
journalctl -u robinhood-auto-entry -f
```

## Daftar perintah

```
wallets.py init                  bikin funder baru
wallets.py new 400 [grup]        tambah 400 wallet (nambah, tidak menimpa)
wallets.py groups                daftar grup + jumlah
wallets.py stats                 ringkasan cepat, TANPA RPC
wallets.py list                  saldo + status hari ini (pakai RPC)
wallets.py verify                cek keutuhan file & kecocokan pk
wallets.py backup                salinan bertanggal
wallets.py export file.csv       export address+pk ke CSV
wallets.py import 0xKEY label    tambah wallet yang sudah ada
wallets.py import-funder 0xKEY   set funder dari pk yang sudah ada
wallets.py disable 0xADDR        nonaktifkan (aman, tidak menghapus)
wallets.py enable 0xADDR         aktifkan lagi
wallets.py remove 0xADDR         hapus permanen (minta konfirmasi)

auto_entry.py --dry-run          cek saja, tanpa kirim tx
auto_entry.py --once             1 putaran lalu keluar
auto_entry.py                    loop harian otomatis
auto_entry.py --group NAMA       jalankan grup tertentu
auto_entry.py --limit N          batasi jumlah wallet
auto_entry.py --offset N         mulai dari wallet ke-N
auto_entry.py --no-fund          entry saja, tanpa funding
```

## Pengaturan

| Var | Default | Fungsi |
|---|---|---|
| `RH_WORKERS` | 8 | paralel saat cek status |
| `RH_BATCH` | 25 | wallet per batch saat entry |
| `RH_BATCH_PAUSE` | 15 | jeda detik antar batch |
| `RH_DELAY_MIN` / `RH_DELAY_MAX` | 2 / 6 | delay acak antar wallet |
| `RH_RETRIES` | 3 | ulangan kalau RPC error |
| `RH_TOPUP_ENTRIES` | 5 | top-up untuk berapa hari entry |
| `RH_GAS_MULT` | 1.25 | pengali gas price |
| `RH_WALLETS` | ./wallets.json | lokasi file wallet |
| `RH_BACKUP_DIR` | ./backups | folder backup |
| `RH_STATE` | ./state.json | lokasi file flag |

Hemat biaya transfer, top-up sekali untuk 30 hari:

```bash
RH_TOPUP_ENTRIES=30 ./.venv/bin/python auto_entry.py
```

## Perlindungan data wallet

| Risiko | Perlindungan |
|---|---|
| File ketimpa | `init` menolak kalau `wallets.json` sudah ada |
| Perubahan tak sengaja | Backup bertanggal sebelum SETIAP penulisan |
| Mati listrik saat menulis | Tulis atomik: tmp, fsync, replace |
| File korup | Ditolak dan TIDAK ditimpa, diarahkan ke backup |
| Salah hapus | `remove` minta ketik `HAPUS`, backup dulu |
| PK tidak cocok address | `wallets.py verify` |
| state.json korup | Dipindah ke `.corrupt`, bot lanjut jalan |
| Bocor ke publik | chmod 600 otomatis + `.gitignore` |

Sudah diuji: 400 wallet dibuat, file sengaja dirusak, ditolak tanpa ditimpa,
dipulihkan dari backup, hasilnya 400 wallet utuh dan semua pk cocok.

`state.json` dipangkas otomatis (simpan 7 hari terakhir) supaya tidak membengkak
walau jalan bertahun-tahun dengan ribuan wallet.

## Kalau ada masalah

`wallets.json` rusak:

```bash
ls -lt backups/
cp backups/wallets-YYYYMMDD-HHMMSS-auto.json wallets.json
./.venv/bin/python wallets.py verify
```

Banyak "error" saat cek status: RPC kena rate limit. Turunkan `RH_WORKERS=4`.

"Tidak bisa konek RPC": tunggu 1-2 menit, RPC publik sedang membatasi. Bot dalam
mode loop akan mencoba lagi otomatis.

"gas kurang": isi ulang funder, lalu `auto_entry.py --once`.

## Risiko

`wallets.json` menyimpan private key dalam teks biasa. Siapa pun yang bisa akses
server itu bisa menguras semua wallet. Pakai burner, jangan simpan aset bernilai.

Source kontrak belum diverifikasi, jadi nama fungsi aslinya tidak diketahui,
hanya selector `0xcd960f2b` dan perilakunya yang sudah dikonfirmasi on-chain.
Owner bisa `setPaused` atau mengubah aturan kapan saja.
