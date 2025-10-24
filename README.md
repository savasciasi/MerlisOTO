# Merlis Metin2 Bot Kontrol Paneli

Modern PyQt5 arayüzü ile Metin2 ekran görüntüsü üzerinden Roboflow YOLOv11 tespiti yapan bu proje, oyuncu (`player`) ve özel mesaj kutusu (`pm-box`) sınıflarını gerçek zamanlı izler. Uygulama, tespit edilen PM kutularını isteğe bağlı olarak Telegram'a iletebilir, ekran görüntülerini kaydedebilir ve test modu ile kayıtlı görüntü/video üzerinde çalışabilir.

## Özellikler

- PyQt5 tabanlı koyu temalı kontrol paneli
- MSS ile gerçek zamanlı ekran yakalama
- Roboflow YOLOv11 modeli ile `player` ve `pm-box` tespiti
- Canlı önizlemede tespit kutuları ve etiketleri
- FPS, son olay ve zaman damgalı log takibi
- Son 10 oyuncu tespitinin listelenmesi
- PM kutusu tespiti, Telegram otomatik gönderimi ve kırpma padding ayarları
- Model/API ve Telegram bilgilerinin GUI üzerinden değiştirilmesi
- Test modu ile görüntü veya video dosyası üzerinden simülasyon
- Overlay içeren ekran görüntülerini `captures/` klasörüne kaydetme
- Ayarların `config.json` dosyasında saklanması
- PyInstaller ile tek dosya haline getirme desteği

## Kurulum

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

> Not: `requirements.txt` dosyasını kendi ortamınıza uygun paket listesiyle oluşturabilirsiniz. Projede kullanılan temel paketler: `PyQt5`, `opencv-python`, `numpy`, `mss`, `roboflow`, `python-telegram-bot==13.15`, `pillow`, `supervision` (opsiyonel).

## Kullanım

```bash
python app.py
```

İlk açılışta Roboflow ve Telegram ayarlarını girerek kaydedebilirsiniz. Ayarlar uygulama kapandığında `config.json` dosyasına yazılır.

### Test Modu

- `Model / API` sekmesindeki **Tek Kare Test Et** butonu ile bir görüntü veya video dosyası seçerek tahmini simüle edebilirsiniz.
- Video seçilmesi durumunda tahminler ayrı bir iş parçacığında oynatılır.

### Ekran Görüntüsü

- `Oyuncu Tanıma` sekmesindeki **Ekran Görüntüsü Kaydet** butonu ile overlay içeren kareler `captures/` klasörüne kaydedilir.

## PyInstaller ile Paketleme

```bash
pyinstaller --noconsole --name "MerlisMetin2Bot" app.py --add-data "assets;assets"
```

Oluşturulan `dist/MerlisMetin2Bot.exe` dosyası uygulamayı Windows üzerinde tek başına çalıştırır.

