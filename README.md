# Merlis Metin2 Bot Kontrol Paneli

Modern PyQt5 arayüzü ile Metin2 ekran görüntüsü üzerinden Tesseract OCR tespiti yapan bu proje, oyuncu (`player`) ve özel mesaj kutusu (`pm-box`) ifadelerini gerçek zamanlı izlemenizi sağlar. Uygulama, tespit edilen PM kutularını isteğe bağlı olarak Telegram'a iletebilir, ekran görüntülerini kaydedebilir ve test modu ile kayıtlı görüntü/video üzerinde çalışabilir.

## Özellikler

- PyQt5 tabanlı koyu temalı kontrol paneli
- MSS ile gerçek zamanlı ekran yakalama
- Tesseract OCR ile anahtar kelime tabanlı `player` ve `pm-box` eşleşmeleri
- Canlı önizlemede tespit kutuları ve etiketleri
- FPS, son olay ve zaman damgalı log takibi
- Son 10 oyuncu eşleşmesinin listelenmesi
- PM kutusu eşleşmesi, Telegram otomatik gönderimi ve kırpma padding ayarları
- OCR ve Telegram bilgilerinin GUI üzerinden değiştirilmesi
- Test modu ile görüntü veya video dosyası üzerinden simülasyon
- Overlay içeren ekran görüntülerini `captures/` klasörüne kaydetme
- Ayarların `config.json` dosyasında saklanması
- PyInstaller ile tek dosya haline getirme desteği

## Kurulum

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> Not: Tesseract OCR motorunun sisteminizde kurulu olması gerekir. Windows için [Tesseract kurulumu](https://github.com/UB-Mannheim/tesseract/wiki) sayfasından indirip PATH değişkenine ekleyebilirsiniz.

## Kullanım

```bash
python app.py
```

İlk açılışta OCR ve Telegram ayarlarını girerek kaydedebilirsiniz. Ayarlar uygulama kapandığında `config.json` dosyasına yazılır.

### Test Modu

- `OCR Ayarları` sekmesindeki **Tek Kare OCR Testi** butonu ile bir görüntü veya video dosyası seçerek tespiti simüle edebilirsiniz.
- Video seçilmesi durumunda OCR işlemleri ayrı bir iş parçacığında oynatılır.

### Ekran Görüntüsü

- `Oyuncu Tanıma` sekmesindeki **Ekran Görüntüsü Kaydet** butonu ile overlay içeren kareler `captures/` klasörüne kaydedilir.

## PyInstaller ile Paketleme

```bash
pyinstaller --noconsole --name "MerlisMetin2Bot" app.py --add-data "assets;assets"
```

Oluşturulan `dist/MerlisMetin2Bot.exe` dosyası uygulamayı Windows üzerinde tek başına çalıştırır.
