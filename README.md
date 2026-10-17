# DSO-SERVER

Bu depo, Drakensang Online sunucu yazılımının geliştirilmesi için kullanılan bir fork'tur. Kaynak proje: [DSO-Reconstruction/DSOR_Server](https://github.com/DSO-Reconstruction/DSOR_Server). Bu fork'un GitHub adresi [xm1rth/DSO-SERVER](https://github.com/xm1rth/DSO-SERVER) olarak planlanmıştır.

## Gerekli istemci dosyaları

İstemciye ait oyun verileri bu depoya eklenmez. Sunucuyu başlatmadan önce uyumlu istemci sürümünüzden aşağıdaki dosya ve klasörleri kendiniz kopyalayın:

| Kaynak | Bu depodaki hedef |
| --- | --- |
| `export_win32/db/static.db4` | `data/db/static.db4` |
| `export_win32/maps/` içeriği | `data/maps/` |
| `export_win32/navigation/` içeriği | `data/navigation/` |
| `export_win32/tables/navshapes.bin` | `data/navigation/navshapes.bin` |
| İstemcinin `language/` klasörü | `data/language/` |
| İstemcinin `quests.xml` dosyası | `data/quests.xml` |
| İstemcinin `DSOClient/` klasörünün tamamı | `DSOClient/` |

`maps` ve `navigation` klasörlerinin içeriğini hedef klasöre doğrudan kopyalayın; iç içe ikinci bir `maps/` veya `navigation/` klasörü oluşturmayın. Navigation alt klasörlerinin yapısını koruyun. `navshapes.bin`, istemcinin `tables/` klasöründen gelir.

Sunucu ve istemci aynı uyumlu içerik sürümünü kullanmalıdır. Özellikle `static.db4`, map ve navigation dosyalarının sürümleri eşleşmelidir. Yedekleri (`.bak`, `*backup*`) ve SQLite çalışma dosyalarını (`-wal`, `-shm`) kopyalamayın. Eksik istemci dosyaları nedeniyle sunucu başlamaz; bu dosyalar GitHub deposunda bulunmaz ve `.gitignore` ile hariç tutulur.

## Kurulum ve çalıştırma

64-bit Python 3.12 veya daha yenisini kurun. Depoyu indirdikten sonra:

1. Yukarıdaki istemci dosyalarını yerleştirin.
2. `.env.example` dosyasını `.env` adıyla kopyalayın.
3. Yerel kullanım için `DSO_PUBLIC_HOST=127.0.0.1` değerini bırakın. Başka bilgisayarlardan bağlantı kabul edecekseniz `.env` içinde `DSO_HOST=0.0.0.0` ve istemcilerin erişebileceği `DSO_PUBLIC_HOST` adresini ayarlayın.
4. Windows'ta `start.bat` dosyasını çalıştırın. Linux'ta `bash start.sh` komutunu kullanın.

Sunucu, hesap kayıt/giriş API'sini HTTP `8080` portunda ve yönetim panelini varsayılan olarak yalnızca yerel erişime açık HTTP `8081` portunda sunar. Oyun servisleri UDP `80`, `2190`, `2191` ve `2192` portlarını kullanır. `DSO_PUBLIC_HOST` adresi istemcilerin bu sunucuya ulaşabildiği adres olmalıdır.

`data/events.json` başlangıçta boş bir etkinlik takvimi içerir. Etkinlikleri sunucunun yönetim panelinden tanımlayabilirsiniz.

## Fork ve kaynak

Bu proje, [DSO Reconstruction](https://github.com/DSO-Reconstruction/DSOR_Server) çalışmasının fork'udur. Kaynak projenin amacı Drakensang Online ile ilgili bileşenleri tersine mühendislikle incelemektir. Bu depoda yapılan değişiklikler ve eklemeler bu fork kapsamında geliştirilir.

Planlanan uzak depo: [`https://github.com/xm1rth/DSO-SERVER.git`](https://github.com/xm1rth/DSO-SERVER.git).
