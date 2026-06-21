# Panduan Poly V3 AWS EC2 24/7

Panduan ini menjelaskan cara menjalankan Poly V3 secara terus-menerus (24/7) pada VPS AWS EC2 menggunakan `systemd`.

Gunakan EC2 untuk proses bot yang selalu aktif. Jangan menggunakan Vercel untuk menjalankan bot (executor): fungsi serverless dibatasi oleh waktu/permintaan (request-bound), sementara bot ini memerlukan loop pasar yang berjalan terus-menerus, polling orderbook, status runtime lokal, dan pengawasan restart (restart supervision).

Tidak ada rahasia (secrets) yang disimpan dalam folder ini. Simpan nilai `.env` yang asli hanya pada instance EC2 Anda.

## Apa Saja yang Ditambahkan oleh Deploy Kit Ini

- `bootstrap_ubuntu.sh`: konfigurasi awal untuk sistem operasi Ubuntu.
- `systemd/*.service`: layanan (services) yang selalu aktif untuk bot, dashboard, dan tracker.
- `healthcheck.sh`: memeriksa status layanan serta endpoint HTTP dashboard/tracker.
- `update.sh`: alur pembaruan yang aman yang mencakup pencadangan (backup), `git pull`, pengujian (tests), dan restart.
- `backup_runtime.sh`: mengarsipkan data runtime (`runtime_data`).
- `restore_runtime.sh`: memulihkan cadangan runtime dan membiarkan bot dalam kondisi berhenti.
- `cloud-init-user-data.sh`: titik awal opsional untuk EC2 user-data.
- Dashboard `/api/health`: endpoint kesehatan non-rahasia untuk pemantauan lokal.

## Utamakan Pengamanan Biaya AWS (AWS Cost Guard)

Sebelum meluncurkan apa pun:

1. Buka **AWS Billing / Cost Management**.
2. Buat **AWS Budget** untuk kredit Anda, misalnya dengan batas peringatan (alert thresholds) di `$20`, `$50`, dan `$90`.
3. Tambahkan notifikasi email Anda untuk menerima peringatan.
4. Periksa harga EC2 untuk wilayah (region) yang Anda pilih sebelum melakukan peluncuran.

Referensi AWS:
- Harga EC2: https://aws.amazon.com/ec2/pricing/on-demand/
- Security groups: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-security-groups.html
- Aturan SSH Security group: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/security-group-rules-reference.html
- Alarm penagihan (Billing alarms): https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/monitor_estimated_charges_with_cloudwatch.html

## Konfigurasi AWS yang Direkomendasikan

Mulai secara konservatif:

- **Region**: wilayah terdekat yang stabil dengan Anda, umumnya Singapura (`ap-southeast-1`) atau Tokyo (`ap-northeast-1`). Harga berbeda-beda di setiap wilayah.
- **AMI**: Ubuntu Server 24.04 LTS atau 22.04 LTS.
- **Instance**: `t3.small` x86_64, 2 vCPU, 2 GB RAM.
- **Storage**: 20 GB gp3.
- **Public IP**: ya, untuk koneksi SSH.
- **Security group**:
  - Inbound TCP 22 dari IP Anda saat ini saja.
  - Jangan buka inbound port 5004 atau 5005.
  - Outbound izinkan semua (allow all).

*Catatan: `t3.micro` dapat dicoba untuk mode simulasi (paper mode), namun bot ini menjalankan bot, dashboard, tracker, polling CLOB, dan penulisan berkas JSON sekaligus. `t3.small` adalah target awal yang lebih aman.*

## Model Keamanan

Jaga dashboard dan tracker tetap privat:
- `DASH_HOST=127.0.0.1`
- Akses hanya melalui SSH tunnel.
- Jangan membuka port `5004` or `5005` pada security group AWS.

Jaga kerahasiaan kunci (keys):
- Jangan pernah melakukan commit pada berkas `.env`.
- Jangan pernah menempelkan private key Polymarket ke dalam GitHub Actions, Vercel, atau log publik.
- Repositori ini sudah secara otomatis mengabaikan `.env`, `.env.*`, `runtime_data/`, `logs/`, dan `*.pem`.

---

## Langkah 1: Siapkan GitHub

Gunakan repositori GitHub privat jika memungkinkan.

Dari komputer lokal Anda:

```bash
git status
git remote -v
```

Pastikan berkas `.env` telah diabaikan oleh git:

```bash
git check-ignore .env
git check-ignore runtime_data/trades.json
```

Push kode ke GitHub. Jangan men-push data runtime atau informasi rahasia.

---

## Langkah 2: Luncurkan EC2

Di Konsol AWS:

1. Masuk ke **EC2** -> **Instances** -> **Launch instance**.
2. **Name**: `poly-v3-bot`.
3. **AMI**: Ubuntu Server LTS.
4. **Instance type**: `t3.small`.
5. **Key pair**: buat baru atau pilih yang sudah ada. Unduh berkas `.pem`.
6. **Network settings**:
   - Auto-assign public IP: **Enabled** (diaktifkan).
   - Security group inbound: **SSH**, TCP port 22, source: **My IP**.
   - Jangan menambahkan HTTP, HTTPS, port 5004, atau 5005.
7. **Storage**: 20 GB gp3.
8. Klik **Launch**.

Pada sistem Windows, atur izin berkas kunci (`.pem`) jika SSH menampilkan peringatan mengenai izin akses:

```powershell
icacls C:\path\poly-v3.pem /inheritance:r
icacls C:\path\poly-v3.pem /grant:r "$($env:USERNAME):R"
```

Hubungkan ke instance:

```powershell
ssh -i C:\path\poly-v3.pem ubuntu@EC2_PUBLIC_IP
```

---

## Langkah 3: Clone dan Bootstrap

Di terminal EC2 Anda:

```bash
sudo apt update
sudo apt install -y git
sudo mkdir -p /opt/poly-v3
sudo chown ubuntu:ubuntu /opt/poly-v3
git clone https://github.com/hengkikrs/polymarket-2.git /opt/poly-v3
cd /opt/poly-v3
bash deploy/aws/bootstrap_ubuntu.sh
```

Apa yang dilakukan oleh proses bootstrap:
1. Menginstal paket-paket sistem operasi (OS).
2. Membuat lingkungan virtual Python `.venv`.
3. Menginstal dependensi Python.
4. Membuat direktori `runtime_data`, `logs`, dan `backups`.
5. Membuat berkas `.env` dari `deploy/aws/env.example` jika belum ada.
6. Menginstal layanan `systemd`.
7. Menjalankan pengujian (tests).
8. Menjalankan bot, dashboard, dan tracker.

Jika repositori Anda privat, lakukan clone menggunakan SSH:

```bash
ssh-keygen -t ed25519 -C "poly-v3-ec2"
cat ~/.ssh/id_ed25519.pub
```

Tambahkan public key di atas ke GitHub sebagai **deploy key**, kemudian jalankan:

```bash
git clone git@github.com:hengkikrs/polymarket-2.git /opt/poly-v3
```

---

## Langkah 4: Konfigurasi `.env`

Ubah konfigurasi berkas `.env` hanya di dalam EC2:

```bash
nano /opt/poly-v3/.env
chmod 600 /opt/poly-v3/.env
```

Mulai terlebih dahulu dalam mode simulasi (**mock mode**):

```env
MOCK_MODE=true
DASH_HOST=127.0.0.1
DASH_PORT=5004
LOG_LEVEL=INFO
```

Isi kunci Polymarket hanya jika diperlukan. Untuk mode simulasi, kredensial dapat dikosongkan kecuali jika alur kode Anda saat ini membutuhkan pengecekan saldo riil yang terautentikasi.

Mode riil (**Live mode**) memerlukan konfirmasi eksplisit:

```env
MOCK_MODE=false
LIVE_TRADING_CONFIRM=I_UNDERSTAND_THIS_IS_REAL_MONEY
```

Untuk pengujian mode riil di awal, jaga ukuran transaksi agar tetap kecil:

```env
END_WINDOW_LIVE_TRADE_USD=1.00
SAFETY_MAX_LIVE_TRADE_USD=10.0
SAFETY_MAX_LIVE_WINDOW_EXPOSURE_USD=20.0
```

Lakukan restart layanan setelah melakukan perubahan pada `.env`:

```bash
sudo systemctl restart poly-bot poly-dashboard poly-tracker
```

---

## Langkah 5: Periksa Layanan (Services)

```bash
sudo systemctl status poly-bot
sudo systemctl status poly-dashboard
sudo systemctl status poly-tracker
```

Pantau log bot secara real-time:

```bash
sudo journalctl -u poly-bot -f
```

Jalankan pemeriksaan kesehatan (healthcheck):

```bash
cd /opt/poly-v3
bash deploy/aws/healthcheck.sh
```

Output yang diharapkan:

```text
OK service poly-bot
OK service poly-dashboard
OK service poly-tracker
OK http dashboard http://127.0.0.1:5004/api/health
OK http tracker http://127.0.0.1:5005/api/data
Healthcheck passed.
```

---

## Langkah 6: Buka Dashboard dengan Aman

Dari laptop Windows Anda, buka SSH tunnel untuk meneruskan port:

```powershell
ssh -i C:\path\poly-v3.pem -L 5004:127.0.0.1:5004 -L 5005:127.0.0.1:5005 ubuntu@EC2_PUBLIC_IP
```

Biarkan jendela SSH tersebut tetap terbuka, lalu buka peramban (browser) lokal Anda:
- http://localhost:5004
- http://localhost:5005

Jangan pernah mengekspos port 5004 atau 5005 secara publik kecuali Anda telah mengonfigurasi autentikasi yang tepat, TLS, whitelist IP, dan memahami risiko keamanannya.

---

## Langkah 7: Kontrol Status Trading

Sebisa mungkin gunakan tombol pada dashboard untuk mengontrol bot.

Pemeriksaan melalui CLI:

```bash
cat /opt/poly-v3/runtime_data/bot_control.json
curl -s http://127.0.0.1:5004/api/health
```

Jika Anda ingin menghentikan paksa aktivitas trading secara langsung pada tingkat berkas (file level):

```bash
cd /opt/poly-v3
python - <<'PY'
import json
from pathlib import Path
Path("runtime_data/bot_control.json").write_text(
    json.dumps({"trading_enabled": False}, indent=2),
    encoding="utf-8",
)
PY
sudo systemctl restart poly-bot
```

---

## Langkah 8: Perbarui Kode Bot

Gunakan skrip pembantu pembaruan (update helper):

```bash
cd /opt/poly-v3
bash deploy/aws/update.sh
```

Metode manual yang setara:

```bash
cd /opt/poly-v3
sudo systemctl stop poly-bot poly-dashboard poly-tracker
bash deploy/aws/backup_runtime.sh
git pull --ff-only
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
sudo systemctl restart poly-bot poly-dashboard poly-tracker
bash deploy/aws/healthcheck.sh
```

---

## Langkah 9: Cadangan (Backups) dan Pemulihan (Restore)

Membuat cadangan data runtime baru:

```bash
cd /opt/poly-v3
bash deploy/aws/backup_runtime.sh
```

Melihat daftar berkas cadangan:

```bash
ls -lh /opt/poly-v3/backups
```

Melakukan pemulihan (restore):

```bash
cd /opt/poly-v3
bash deploy/aws/restore_runtime.sh /opt/poly-v3/backups/runtime_data_YYYYmmddTHHMMSSZ.tar.gz
```

*Catatan: Proses pemulihan sengaja membiarkan bot dalam keadaan berhenti atau hanya memulai ulang dashboard/tracker tergantung pada perilaku skrip. Periksa dashboard terlebih dahulu sebelum mulai menjalankan perdagangan riil kembali:*

```bash
sudo systemctl start poly-bot
```

---

## Langkah 10: Uji Coba Reboot

Pastikan bot yang berjalan 24/7 tetap aktif secara otomatis setelah server di-reboot:

```bash
sudo reboot
```

Hubungkan kembali setelah 1-2 menit:

```bash
ssh -i C:\path\poly-v3.pem ubuntu@EC2_PUBLIC_IP
cd /opt/poly-v3
bash deploy/aws/healthcheck.sh
```

---

## Opsional: EC2 User Data

Skrip `cloud-init-user-data.sh` disediakan sebagai titik awal. Untuk repositori privat, kloning manual menggunakan SSH deploy key jauh lebih aman karena data EC2 user-data dapat terlihat oleh pengguna yang memiliki hak akses ke metadata/API EC2.

Jika Anda tetap ingin menggunakannya:
1. Edit variabel `REPO_URL` di dalam skrip.
2. Tempelkan isi skrip ke bagian **EC2 Advanced details** -> **User data**.
3. Luncurkan instance.
4. Hubungkan via SSH dan lakukan konfigurasi `.env`.

*Jangan pernah meletakkan API key atau private key Polymarket di dalam EC2 user-data.*

---

## Pemecahan Masalah (Troubleshooting)

### Layanan bot terus melakukan restart
```bash
sudo journalctl -u poly-bot -n 200 --no-pager
```
**Penyebab umum**:
- Berkas `.env` tidak ditemukan atau salah format.
- Mode riil aktif tanpa adanya konfigurasi `LIVE_TRADING_CONFIRM`.
- Proses instalasi paket dependensi gagal.
- Terjadi galat (error) jaringan atau batas waktu (timeout) API.

### Dashboard tidak dapat dibuka di peramban (browser)
Di terminal EC2:
```bash
curl -s http://127.0.0.1:5004/api/health
sudo systemctl status poly-dashboard
```
Di komputer/laptop lokal Anda:
- Pastikan koneksi SSH tunnel masih aktif dan terbuka.
- Buka alamat `http://localhost:5004`, bukan alamat IP publik EC2 Anda secara langsung.

### Tracker tidak dapat dibuka
```bash
curl -s http://127.0.0.1:5005/api/data >/dev/null && echo ok
sudo systemctl status poly-tracker
```

### Git pull gagal
Jika repositori Anda privat, gunakan SSH deploy key:
```bash
ssh -T git@github.com
git remote -v
```

### Ruang penyimpanan disk penuh (disk full)
```bash
df -h
du -sh /opt/poly-v3/*
journalctl --disk-usage
```
Bersihkan log journal yang sudah usang jika diperlukan:
```bash
sudo journalctl --vacuum-time=7d
```

### Penghentian Darurat (Emergency Stop)
Penghentian cepat bot saja:
```bash
sudo systemctl stop poly-bot
```
Biarkan layanan dashboard dan tracker tetap aktif untuk pemantauan:
```bash
sudo systemctl restart poly-dashboard poly-tracker
```

---

## Daftar Periksa Operasional Sebelum Mode Riil (Live)

- [ ] **AWS Budget** sudah dibuat dan aktif.
- [ ] **Security group** hanya mengizinkan akses masuk SSH (port 22) dari alamat IP Anda saat ini.
- [ ] Dashboard dan tracker dipastikan **tidak terekspos ke publik**.
- [ ] Berkas `.env` telah berada di EC2 dengan izin akses aman (`chmod 600`).
- [ ] Bot telah berhasil diuji coba dalam mode simulasi (`MOCK_MODE=true`) pada EC2 selama **minimal 24 jam**.
- [ ] Skrip pemeriksaan kesehatan (`bash deploy/aws/healthcheck.sh`) berhasil tanpa galat.
- [ ] Tampilan dashboard menunjukkan konfigurasi saat ini yang sudah sesuai.
- [ ] Variabel `END_WINDOW_LIVE_TRADE_USD` diatur dalam nilai yang kecil.
- [ ] Anda sepenuhnya menyadari dan memahami bahwa live mode dapat berakibat pada kehilangan uang sungguhan.

**Setelah semua poin di atas terpenuhi**, barulah Anda dapat mengonfigurasi `.env` ke mode riil:

```env
MOCK_MODE=false
LIVE_TRADING_CONFIRM=I_UNDERSTAND_THIS_IS_REAL_MONEY
```

Kemudian jalankan kembali layanan dan pantau log secara langsung:

```bash
sudo systemctl restart poly-bot poly-dashboard poly-tracker
sudo journalctl -u poly-bot -f
```
