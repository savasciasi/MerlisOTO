# Merlis Metin2 PM OCR Botu

Windows üzerinde çalışan bu proje, Merlis oyun penceresini DXCam ile 60 FPS'e kadar yakalayarak Tesseract OCR ile özel mesaj (PM)
panelindeki yeni mesajları tespit eder. PyQt5 ile hazırlanmış koyu temalı kontrol paneli üzerinden tüm ayarları yönetebilir, yeni
PM'leri Telegram'a otomatik iletebilir ve overlay'li ekran görüntüleri kaydedebilirsiniz.

## Özellikler

- DXCam tabanlı pencere yakalama, Merlis sürecine PID ile kilitlenme
- PM panelini ikon temelli template matching ile bulma
- Tesseract + PyTesseract ile hızlı OCR ve checksum bazlı değişim takibi
- 60 FPS canlı önizleme (ölçeklenebilir), 8–12 FPS OCR döngüsü
- Yeni PM bulunduğunda Telegram'a metin ve ekran kırpıntısı gönderimi
- Tüm eşi̇kler, ROI ve OCR parametreleri GUI üzerinden ayarlanabilir
- Ayarlar `config.json` dosyasında kalıcı olarak saklanır
- Overlay'li ekran görüntüsünü `captures/` klasörüne kaydetme
- Zaman damgalı log paneli, son tespit edilen mesaj listesi

## Kurulum

```bash
python -m venv .venv
. .venv/Scripts/activate  # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> Notlar:
> - `dxcam`, `pywin32` ve `psutil` yalnızca Windows üzerinde çalışır.
> - `assets/` klasöründe `pm_icon.png`, `pm_close_x.png`, `pm_send_btn.png` dosyalarının bulunduğundan emin olun.
> - OCR için Windows'a [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) yükleyin. Varsayılan yol `C:\\Program Files\\Tesseract-OCR\\tesseract.exe` olarak ayarlanır, farklıysa GUI üzerinden güncelleyebilirsiniz.
> - PyTesseract yüklenemez veya Tesseract yolu bulunamazsa uygulama OCR'i devre dışı bırakıp log panelinde ayrıntılı bir hata mesajı gösterir.

## Kullanım

```bash
python app.py
```

1. **Pencere / PID** sekmesinden Merlis sürecini otomatik bulabilir ya da PID değerini girebilirsiniz.
2. Gerekirse ROI override alanlarını kullanarak manuel bir yakalama bölgesi tanımlayın.
3. **Telegram** sekmesinden bot token ve chat ID bilgilerinizi girerek otomatik gönderimi etkinleştirin.
4. Dashboard üzerinden "Başlat" düğmesine basarak canlı yakalamayı başlatın. Son PM'ler listede görüntülenir.
5. "Ekran Görüntüsü Kaydet" düğmesi o anki overlay'li kareyi `captures/` klasörüne kaydeder.

## Paketleme

PyInstaller ile GUI'yi tek yürütülebilir dosya haline getirebilirsiniz:

```bash
pyinstaller --noconsole --name "MerlisPMBot" app.py --add-data "assets;assets"
```

Ortaya çıkan `dist/MerlisPMBot.exe` dosyası tek başına çalıştırılabilir.

