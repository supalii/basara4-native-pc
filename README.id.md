# Sengoku Basara 4 Sumeragi untuk PC (Basara 4 PC)

[English](README.md) | **Bahasa Indonesia**

Port PC native open-source dan launcher untuk **Sengoku Basara 4 Sumeragi** (PlayStation 4, CUSA01159) untuk Windows 10/11. Kode x86-64 asli game berjalan langsung di CPU PC Anda tanpa emulasi. Library sistem PlayStation 4 diimplementasikan ulang fungsi demi fungsi ke dalam C/C++ native, dan perintah GPU diterjemahkan ke Vulkan 1.3 secara real-time.

> **Tidak menyertakan file game berhak cipta.** Anda wajib menyediakan salinan dump legal dari konsol PlayStation 4 milik sendiri (teruji: CUSA01159).
>
> Proyek ini adalah proyek rekayasa balik (reverse-engineering) dan preservasi open-source independen.
> Tidak berafiliasi dengan, didukung oleh, atau terkait dengan Capcom maupun Sony Interactive Entertainment.
> "Sengoku Basara" adalah merek dagang milik Capcom Co., Ltd. "PlayStation" adalah merek dagang milik Sony Interactive Entertainment.

## Fork
Repositori ini adalah fork dari Basara 4 PC karya Razz ([razzconnect/basara4-native-pc](https://github.com/razzconnect/basara4-native-pc)), dibangun di atas SpartanPort, bloodborne_pc, dan shadPS4; kredit lengkap di [NOTICE.md](NOTICE.md).

## Spesifikasi Sistem
- **Sistem Operasi**: Windows 10 64-bit (1903 ke atas) / Windows 11 64-bit
- **Prosesor (CPU)**: CPU 64-bit dengan dukungan AVX (Intel Sandy Bridge 2011+, AMD FX)
- **Kartu Grafis (GPU)**: GPU yang mendukung Vulkan 1.3 (GTX 1060 / Radeon RX 580)
- **RAM**: 8 GB RAM
- **Penyimpanan**: Ruang kosong ~20 GB
- **File Game**: Salinan dump legal dari Sengoku Basara 4 Sumeragi (`CUSA01159`)

Linux tidak didukung secara native; menjalankan build Windows melalui Proton/Wine belum diuji.

## Menyiapkan File Game
Dump game dari konsol PlayStation 4 milik Anda sendiri menggunakan panduan dumping konsol yang terpercaya. Launcher memerlukan folder dump yang telah diekstrak dan didekripsi, bukan file `.pkg`.

Simpan base game, update, dan add-on dalam tiga folder terpisah—jangan pernah menggabungkannya secara manual:
```
CUSA01159/            base game: eboot.bin, sce_module/, sce_sys/param.sfo, nativePS4/...
CUSA01159-patch/      update opsional, misal v01.02
<DLC root>/CUSA01159/<entitlement>/   add-on opsional, satu folder per add-on
```
- **Syarat update**: Folder update wajib memiliki `eboot.bin` dan `sce_sys/param.sfo` sendiri dengan `TITLE_ID` yang sesuai (`CUSA01159`).
- **Syarat DLC**: Setiap folder add-on di bawah `<DLC root>/CUSA01159/<entitlement>/` wajib memiliki `sce_sys/param.sfo` dengan `CATEGORY` bernilai `ac` (add-on content), `TITLE_ID` yang sesuai, serta akhiran `CONTENT_ID` yang cocok dengan nama folder.

Jangan salin file update ke dalam folder base game. Arahkan pengaturan di halaman **Game** launcher ke masing-masing folder (**Game folder**, **Game update folder**, dan **DLC folder**). Dump asli Anda tidak akan diubah; launcher membuat tampilan gabungan read-only secara otomatis.

Untuk memverifikasi dump, pastikan `eboot.bin` dan `sce_sys/param.sfo` ada. Saat folder dipilih, indikator status pada halaman Game akan mengonfirmasi: status image game menampilkan **"Game image ready."** (atau **"Will be prepared on first run."**), update menampilkan **"Update v01.02 found"**, dan DLC menampilkan **"<N> add-ons found"**.

## Cara Pasang & Memainkan
1. Unduh `basara4-windows.zip` dari https://github.com/DarkTama/basara4-native-pc/releases lalu ekstrak.
2. Salin folder dump game PS4 Anda ke dalam `game\`, atau klik tombol **Browse** di launcher untuk memilih lokasinya. Atur **Game update folder** dan **DLC folder** pada halaman Game jika memiliki update atau add-on.
3. Jalankan **`Sengoku Basara 4.exe`**, pilih pengaturan Anda, dan klik **PLAY**. Peluncuran pertama akan menyiapkan image binary ke `out\` (memakan waktu sekitar 30–60 detik); peluncuran berikutnya langsung instan.
4. Untuk bermain dengan konfigurasi tersimpan tanpa menampilkan launcher, jalankan **`Play Sengoku Basara 4.exe`**. Proses launcher keluar setelah game mulai. Paket juga menyertakan `Play Sengoku Basara 4.cmd` sebagai shortcut.

Menutup launcher saat game berjalan tidak menghentikan game setelah dikonfirmasi; launcher tidak lagi memantau sesi tersebut.

## Patch Translasi Bahasa Inggris
Repositori ini **tidak menyertakan teks terjemahan atau aset game**. Pengguna wajib menyediakan patch translasi bahasa Inggris versi PS3 **BLJM61248** (v0.85 + Matabe fix).

1. Dapatkan file patch translasi PS3 (BLJM61248) (`patch 0.85/rom` dan `matabe fix/pl035`).
2. Buka launcher, klik **"Import PS3 English Patch…"**, lalu pilih folder atau file `.zip` patch PS3 Anda.
3. Repatcher akan memverifikasi file patch dengan manifes hash (`data/ps3_patch_manifest.json`), mengekstrak tabel teks GMD, mengonversi tekstur UI ke format native PS4, dan mengemas ulang arsip ke `mods/english_patch/`.
4. Centang opsi **"English Translation Patch (PS3 v0.85 Port)"** kapan saja untuk menerapkan patch via overlay sparse (`user/app0_overlay`) tanpa mengubah file asli game.

Jika folder update game diatur, patch akan dibangun dari file versi update secara otomatis. Percakapan saat bertarung dan dialog cerita tidak pernah diterjemahkan dalam patch PS3 asli sehingga tetap berbahasa Jepang. Beberapa judul menu dan label status juga masih berbahasa Jepang untuk saat ini; lihat docs/ENGLISH-PATCH-STATUS.md.

```bash
python launcher/basara4_repatch.py "mods/ps3 english patch" --game game/CUSA01159 --out mods/english_patch
```

## Cheat
Lima cheat gameplay (**Gold won't decrease**, **Special Coin x99999**, **Growth points won't decrease**, **Weapon points won't decrease**, **Weapon rank points won't decrease**) dapat diaktifkan dari tab **Mods & Cheats** di launcher atau di dalam game lewat menu overlay (**Insert** / **R3 + L2**). Cheat dimatikan secara default. Dibuat untuk game versi 01.02 dan dicari berdasarkan signature pada versi lainnya.

## Kontrol
- **Insert** (Keyboard) atau **R3 + L2** (Gamepad): Membuka menu overlay dalam game (pengaturan FSR, anisotropic filtering, status controller, cheat).
- **F12**: Mengambil screenshot ke folder `user\`.
- **Kontrol Keyboard & Mouse (KBM)**:
  - **W, A, S, D**: Pergerakan karakter (emulasi Stik Kiri, P1).
  - **Free Mouse Camera Look (Mouse Look)**: Pergerakan mouse memutar kamera 3D secara bebas via emulasi Stik Kanan (Right Stick), dengan penguncian kursor otomatis selama gameplay dan pelepasan kursor saat Alt-Tab atau game dijeda (pause).
  - **Pengaturan Launcher / Keymap**: Toggle aktif/nonaktifkan Mouse Look, atur sensitivitas kamera mouse (20% – 300%, default 100%), serta opsi Invert X / Invert Y.
  - **Pemetaan Tombol Default**:
    - **Klik Kiri**: Serangan Biasa / Normal Attack (Kotak / Square)
    - **Klik Kanan**: Special Skill 1 (Segitiga / Triangle)
    - **Spasi**: Lompat / Jump (Silang / Cross)
    - **Left Shift**: Tangkis / Guard / Dash / Evade (L1)
    - **E**: Special Skill 2 (R1)
    - **Q**: Special Skill 3 (R2)
    - **F**: Basara Attack / Ultimate (Lingkaran / Circle)
    - **R**: Sengoku Drive / Poise Boost (L2)
    - **Tab**: Ejekan / Taunt / Provoke (Touchpad)
    - **Enter**: Jeda / Menu Dalam Game (Options)
    - **Tombol Panah (Atas, Bawah, Kiri, Kanan)**: Navigasi D-Pad
    - **Z**: L3
    - **C**: R3 / Lock-on
- **Gamepad & Controller**: Controller native XInput & SDL3 (Xbox, DualShock 4, DualSense, Switch Pro) serta gamepad DirectInput generik dipetakan otomatis melalui SDL GameControllerDB bawaan. Untuk gamepad yang belum dikenali, klik **Map controller** di launcher untuk kalibrasi tombol dan stik analog menggunakan `basara4-pad-mapper.exe`.

## Save Data & Starter Save 100%
Lokasi penyimpanan save data berada di:
```
user\savedata\1\CUSA01159\CUSA011590000\BSR4_SUMERAGI.DAT
```
- **Starter Save Patcher (Level 1)**: Fitur patcher terencana yang dirancang untuk membuka seluruh senjata offline maupun DLC, item Net Basara Shop, inskripsi (inscriptions), serta koin maksimal, dengan tetap mempertahankan level karakter pada level 0/1 agar pemain tetap dapat menikmati tantangan dan progresi alami permainan.

## Membangun dari Source Code
Prasyarat: Pasang MSYS2 (`winget install MSYS2.MSYS2`), buka shell **MSYS2 CLANG64** (bukan PowerShell: perintah `bash` di PowerShell akan membuka WSL), jalankan `pacman -Syu` terlebih dahulu, serta pastikan Windows Python 3.10+ terpasang.

```bash
pacman -S --needed git patch \
  mingw-w64-clang-x86_64-{toolchain,cmake,ninja,pkgconf,python} \
  mingw-w64-clang-x86_64-{vulkan-headers,vulkan-loader,vulkan-memory-allocator,glslang} \
  mingw-w64-clang-x86_64-{sdl3,fmt,boost,robin-map,xxhash,ffmpeg,zydis} \
  mingw-w64-clang-x86_64-{spirv-tools,spirv-headers,spirv-cross}
```

```powershell
python -m pip install pyinstaller pillow numpy etcpak iced-x86==1.21.0
```

Langkah kompilasi:
```bash
git clone --recursive https://github.com/DarkTama/basara4-native-pc.git
cd basara4-native-pc
git submodule update --init --recursive
bash build.sh
```

Kemas game (package harus dibuild dengan launcher dalam keadaan tertutup):
```powershell
python packaging/basara4/make_package.py --out dist/basara4-windows
```

Tool arsip MT Framework dan teks GMD:
```bash
python tools/arc_tool.py list|extract|repack|gmd ...
```

## Log
- `user\last_run.log`: Log diagnostik runtime utama (inisialisasi, kompilasi shader GPU, crash traceback).
- `user\prepare.log`: Output dari tahap persiapan dan relokasi image.
- `user\audio_debug.log`: Detail alokasi suara Ngs2, streaming feed, dan status buffer.

## Kredit
- **Razz** - Basara 4 PC port
- **Felipe Prado** - SpartanPort
- **Supermedo & deadinside28** - bloodborne_pc
- **shadPS4 team** - GPU core
- **Mystechry / MikoMango & indrasundanese** - PS3 English patch
- **i7Exceed** - Port hook cheat Basara 4, overlay update game, dan mounting DLC lokal
- **Q8Despair / GoldHEN Cheat Repository** - Data patch cheat
- **supalii** - Perbaikan build

Pemberitahuan dan lisensi lengkap: [NOTICE.md](NOTICE.md).

## Lisensi
Dirilis di bawah lisensi GNU General Public License v2.0 atau lebih baru (GPL-2.0-or-later).
Lihat [LICENSE](LICENSE) dan [NOTICE.md](NOTICE.md).
