# Merlis Metin2 PM Kontrol Merkezi

Windows üzerinde çalışan bu uygulama, Merlis oyun penceresini DXCam ile yakalayıp PM panelini tespit eder, Tesseract OCR ile mesajı çözümler ve modern PyQt5 arayüzü üzerinden iki ayrı oyun istemcisini eş zamanlı olarak yönetmenizi sağlar. Yeni PM'lerin ekran görüntüleri Telegram'a gönderilir; Telegram üzerinden verdiğiniz yanıtlar otomatik olarak oyuna yazılır ve pencere güvenle kapatılır.

## Öne Çıkan Özellikler

- **Çift istemci desteği:** Her iki Merlis penceresini bağımsız PID, ROI ve OCR ayarlarıyla izleyip otomasyon akışını ayrı ayrı başlatıp durdurabilirsiniz.
- **60 FPS canlı önizleme:** DXCam + `get_latest_frame` ile hedef pencereyi yüksek FPS'te yakalar, önizlemeyi 0.5/0.75/1.0 ölçeğine göre hafifletir.
- **Template matching tabanlı PM algısı:** `assets/pm_icon.png` ikonunu bulur, ikon kutusundan PM metin alanının ROI'sini çıkartır ve çakışmaları NMS ile sadeleştirir.
- **Tesseract OCR:** Hafif Gaussian blur ön işleme sonrası Türkçe + İngilizce karma OCR yapılır, checksum ile değişim algılanır ve dedupe zaman penceresi uygulanır.
- **Telegram köprüsü:** Yeni PM bulunduğunda Merlis penceresi öne getirilir, PM ikonu tıklanır, `pm_send_btn.png` ile gönder butonu bulunur, ekran görüntüsü & ROI Telegram'a gönderilir ve sizden yanıt beklenir. Telegram'da `#1 Merhaba` gibi ön ekli mesajla (veya yalnızca bir istemci bekliyorsa direkt mesajla) cevap verdiğinizde oyun penceresine otomatik yapıştırılır, gönderilir, pencere `pm_close_x.png` ile kapatılır ve Space tuşu tetiklenir. Gönderimin başarılı olduğu Telegram mesajıyla onaylanır.
- **Sarı oyuncu alarmı:** Eşleştirme döngüsü her karede sarı tonlu oyuncu siluetlerini de tarar, yakındaki oyuncuya ait ekran görüntüsünü Telegram'a yollar ve altında beyaz sohbet metni varsa OCR ile çözüp mesaj olarak iletir. Telegram'dan gelen yanıtlar oyun içi genel sohbete otomatik yapıştırılır.
- **Modern koyu arayüz:** Sol tarafta camgöbeği vurgulu gezinme paneli, üstte hero başlık şeridi ve kart tabanlı dashboard ile canlı PM önizlemeleri, son 10 mesaj listesi ve zaman damgalı log paneli.
- **Kalıcı ayarlar:** Telegram bilgileri, otomasyon şablonları, ROI ve OCR parametreleri `config.json` içinde yeni çift-istemci şemasında saklanır.
- **Ekran görüntüsü arşivi:** Tek tıkla overlay'li kare `captures/` klasörüne tarih damgalı dosya olarak kaydedilir.

## Kurulum

```bash
python -m venv .venv
. .venv/Scripts/activate  # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> **Notlar**
> - `dxcam`, `pywin32` ve `psutil` yalnızca Windows üzerinde çalışır.
> - `assets/` klasöründe `pm_icon.png`, `pm_close_x.png`, `pm_send_btn.png` dosyalarının mevcut olduğundan emin olun.
> - OCR için [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) kurulu olmalıdır. Varsayılan yol `C:\\Program Files\\Tesseract-OCR\\tesseract.exe` olarak gelir, farklıysa GUI'den güncelleyebilirsiniz.
> - PyTesseract veya Tesseract yolu doğrulanamazsa uygulama OCR'i devre dışı bırakır ve log panelinde hata mesajı gösterir.

## Kullanım

```bash
python app.py
```

1. **Genel Bakış** sekmesinde iki istemci kartını görürsünüz. "Başlat" ile yakalamayı başlatabilir, "Durdur" ile sonlandırabilir, "Ekran Görüntüsü Kaydet" ile anlık kareyi arşivleyebilirsiniz.
2. Her istemci için **Client 1 / Client 2** sayfalarında PID, süreç ipucu, ROI override, ikon/buton eşikleri, OCR periyodu ve otomasyon offset'lerini ayarlayın. "Merlis'i Bul (PID)" düğmesi süreç adından PID bulur. Aynı sayfadan sarı oyuncu taramasını etkinleştirip minimum alan, padding ve dedupe değerlerini güncelleyebilirsiniz.
3. **Telegram & Otomasyon** sayfasında bot token ve chat ID'yi girin, otomatik gönderimi etkinleştirin ve mesaj/başlık şablonlarını, yanıt ön ekini (`#`) ve zaman aşımı değerlerini düzenleyin. "Telegram Test Mesajı Gönder" ile bağlantıyı doğrulayabilirsiniz.
4. Yeni PM geldiğinde uygulama ekran görüntüsünü Telegram'a atar ve kart üzerinde "Yanıt bekleniyor" rozetini gösterir. Telegram'da `#1 cevap` (ya da yalnızca bir istemci bekliyorsa direkt mesaj) yazarak oyuna yanıt gönderebilirsiniz.
5. Gönderim tamamlandığında Telegram'da onay mesajı gelir, PM penceresi kapanır ve Space tuşu tanımladığınız süre boyunca basılı tutulur. Başarısız adımlar log panelinde WARN/ERROR olarak görünür.
6. Sarı oyuncu algılandığında ekran görüntüsü ve varsa beyaz sohbet metni Telegram'a iletilir; `#1 yanıt` benzeri mesajlarla genel sohbete otomatik cevap gönderebilirsiniz.

## PM Otomasyon Akışı

1. İkon tespit edilir, Merlis penceresi öne getirilir ve ikona tıklanır.
2. `pm_send_btn.png` şablonu bulunur; buton merkezinden konfigüre ettiğiniz offset kadar sola/yukarı gidilerek metin alanı odaklanır.
3. Tam pencere ekran görüntüsü ve ROI Telegram'a gönderilir, istemci "Yanıt bekleniyor" durumuna geçer.
4. Telegram üzerinden gelen yanıt kuyruklanır, pencere odaklanır, metin panoya kopyalanıp yapıştırılır, gönder butonuna tıklanır.
5. `pm_close_x.png` ile pencere kapatılır ve ayarladığınız gecikme + basma süresi ile Space tuşu tetiklenir.
6. İşlem başarıyla tamamlanırsa Telegram'a "✅ PM gönderildi" şablonlu onay iletilir; zaman aşımında ise ⚠️ uyarısı gönderilir ve otomasyon güvenle bırakılır.

## Paketleme

PyInstaller ile GUI'yi tek bir .exe dosyasına dönüştürebilirsiniz:

```bash
pyinstaller --noconsole --name "MerlisPMBot" app.py --add-data "assets;assets"
```

`dist/MerlisPMBot.exe` dosyasını bağımsız olarak çalıştırabilirsiniz.
