# BTC Polymarket Trading Bot (Poly V3)

Bot trading otomatis frekuensi tinggi untuk pasar prediksi Bitcoin Up/Down (interval 5 menit / 15 menit) di **Polymarket** menggunakan **CTF Exchange V2 CLOB API** dan EIP-712 signing.

## Fitur Utama

- **Integrasi CTF Exchange V2**: Menggunakan SDK Polymarket V2 yang mendukung penempatan order FAK (Fill-and-Kill) untuk pembelian instan dan GTC (Good 'Til Cancelled) untuk eksekusi jual.
- **Strategi End-Window**: Menempatkan taruhan directional (UP/DOWN) di akhir window waktu berdasarkan selisih harga BTC spot terhadap target Polymarket (priceToBeat).
- **Berbagai Layer Trigger**:
  - `T1` (Sniper) - Detik awal window (5-50 detik)
  - `T2` (Early) - Konfirmasi awal tren (40-100 detik)
  - `T3` (Momentum) - Mengikuti tren yang kuat (80-130 detik)
  - `T7` (Scalp) - Detik-detik akhir window (250-280 detik)
  - `BUY-1` - Strategi keluar cepat (quick-exit/take-profit terprogram)
- **Modus Simulasi/Mock Akurat**: Melakukan simulasi realistis biaya live trading seperti spread, slippage, dan biaya protokol V2 (protocol fee) tanpa menggunakan uang riil.
- **Dashboard Real-Time**: Visualisasi data performa bot, P&L harian, log trading, pergerakan harga BTC, dan kedalaman orderbook secara interaktif.
- **Integrasi Telegram**: Kontrol bot langsung dari Telegram menggunakan perintah seperti `/status`, `/trading`, `/stop`, `/open`, dan `/screenshot`.
- **Sistem Keamanan Berlapis (Safety Gate)**:
  - Batasan eksposur trading live maksimum per transaksi dan per window.
  - Deteksi clock skew (selisih waktu lokal vs server/blockchain).
  - Proteksi terhadap orderbook yang tipis (liquidity guard) dan usia orderbook yang usang (stale orderbook guard).

---

## Persyaratan Sistem

- Python 3.10+
- Akun Polymarket dengan saldo **pUSD** (Polymarket USD) jika ingin menjalankan mode Live.

---

## Cara Instalasi

1. **Clone repositori** ini.
2. **Instal seluruh dependensi**:
   ```bash
   pip install -r requirements.txt
   ```
3. **Konfigurasi Environment**:
   Salin file `.env.example` menjadi `.env` lalu lengkapi isinya:
   - `POLYMARKET_PRIVATE_KEY` (Kunci privat wallet Polygon Anda)
   - `POLYMARKET_FUNDER_ADDRESS` (Alamat dompet proxy Polymarket Anda)
   - `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID` (Opsional, untuk bot Telegram)

---

## Cara Menjalankan Bot

### 1. Menjalankan dalam Mode Pengembangan (Dev Mode)
Gunakan file batch berikut untuk memulai bot dengan fitur auto-reload saat ada perubahan file:
```bash
run_dev.bat
```

### 2. Menjalankan dalam Mode Produksi / Normal
Jalankan perintah ini:
```bash
run.bat
```

Atau jalankan entrypoint Python secara langsung:
```bash
python main.py
```

Setelah bot berjalan, Anda dapat mengakses dashboard interaktif melalui peramban di alamat:
`http://localhost:5004` (Port default dapat disesuaikan di file `.env`).

---

## Lisensi

Projek ini dibuat untuk keperluan pembelajaran dan riset perdagangan otomatis di pasar prediksi crypto. Gunakan dengan risiko Anda sendiri (DYOR).
