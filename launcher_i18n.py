"""Static launcher UI translations for the supported client languages."""

STRINGS = {'en': {'subtitle': 'DSOR Community Launcher',
        'server_ip': 'Server IP Address',
        'game_language': 'Game Language',
        'err_ip': 'Enter a valid server IP address.',
        'err_language': 'The selected game language is not supported.',
        'username': 'Username',
        'password': 'Password',
        'password2': 'Password (repeat)',
        'remember': 'Remember account',
        'old_look': 'Old item look (old frames and backgrounds)',
        'inventory_vfx': 'Inventory VFX fix (this session)',
        'err_inventory_vfx': 'The inventory VFX fix does not support this client build.',
        'play_saved': 'PLAY',
        'login': 'SIGN IN',
        'register': 'CREATE ACCOUNT',
        'connecting': 'CONNECTING...',
        'to_register': 'No account yet? Create one',
        'to_login': 'Already have an account? Sign in',
        'version': 'DSOR • Community Server',
        'err_fields': 'Username and password are required.',
        'err_pw_match': 'Passwords do not match.',
        'status_creating': 'Creating account...',
        'status_created': 'Account created! Signing in...',
        'status_connecting': 'Connecting to server...',
        'status_launching': 'Launching game...',
        'err_conn': 'Could not connect to the server. Check the server address and your '
                    'connection, then try again.',
        'err_no_client': 'dro_client64.exe not found!\n'
                         'Check the Client\\dlcache or DSOClient\\dlcache folder.',
        'err_launch': 'Sign-in failed (HTTP {code})',
        'err_register': 'Registration failed (HTTP {code})',
        'language': 'Language',
        'feed_title': 'DSOR_SERVER • LAST COMMIT ON MAIN',
        'feed_hint': 'Double-click a commit to open it on GitHub.',
        'feed_loading': 'Loading commits from main…',
        'feed_empty': 'No commits to show.',
        'feed_error': 'Could not reach GitHub: {error}',
        'update_checking': 'Checking the GitHub server revision…',
        'update_check': 'CHECK FOR UPDATES',
        'update_checking_button': 'CHECKING…',
        'update_check_failed': 'Update check failed: {error}',
        'update_retry': 'RETRY',
        'update_disconnected': 'Server checkout not linked • GitHub main: {sha}',
        'open_repo': 'OPEN REPOSITORY',
        'up_to_date': 'Server files are up to date • {sha}',
        'up_to_date_button': 'UP TO DATE',
        'update_available': 'Server update available • {local} → {remote}',
        'update_button': 'UPDATE',
        'update_title': 'Server update',
        'update_confirm': 'The latest DSOR_Server main files will be downloaded from GitHub.\n'
                          '\n'
                          'Commit: {sha}\n'
                          '{message}\n'
                          '\n'
                          'Git is optional. A clean Git checkout is fast-forwarded; installations '
                          'without Git install the GitHub archive.',
        'update_running': 'Fetching server files from GitHub…',
        'update_running_button': 'UPDATING…',
        'update_done': 'Server updated • {sha} • Restart the server to apply the changes.',
        'update_done_title': 'Update complete',
        'update_done_message': 'The server files are updated. Restart the server, then close and '
                               'reopen the launcher.',
        'update_failed': 'Could not apply update: {error}',
        'update_retry_check': 'CHECK AGAIN',
        'settings': 'SETTINGS',
        'play': 'PLAY',
        'settings_title': 'LAUNCHER SETTINGS',
        'client_folder': 'Game Client Folder',
        'browse': 'BROWSE…',
        'save_settings': 'SAVE SETTINGS',
        'test_connection': 'TEST CONNECTION',
        'testing_connection': 'Testing server connection…',
        'connection_ok': 'Connection to the server succeeded.',
        'connection_failed': 'Could not connect to the server: {error}',
        'client_path_required': 'Select the game client folder first.',
        'client_path_invalid': 'The selected folder does not contain a valid DSO client.',
        'unpack_client': 'UNPACK CLIENT',
        'checking_client': 'Checking the client download…',
        'client_not_complete': 'Client files are incomplete or could not be verified: {error}',
        'unpack_confirm': 'The client bundles have been verified ({bundles} bundles). They will be '
                          'unpacked into the selected game folder, then current server assets will '
                          'replace their older versions in data/. This may use about 7 GB of '
                          'additional space. The client dlcache index is backed up before it is '
                          'replaced. Existing data/quests.xml will also be updated. Continue?\n'
                          '\n'
                          'Stop the server before continuing.\n'
                          '\n'
                          'Old maps, navigation, and language assets absent from this cache will '
                          'be removed.',
        'unpack_running': 'Unpacking client bundles and refreshing server data…',
        'unpack_progress': '{line}',
        'unpack_done': 'Client unpack and server data update completed. Restart the server before '
                       'playing.',
        'unpack_failed': 'Client unpack failed: {error}',
        'unpack_no_script': 'setup_client_assets.py was not found beside the launcher.',
        'settings_saved': 'Settings saved.'},
 'tr': {'subtitle': "DSOR Topluluk Launcher'ı",
        'server_ip': 'Sunucu IP Adresi',
        'game_language': 'Oyun Dili',
        'err_ip': 'Geçerli bir sunucu IP adresi gir.',
        'err_language': 'Desteklenmeyen oyun dili seçildi.',
        'username': 'Kullanıcı Adı',
        'password': 'Şifre',
        'password2': 'Şifre (tekrar)',
        'remember': 'Hesabı kaydet',
        'old_look': 'Eski item görünümü (eski çerçeve ve arka planlar)',
        'inventory_vfx': 'Envanter VFX düzeltmesi (bu oturum)',
        'err_inventory_vfx': 'Envanter VFX düzeltmesi bu istemci sürümünü desteklemiyor.',
        'play_saved': 'OYNA',
        'login': 'GİRİŞ YAP',
        'register': 'HESAP OLUŞTUR',
        'connecting': 'BAĞLANIYOR...',
        'to_register': 'Hesabın yok mu? Yeni hesap oluştur',
        'to_login': 'Zaten hesabın var mı? Giriş yap',
        'version': 'DSOR • Topluluk Sunucusu',
        'err_fields': 'Kullanıcı adı ve şifre gerekli.',
        'err_pw_match': 'Şifreler eşleşmiyor.',
        'status_creating': 'Hesap oluşturuluyor...',
        'status_created': 'Hesap oluşturuldu! Şimdi giriş yapılıyor...',
        'status_connecting': 'Sunucuya bağlanılıyor...',
        'status_launching': 'Oyun başlatılıyor...',
        'err_conn': 'Sunucuya bağlanılamadı. Sunucu adresini ve bağlantını kontrol edip tekrar '
                    'dene.',
        'err_no_client': 'dro_client64.exe bulunamadı!\n'
                         'Client\\dlcache veya DSOClient\\dlcache klasörünü kontrol et.',
        'err_launch': 'Giriş başarısız (HTTP {code})',
        'err_register': 'Kayıt başarısız (HTTP {code})',
        'language': 'Dil',
        'feed_title': 'DSOR_SERVER • MAIN DALINDA SON COMMIT',
        'feed_hint': "Bir commit'i GitHub'da açmak için çift tıkla.",
        'feed_loading': 'main dalındaki commitler yükleniyor…',
        'feed_empty': 'Gösterilecek commit bulunamadı.',
        'feed_error': "GitHub'a ulaşılamadı: {error}",
        'update_checking': 'GitHub sunucu sürümü kontrol ediliyor…',
        'update_check': 'GÜNCELLEMEYİ KONTROL ET',
        'update_checking_button': 'KONTROL EDİLİYOR…',
        'update_check_failed': 'Güncelleme kontrolü başarısız: {error}',
        'update_retry': 'TEKRAR DENE',
        'update_disconnected': 'Sunucu deposu bağlı değil • GitHub main: {sha}',
        'open_repo': 'DEPOYU AÇ',
        'up_to_date': 'Sunucu dosyaları güncel • {sha}',
        'up_to_date_button': 'GÜNCEL',
        'update_available': 'Sunucu güncellemesi var • {local} → {remote}',
        'update_button': 'GÜNCELLE',
        'update_title': 'Sunucu güncellemesi',
        'update_confirm': 'DSOR_Server main dalındaki güncel dosyalar GitHub’dan indirilecek.\n'
                          '\n'
                          'Commit: {sha}\n'
                          '{message}\n'
                          '\n'
                          'Git kullanmak zorunlu değildir. Temiz Git kopyaları fast-forward '
                          'güncellenir; Git olmayan kurulumlarda GitHub arşivi yüklenir.',
        'update_running': "GitHub'dan sunucu dosyaları alınıyor…",
        'update_running_button': 'GÜNCELLENİYOR…',
        'update_done': 'Sunucu güncellendi • {sha} • Etkinleşmesi için sunucuyu yeniden başlat.',
        'update_done_title': 'Güncelleme tamamlandı',
        'update_done_message': 'Sunucu dosyaları güncellendi. Çalışan sunucuyu yeniden başlat; '
                               "launcher'ı da kapatıp tekrar aç.",
        'update_failed': 'Güncelleme uygulanamadı: {error}',
        'update_retry_check': 'TEKRAR KONTROL ET',
        'settings': 'AYARLAR',
        'play': 'OYNA',
        'settings_title': 'LAUNCHER AYARLARI',
        'client_folder': 'Oyun İstemcisi Klasörü',
        'browse': 'GÖZ AT…',
        'save_settings': 'AYARLARI KAYDET',
        'test_connection': 'BAĞLANTIYI TEST ET',
        'testing_connection': 'Sunucu bağlantısı test ediliyor…',
        'connection_ok': 'Sunucuya bağlantı başarılı.',
        'connection_failed': 'Sunucuya bağlanılamadı: {error}',
        'client_path_required': 'Önce oyun istemcisi klasörünü seç.',
        'client_path_invalid': 'Seçilen klasörde geçerli bir DSO istemcisi bulunamadı.',
        'unpack_client': 'CLIENT DOSYALARINI AÇ',
        'checking_client': 'İstemci indirmesinin tamlığı kontrol ediliyor…',
        'client_not_complete': 'İstemci dosyaları eksik veya doğrulanamadı: {error}',
        'unpack_confirm': 'İstemci paketleri doğrulandı ({bundles} paket). Paketler seçilen oyun '
                          'klasörüne açılacak; ardından güncel sunucu verileri data/ klasörüne '
                          'aktarılıp eski asset dosyalarının üzerine yazılacak. Yaklaşık 7 GB ek '
                          'alan gerekebilir. Değiştirilmeden önce istemcinin dlcache dizin '
                          'dosyasının yedeği alınır. Mevcut data/quests.xml de güncellenir. Devam '
                          'edilsin mi?\n'
                          '\n'
                          'Devam etmeden önce sunucuyu durdur.\n'
                          '\n'
                          'Bu cache içinde bulunmayan eski harita, navigasyon ve dil dosyaları '
                          'silinecek.',
        'unpack_running': 'İstemci paketleri açılıyor ve sunucu verileri yenileniyor…',
        'unpack_progress': '{line}',
        'unpack_done': 'İstemci açma ve sunucu verisi güncellemesi tamamlandı. Oyundan önce '
                       'sunucuyu yeniden başlat.',
        'unpack_failed': 'İstemci açma işlemi başarısız: {error}',
        'unpack_no_script': 'setup_client_assets.py launcher klasöründe bulunamadı.',
        'settings_saved': 'Ayarlar kaydedildi.'},
 'ar': {'subtitle': 'مشغل مجتمع DSOR',
        'server_ip': 'عنوان IP للخادم',
        'game_language': 'لغة اللعبة',
        'err_ip': 'أدخل عنوان IP صالحًا للخادم.',
        'err_language': 'لغة اللعبة المحددة غير مدعومة.',
        'username': 'اسم المستخدم',
        'password': 'كلمة المرور',
        'password2': 'كلمة المرور (تكرار)',
        'remember': 'تذكر الحساب',
        'old_look': 'مظهر العنصر القديم (الإطارات والخلفيات القديمة)',
        'inventory_vfx': 'إصلاح VFX للمخزون (هذه الجلسة)',
        'err_inventory_vfx': 'لا يدعم إصلاح VFX للمخزون إصدار العميل هذا.',
        'play_saved': 'تشغيل',
        'login': 'تسجيل الدخول',
        'register': 'إنشاء حساب',
        'connecting': 'جارٍ الاتصال...',
        'to_register': 'ليس لديك حساب بعد؟ قم بإنشاء واحدة',
        'to_login': 'هل لديك حساب بالفعل؟ تسجيل الدخول',
        'version': 'DSOR • خادم المجتمع',
        'err_fields': 'اسم المستخدم وكلمة المرور مطلوبان.',
        'err_pw_match': 'كلمات المرور غير متطابقة.',
        'status_creating': 'جارٍ إنشاء الحساب...',
        'status_created': 'تم إنشاء الحساب! تسجيل الدخول...',
        'status_connecting': 'جارٍ الاتصال بالخادم...',
        'status_launching': 'إطلاق اللعبة...',
        'err_conn': 'تعذر الاتصال بالخادم. تحقق من عنوان الخادم واتصالك، ثم حاول مرة أخرى.',
        'err_no_client': 'لم يتم العثور على dro_client64.exe! \n'
                         ' تحقق من المجلد Client\\dlcache أو DSOClient\\dlcache.',
        'err_launch': 'فشل تسجيل الدخول (HTTP {code})',
        'err_register': 'فشل التسجيل (HTTP {code})',
        'language': 'اللغة',
        'feed_title': 'DSOR_SERVER • آخر commit على main',
        'feed_hint': 'انقر نقرًا مزدوجًا فوق الالتزام لفتحه على GitHub.',
        'feed_loading': 'جارٍ تحميل الالتزامات من main...',
        'feed_empty': 'لا يوجد التزام بالعرض.',
        'feed_error': 'تعذر الوصول إلى GitHub: {error}',
        'update_checking': 'جارٍ التحقق من مراجعة خادم GitHub...',
        'update_check': 'التحقق من وجود تحديثات',
        'update_checking_button': 'جارٍ التحقق...',
        'update_check_failed': 'فشل التحقق من التحديث: {error}',
        'update_retry': 'إعادة المحاولة',
        'update_disconnected': 'لم يتم ربط عملية الدفع بالخادم • GitHub main: {sha}',
        'open_repo': 'المستودع المفتوح',
        'up_to_date': 'ملفات الخادم محدثة • {sha}',
        'up_to_date_button': 'محدث',
        'update_available': 'يتوفر تحديث للخادم • {local} → {remote}',
        'update_button': 'تحديث',
        'update_title': 'تحديث الخادم',
        'update_confirm': 'سيتم تنزيل أحدث ملفات DSOR_Server main من GitHub.\n'
                          '\n'
                          'الالتزام: {sha}\n'
                          '{message}\n'
                          '\n'
                          'بوابة اختيارية. يتم إعادة توجيه عملية دفع Git النظيفة بسرعة؛ عمليات '
                          'التثبيت بدون Git، قم بتثبيت أرشيف GitHub.',
        'update_running': 'جلب ملفات الخادم من GitHub...',
        'update_running_button': 'جارٍ التحديث...',
        'update_done': 'تم تحديث الخادم • {sha} • أعد تشغيل الخادم لتطبيق التغييرات.',
        'update_done_title': 'اكتمل التحديث',
        'update_done_message': 'تم تحديث ملفات الخادم. أعد تشغيل الخادم، ثم أغلق المشغل وأعد فتحه.',
        'update_failed': 'تعذر تطبيق التحديث: {error}',
        'update_retry_check': 'تحقق مرة أخرى',
        'settings': 'الإعدادات',
        'play': 'العب',
        'settings_title': 'إعدادات المشغل',
        'client_folder': 'مجلد عميل اللعبة',
        'browse': 'تصفح...',
        'save_settings': 'حفظ الإعدادات',
        'test_connection': 'اختبار الاتصال',
        'testing_connection': 'جارٍ اختبار الاتصال بالخادم...',
        'connection_ok': 'نجح الاتصال بالخادم.',
        'connection_failed': 'تعذر الاتصال بالخادم: {error}',
        'client_path_required': 'حدد مجلد عميل اللعبة أولاً.',
        'client_path_invalid': 'المجلد المحدد لا يحتوي على عميل DSO صالح.',
        'unpack_client': 'فك العميل',
        'checking_client': 'جارٍ التحقق من تنزيل العميل...',
        'client_not_complete': 'ملفات العميل غير مكتملة أو لا يمكن التحقق منها: {error}',
        'unpack_confirm': 'تم التحقق من حزم العميل (حزم {bundles}). سيتم تفريغها في مجلد اللعبة '
                          'المحدد، ثم ستحل أصول الخادم الحالية محل الإصدارات القديمة في data/. قد '
                          'يستخدم هذا حوالي 7 جيجابايت من المساحة الإضافية. يتم عمل نسخة احتياطية '
                          'من فهرس العميل dlcache قبل استبداله. سيتم أيضًا تحديث data/quests.xml '
                          'الموجود. يكمل؟\\n\\nأوقف الخادم قبل المتابعة.\n'
                          '\n'
                          'ستتم إزالة ملفات الخرائط والتنقل واللغة القديمة غير الموجودة في هذه '
                          'الذاكرة المؤقتة.',
        'unpack_running': 'تفريغ حزم العميل وتحديث بيانات الخادم...',
        'unpack_progress': '{line}',
        'unpack_done': 'اكتمل فك ضغط العميل وتحديث بيانات الخادم. أعد تشغيل الخادم قبل اللعب.',
        'unpack_failed': 'فشل فك حزمة العميل: {error}',
        'unpack_no_script': 'لم يتم العثور على setup_client_assets.py بجانب المشغل.',
        'settings_saved': 'تم حفظ الإعدادات.'},
 'bg': {'subtitle': 'DSOR общност за стартиране',
        'server_ip': 'IP адрес на сървъра',
        'game_language': 'Език на играта',
        'err_ip': 'Въведете валиден IP адрес на сървъра.',
        'err_language': 'Избраният език на играта не се поддържа.',
        'username': 'Потребителско име',
        'password': 'Парола',
        'password2': 'Парола (повтаряне)',
        'remember': 'Запомни акаунта',
        'old_look': 'Изглед на стар артикул (стари рамки и фонове)',
        'inventory_vfx': 'Корекция на VFX на инвентара (тази сесия)',
        'err_inventory_vfx': 'VFX корекцията на инвентара не поддържа тази клиентска компилация.',
        'play_saved': 'ИГРАЙТЕ',
        'login': 'ВХОД',
        'register': 'СЪЗДАВАНЕ НА АКАУНТ',
        'connecting': 'СВЪРЗВАНЕ...',
        'to_register': 'Все още нямате акаунт? Създайте такъв',
        'to_login': 'Вече имате акаунт? влезте',
        'version': 'DSOR • Общностен сървър',
        'err_fields': 'Необходими са потребителско име и парола.',
        'err_pw_match': 'Паролите не съвпадат.',
        'status_creating': 'Създаване на акаунт...',
        'status_created': 'Акаунтът е създаден! Влизам...',
        'status_connecting': 'Свързване към сървър...',
        'status_launching': 'Играта се стартира...',
        'err_conn': 'Не може да се свърже със сървъра. Проверете адреса на сървъра и вашата '
                    'връзка, след което опитайте отново.',
        'err_no_client': 'dro_client64.exe не \u200b\u200bе намерен! \n'
                         ' Проверете папката Client\\dlcache или DSOClient\\dlcache.',
        'err_launch': 'Неуспешно влизане (HTTP {code})',
        'err_register': 'Неуспешна регистрация (HTTP {code})',
        'language': 'Език',
        'feed_title': 'DSOR_SERVER • ПОСЛЕДЕН COMMIT В MAIN',
        'feed_hint': 'Щракнете двукратно върху ангажимент, за да го отворите на GitHub.',
        'feed_loading': 'Зареждане на ангажименти от main...',
        'feed_empty': 'Няма ангажименти за показване.',
        'feed_error': 'Не може да се достигне до GitHub: {error}',
        'update_checking': 'Проверка на версията на сървъра GitHub...',
        'update_check': 'ПРОВЕРКА ЗА АКТУАЛИЗАЦИИ',
        'update_checking_button': 'ПРОВЕРКА...',
        'update_check_failed': 'Проверката на актуализацията е неуспешна: {error}',
        'update_retry': 'ОПИТАЙТЕ НОВО',
        'update_disconnected': 'Изтеглянето на сървъра не е свързано • GitHub main: {sha}',
        'open_repo': 'ОТВОРЕНО ХРАНИЛИЩЕ',
        'up_to_date': 'Сървърните файлове са актуални • {sha}',
        'up_to_date_button': 'АКТУАЛЕН',
        'update_available': 'Налична е актуализация на сървъра • {local} → {remote}',
        'update_button': 'АКТУАЛИЗАЦИЯ',
        'update_title': 'Актуализация на сървъра',
        'update_confirm': 'Най-новите DSOR_Server main файлове ще бъдат изтеглени от GitHub.\n'
                          '\n'
                          'Ангажимент: {sha}\n'
                          '{message}\n'
                          '\n'
                          'Git не е задължителен. Чистото плащане на Git се превърта бързо напред; '
                          'инсталации без Git инсталирайте GitHub архива.',
        'update_running': 'Извличане на сървърни файлове от GitHub…',
        'update_running_button': 'АКТУАЛИЗИРАНЕ...',
        'update_done': 'Сървърът е актуализиран • {sha} • Рестартирайте сървъра, за да приложите '
                       'промените.',
        'update_done_title': 'Актуализацията е завършена',
        'update_done_message': 'Сървърните файлове са актуализирани. Рестартирайте сървъра, след '
                               'това затворете и отворете отново стартовия панел.',
        'update_failed': 'Не може да се приложи актуализация: {error}',
        'update_retry_check': 'ПРОВЕРКА ОТНОВО',
        'settings': 'НАСТРОЙКИ',
        'play': 'ИГРА',
        'settings_title': 'НАСТРОЙКИ НА СТАРТОВАТА ПРОГРАМА',
        'client_folder': 'Папка на клиента на играта',
        'browse': 'ПРЕГЛЕД…',
        'save_settings': 'ЗАПАЗЕТЕ НАСТРОЙКИТЕ',
        'test_connection': 'ТЕСТОВА ВРЪЗКА',
        'testing_connection': 'Тестване на връзката със сървъра...',
        'connection_ok': 'Връзката със сървъра е успешна.',
        'connection_failed': 'Не може да се свърже със сървъра: {error}',
        'client_path_required': 'Първо изберете папката на клиента на играта.',
        'client_path_invalid': 'Избраната папка не съдържа валиден DSO клиент.',
        'unpack_client': 'РАЗПАКОВАНЕ НА КЛИЕНТА',
        'checking_client': 'Проверка на изтеглянето на клиента…',
        'client_not_complete': 'Клиентските файлове са непълни или не могат да бъдат проверени: '
                               '{error}',
        'unpack_confirm': 'Клиентските пакети са проверени ({bundles} пакети). Те ще бъдат '
                          'разопаковани в избраната папка на играта, след което текущите сървърни '
                          'активи ще заменят по-старите си версии в data/. Това може да използва '
                          'около 7 GB допълнително пространство. Индексът dlcache на клиента се '
                          'архивира, преди да бъде заменен. Съществуващият data/quests.xml също ще '
                          'бъде актуализиран. Продължаване?\\n\\nСпрете сървъра, преди да '
                          'продължите.\n'
                          '\n'
                          'Старите карти, навигация и езикови файлове, които липсват в този кеш, '
                          'ще бъдат премахнати.',
        'unpack_running': 'Разопаковане на клиентски пакети и опресняване на сървърни данни…',
        'unpack_progress': '{line}',
        'unpack_done': 'Разопаковането на клиента и актуализирането на данните на сървъра е '
                       'завършено. Рестартирайте сървъра преди да играете.',
        'unpack_failed': 'Неуспешно разопаковане на клиента: {error}',
        'unpack_no_script': 'setup_client_assets.py не беше намерен до стартовия панел.',
        'settings_saved': 'Настройките са запазени.'},
 'cs': {'subtitle': 'Spouštěč komunity DSOR',
        'server_ip': 'IP adresa serveru',
        'game_language': 'Herní jazyk',
        'err_ip': 'Zadejte platnou IP adresu serveru.',
        'err_language': 'Vybraný jazyk hry není podporován.',
        'username': 'Uživatelské jméno',
        'password': 'Heslo',
        'password2': 'Heslo (opakování)',
        'remember': 'Zapamatovat účet',
        'old_look': 'Starý vzhled položky (staré rámečky a pozadí)',
        'inventory_vfx': 'Oprava VFX inventáře (tato relace)',
        'err_inventory_vfx': 'Oprava inventáře VFX nepodporuje toto sestavení klienta.',
        'play_saved': 'PLAY',
        'login': 'PŘIHLÁSIT SE',
        'register': 'VYTVOŘIT ÚČET',
        'connecting': 'PŘIPOJOVÁNÍ...',
        'to_register': 'Ještě nemáte účet? Vytvořte jeden',
        'to_login': 'Již máte účet? Přihlaste se',
        'version': 'DSOR • Komunitní server',
        'err_fields': 'Je vyžadováno uživatelské jméno a heslo.',
        'err_pw_match': 'Hesla se neshodují.',
        'status_creating': 'Vytváření účtu...',
        'status_created': 'Účet vytvořen! Přihlašování...',
        'status_connecting': 'Připojování k serveru...',
        'status_launching': 'Spouštění hry...',
        'err_conn': 'Nelze se připojit k serveru. Zkontrolujte adresu serveru a připojení a zkuste '
                    'to znovu.',
        'err_no_client': 'dro_client64.exe nebyl nalezen! \n'
                         ' Zkontrolujte složku Client\\dlcache nebo DSOClient\\dlcache.',
        'err_launch': 'Přihlášení se nezdařilo (HTTP {code})',
        'err_register': 'Registrace se nezdařila (HTTP {code})',
        'language': 'Jazyk',
        'feed_title': 'DSOR_SERVER • POSLEDNÍ COMMIT NA MAIN',
        'feed_hint': 'Poklepáním na potvrzení jej otevřete na GitHub.',
        'feed_loading': 'Načítání potvrzení ze main…',
        'feed_empty': 'Žádné závazky k zobrazení.',
        'feed_error': 'Nelze dosáhnout GitHub: {error}',
        'update_checking': 'Kontrola revize serveru GitHub…',
        'update_check': 'ZKONTROLUJTE AKTUALIZACE',
        'update_checking_button': 'KONTROLA…',
        'update_check_failed': 'Kontrola aktualizace selhala: {error}',
        'update_retry': 'OPAKOVAT',
        'update_disconnected': 'Pokladna serveru není propojena • GitHub main: {sha}',
        'open_repo': 'OTEVŘENÉ ÚLOŽIŠTĚ',
        'up_to_date': 'Soubory serveru jsou aktuální • {sha}',
        'up_to_date_button': 'AKTUÁLNÍ',
        'update_available': 'K dispozici aktualizace serveru • {local} → {remote}',
        'update_button': 'AKTUALIZACE',
        'update_title': 'Aktualizace serveru',
        'update_confirm': 'Nejnovější soubory DSOR_Server main budou staženy ze GitHub.\n'
                          '\n'
                          'Závazek: {sha}\n'
                          '{message}\n'
                          '\n'
                          'Git je volitelný. Čistá pokladna Git je rychle přeposílána; instalace '
                          'bez Gitu nainstalují archiv GitHub.',
        'update_running': 'Načítání serverových souborů ze GitHub…',
        'update_running_button': 'AKTUALIZACE…',
        'update_done': 'Server aktualizován • {sha} • Pro použití změn restartujte server.',
        'update_done_title': 'Aktualizace dokončena',
        'update_done_message': 'Soubory serveru jsou aktualizovány. Restartujte server, zavřete a '
                               'znovu otevřete spouštěč.',
        'update_failed': 'Nelze použít aktualizaci: {error}',
        'update_retry_check': 'ZKONTROLUJTE ZNOVU',
        'settings': 'NASTAVENÍ',
        'play': 'PLAY',
        'settings_title': 'NASTAVENÍ SPUŠTĚNÍ',
        'client_folder': 'Složka herního klienta',
        'browse': 'PROCHÁZET…',
        'save_settings': 'ULOŽIT NASTAVENÍ',
        'test_connection': 'ZKUŠEBNÍ PŘIPOJENÍ',
        'testing_connection': 'Testování připojení k serveru…',
        'connection_ok': 'Připojení k serveru bylo úspěšné.',
        'connection_failed': 'Nelze se připojit k serveru: {error}',
        'client_path_required': 'Nejprve vyberte složku herního klienta.',
        'client_path_invalid': 'Vybraná složka neobsahuje platného klienta DSO.',
        'unpack_client': 'ROZBALENÍ KLIENTA',
        'checking_client': 'Kontrola stahování klienta…',
        'client_not_complete': 'Klientské soubory jsou neúplné nebo je nebylo možné ověřit: '
                               '{error}',
        'unpack_confirm': 'Klientské balíčky byly ověřeny (balíčky {bundles}). Budou rozbaleny do '
                          'vybrané herní složky a poté aktuální serverové prostředky nahradí '
                          'jejich starší verze v data/. To může vyžadovat přibližně 7 GB dalšího '
                          'prostoru. Klientský index dlcache je před nahrazením zálohován. '
                          'Stávající soubor data/quests.xml bude také aktualizován. '
                          'Pokračovat?\\n\\nPřed pokračováním zastavte server.\n'
                          '\n'
                          'Staré mapy, navigační a jazykové soubory, které v této mezipaměti '
                          'nejsou, budou odstraněny.',
        'unpack_running': 'Rozbalování klientských balíčků a obnovování dat serveru…',
        'unpack_progress': '{line}',
        'unpack_done': 'Rozbalení klienta a aktualizace dat serveru dokončena. Před hraním '
                       'restartujte server.',
        'unpack_failed': 'Rozbalení klienta se nezdařilo: {error}',
        'unpack_no_script': 'setup_client_assets.py nebyl nalezen vedle spouštěče.',
        'settings_saved': 'Nastavení bylo uloženo.'},
 'de': {'subtitle': 'DSOR Community Launcher',
        'server_ip': 'Server-IP-Adresse',
        'game_language': 'Spielsprache',
        'err_ip': 'Geben Sie eine gültige Server-IP-Adresse ein.',
        'err_language': 'Die ausgewählte Spielsprache wird nicht unterstützt.',
        'username': 'Benutzername',
        'password': 'Passwort',
        'password2': 'Passwort (wiederholen)',
        'remember': 'Konto merken',
        'old_look': 'Aussehen alter Artikel (alte Rahmen und Hintergründe)',
        'inventory_vfx': 'Inventar-VFX-Korrektur (diese Sitzung)',
        'err_inventory_vfx': 'Der Inventar-VFX-Fix unterstützt diesen Client-Build nicht.',
        'play_saved': 'SPIELEN',
        'login': 'ANMELDEN',
        'register': 'KONTO ERSTELLEN',
        'connecting': 'VERBINDEN...',
        'to_register': 'Noch kein Konto? Erstellen Sie eins',
        'to_login': 'Sie haben bereits ein Konto? Melden Sie sich an',
        'version': 'DSOR • Community-Server',
        'err_fields': 'Benutzername und Passwort sind erforderlich.',
        'err_pw_match': 'Passwörter stimmen nicht überein.',
        'status_creating': 'Konto wird erstellt...',
        'status_created': 'Konto erstellt! Anmelden...',
        'status_connecting': 'Verbindung zum Server wird hergestellt...',
        'status_launching': 'Spiel wird gestartet...',
        'err_conn': 'Verbindung zum Server konnte nicht hergestellt werden. Überprüfen Sie die '
                    'Serveradresse und Ihre Verbindung und versuchen Sie es dann erneut.',
        'err_no_client': 'dro_client64.exe nicht gefunden! \n'
                         ' Überprüfen Sie den Ordner Client\\dlcache oder DSOClient\\dlcache.',
        'err_launch': 'Anmeldung fehlgeschlagen (HTTP {code})',
        'err_register': 'Registrierung fehlgeschlagen (HTTP {code})',
        'language': 'Sprache',
        'feed_title': 'DSOR_SERVER • LETZTER COMMIT AUF MAIN',
        'feed_hint': 'Doppelklicken Sie auf ein Commit, um es auf GitHub zu öffnen.',
        'feed_loading': 'Commits werden von main geladen…',
        'feed_empty': 'Keine Commits zum Anzeigen.',
        'feed_error': 'GitHub: {error} konnte nicht erreicht werden',
        'update_checking': 'Überprüfung der GitHub-Serverrevision…',
        'update_check': 'NACH UPDATES PRÜFEN',
        'update_checking_button': 'ÜBERPRÜFEN…',
        'update_check_failed': 'Aktualisierungsprüfung fehlgeschlagen: {error}',
        'update_retry': 'WIEDERHOLEN',
        'update_disconnected': 'Server-Checkout nicht verknüpft • GitHub main: {sha}',
        'open_repo': 'REPOSITORY ÖFFNEN',
        'up_to_date': 'Serverdateien sind aktuell • {sha}',
        'up_to_date_button': 'AKTUELL',
        'update_available': 'Server-Update verfügbar • {local} → {remote}',
        'update_button': 'UPDATE',
        'update_title': 'Server-Update',
        'update_confirm': 'Die neuesten DSOR_Server main-Dateien werden von GitHub '
                          'heruntergeladen.\n'
                          '\n'
                          'Commit: {sha}\n'
                          '{message}\n'
                          '\n'
                          'Git ist optional. Ein sauberer Git-Checkout erfolgt im Schnellvorlauf; '
                          'Installationen ohne Git installieren das GitHub-Archiv.',
        'update_running': 'Serverdateien werden von GitHub abgerufen…',
        'update_running_button': 'AKTUALISIERT…',
        'update_done': 'Server aktualisiert • {sha} • Starten Sie den Server neu, um die '
                       'Änderungen zu übernehmen.',
        'update_done_title': 'Aktualisierung abgeschlossen',
        'update_done_message': 'Die Serverdateien werden aktualisiert. Starten Sie den Server neu, '
                               'schließen Sie dann den Launcher und öffnen Sie ihn erneut.',
        'update_failed': 'Update konnte nicht angewendet werden: {error}',
        'update_retry_check': 'NOCHMALS PRÜFEN',
        'settings': 'EINSTELLUNGEN',
        'play': 'SPIELEN',
        'settings_title': 'LAUNCHER-EINSTELLUNGEN',
        'client_folder': 'Game-Client-Ordner',
        'browse': 'DURCHSUCHEN…',
        'save_settings': 'EINSTELLUNGEN SPEICHERN',
        'test_connection': 'TESTVERBINDUNG',
        'testing_connection': 'Serververbindung testen…',
        'connection_ok': 'Verbindung zum Server erfolgreich.',
        'connection_failed': 'Verbindung zum Server konnte nicht hergestellt werden: {error}',
        'client_path_required': 'Wählen Sie zuerst den Spiel-Client-Ordner aus.',
        'client_path_invalid': 'Der ausgewählte Ordner enthält keinen gültigen DSO-Client.',
        'unpack_client': 'CLIENT AUSPACKEN',
        'checking_client': 'Client-Download wird überprüft…',
        'client_not_complete': 'Clientdateien sind unvollständig oder konnten nicht überprüft '
                               'werden: {error}',
        'unpack_confirm': 'Die Client-Bundles wurden überprüft ({bundles}-Bundles). Sie werden in '
                          'den ausgewählten Spielordner entpackt, dann ersetzen die aktuellen '
                          'Server-Assets ihre älteren Versionen in data/. Dies kann etwa 7 GB '
                          'zusätzlichen Speicherplatz beanspruchen. Der Client-dlcache-Index wird '
                          'gesichert, bevor er ersetzt wird. Vorhandene data/quests.xml werden '
                          'ebenfalls aktualisiert. Weitermachen?\\n\\nStoppen Sie den Server, '
                          'bevor Sie fortfahren.\n'
                          '\n'
                          'Alte Karten-, Navigations- und Sprachdateien, die in diesem Cache '
                          'fehlen, werden entfernt.',
        'unpack_running': 'Client-Bundles entpacken und Serverdaten aktualisieren…',
        'unpack_progress': '{line}',
        'unpack_done': 'Entpacken des Clients und Aktualisierung der Serverdaten abgeschlossen. '
                       'Starten Sie den Server neu, bevor Sie spielen.',
        'unpack_failed': 'Entpacken des Clients fehlgeschlagen: {error}',
        'unpack_no_script': 'setup_client_assets.py wurde neben dem Launcher nicht gefunden.',
        'settings_saved': 'Einstellungen gespeichert.'},
 'el': {'subtitle': 'DSOR Community Launcher',
        'server_ip': 'Διεύθυνση IP διακομιστή',
        'game_language': 'Γλώσσα παιχνιδιού',
        'err_ip': 'Εισαγάγετε μια έγκυρη διεύθυνση IP διακομιστή.',
        'err_language': 'Η επιλεγμένη γλώσσα παιχνιδιού δεν υποστηρίζεται.',
        'username': 'Όνομα χρήστη',
        'password': 'Κωδικός πρόσβασης',
        'password2': 'Κωδικός πρόσβασης (επανάληψη)',
        'remember': 'Απομνημόνευση λογαριασμού',
        'old_look': 'Εμφάνιση παλαιού αντικειμένου (παλιά πλαίσια και φόντο)',
        'inventory_vfx': 'Διόρθωση VFX αποθέματος (αυτή η περίοδος σύνδεσης)',
        'err_inventory_vfx': 'Η ενημέρωση κώδικα VFX αποθέματος δεν υποστηρίζει αυτήν την έκδοση '
                             'πελάτη.',
        'play_saved': 'PLAY',
        'login': 'ΕΙΣΟΔΟΣ',
        'register': 'ΔΗΜΙΟΥΡΓΙΑ ΛΟΓΑΡΙΑΣΜΟΥ',
        'connecting': 'ΣΥΝΔΕΣΗ...',
        'to_register': 'Δεν έχετε λογαριασμό ακόμα; Δημιουργήστε ένα',
        'to_login': 'Έχετε ήδη λογαριασμό; Συνδεθείτε',
        'version': 'DSOR • Διακομιστής κοινότητας',
        'err_fields': 'Απαιτούνται όνομα χρήστη και κωδικός πρόσβασης.',
        'err_pw_match': 'Οι κωδικοί πρόσβασης δεν ταιριάζουν.',
        'status_creating': 'Δημιουργία λογαριασμού...',
        'status_created': 'Ο λογαριασμός δημιουργήθηκε! Σύνδεση...',
        'status_connecting': 'Σύνδεση με διακομιστή...',
        'status_launching': 'Εκκίνηση παιχνιδιού...',
        'err_conn': 'Δεν ήταν δυνατή η σύνδεση με τον διακομιστή. Ελέγξτε τη διεύθυνση διακομιστή '
                    'και τη σύνδεσή σας και δοκιμάστε ξανά.',
        'err_no_client': 'Το dro_client64.exe δεν βρέθηκε! \n'
                         ' Ελέγξτε το φάκελο Client\\dlcache ή DSOClient\\dlcache.',
        'err_launch': 'Η σύνδεση απέτυχε (HTTP {code})',
        'err_register': 'Η εγγραφή απέτυχε (HTTP {code})',
        'language': 'Γλώσσα',
        'feed_title': 'DSOR_SERVER • ΤΕΛΕΥΤΑΙΟ COMMIT ΣΤΟ MAIN',
        'feed_hint': 'Κάντε διπλό κλικ σε μια δέσμευση για να την ανοίξετε στο GitHub.',
        'feed_loading': 'Φόρτωση δεσμεύσεων από το main…',
        'feed_empty': 'Δεν δεσμεύεται για εμφάνιση.',
        'feed_error': 'Δεν ήταν δυνατή η πρόσβαση στο GitHub: {error}',
        'update_checking': 'Έλεγχος της αναθεώρησης διακομιστή GitHub…',
        'update_check': 'ΕΛΕΓΧΟΣ ΓΙΑ ΕΝΗΜΕΡΩΣΕΙΣ',
        'update_checking_button': 'ΕΛΕΓΧΟΣ…',
        'update_check_failed': 'Ο έλεγχος ενημέρωσης απέτυχε: {error}',
        'update_retry': 'ΕΠΑΝΑΛΗΨΗ',
        'update_disconnected': 'Η ολοκλήρωση αγοράς διακομιστή δεν είναι συνδεδεμένη • GitHub '
                               'main: {sha}',
        'open_repo': 'ΑΝΟΙΧΤΟ ΑΠΟΘΗΚΕΥΤΙΚΟ',
        'up_to_date': 'Τα αρχεία διακομιστή είναι ενημερωμένα • {sha}',
        'up_to_date_button': 'ΕΝΗΜΕΡΩΣΗ',
        'update_available': 'Διαθέσιμη ενημέρωση διακομιστή • {local} → {remote}',
        'update_button': 'ΕΝΗΜΕΡΩΣΗ',
        'update_title': 'Ενημέρωση διακομιστή',
        'update_confirm': 'Τα πιο πρόσφατα αρχεία DSOR_Server main θα ληφθούν από το GitHub.\n'
                          '\n'
                          'Δέσμευση: {sha}\n'
                          '{message}\n'
                          '\n'
                          'Το Git είναι προαιρετικό. Ένα καθαρό ταμείο Git προωθείται γρήγορα. '
                          'εγκαταστάσεις χωρίς Git Εγκαταστήστε το αρχείο GitHub.',
        'update_running': 'Ανάκτηση αρχείων διακομιστή από το GitHub…',
        'update_running_button': 'ΕΝΗΜΕΡΩΣΗ…',
        'update_done': 'Ενημερώθηκε ο διακομιστής • {sha} • Επανεκκινήστε τον διακομιστή για να '
                       'εφαρμόσετε τις αλλαγές.',
        'update_done_title': 'Η ενημέρωση ολοκληρώθηκε',
        'update_done_message': 'Τα αρχεία διακομιστή έχουν ενημερωθεί. Κάντε επανεκκίνηση του '
                               'διακομιστή και, στη συνέχεια, κλείστε και ανοίξτε ξανά τη '
                               'λειτουργία εκκίνησης.',
        'update_failed': 'Δεν ήταν δυνατή η εφαρμογή της ενημέρωσης: {error}',
        'update_retry_check': 'ΕΛΕΓΞΤΕ ΞΑΝΑ',
        'settings': 'ΡΥΘΜΙΣΕΙΣ',
        'play': 'PLAY',
        'settings_title': 'ΡΥΘΜΙΣΕΙΣ ΕΚΚΙΝΗΣΗΣ',
        'client_folder': 'Φάκελος πελάτη παιχνιδιού',
        'browse': 'ΠΕΡΙΗΓΗΣΗ…',
        'save_settings': 'ΑΠΟΘΗΚΕΥΣΗ ΡΥΘΜΙΣΕΩΝ',
        'test_connection': 'ΔΟΚΙΜΗ ΣΥΝΔΕΣΗ',
        'testing_connection': 'Δοκιμή σύνδεσης διακομιστή…',
        'connection_ok': 'Επιτυχής σύνδεση με τον διακομιστή.',
        'connection_failed': 'Δεν ήταν δυνατή η σύνδεση με τον διακομιστή: {error}',
        'client_path_required': 'Επιλέξτε πρώτα το φάκελο πελάτη του παιχνιδιού.',
        'client_path_invalid': 'Ο επιλεγμένος φάκελος δεν περιέχει έγκυρο πρόγραμμα-πελάτη DSO.',
        'unpack_client': 'ΑΠΟ ΣΥΣΚΕΥΑΣΙΑ ΠΕΛΑΤΗ',
        'checking_client': 'Έλεγχος λήψης προγράμματος-πελάτη…',
        'client_not_complete': 'Τα αρχεία πελάτη είναι ελλιπή ή δεν ήταν δυνατή η επαλήθευση: '
                               '{error}',
        'unpack_confirm': 'Τα πακέτα πελατών έχουν επαληθευτεί (πακέτα {bundles}). Θα '
                          'αποσυσκευαστούν στον επιλεγμένο φάκελο παιχνιδιού και, στη συνέχεια, τα '
                          'τρέχοντα στοιχεία διακομιστή θα αντικαταστήσουν τις παλαιότερες '
                          'εκδόσεις τους στο data/. Αυτό μπορεί να χρησιμοποιεί περίπου 7 GB '
                          'επιπλέον χώρου. Προτού αντικατασταθεί, δημιουργείται αντίγραφο '
                          'ασφαλείας του ευρετηρίου πελάτη dlcache. Το υπάρχον data/quests.xml θα '
                          'ενημερωθεί επίσης. Συνεχίζω;\\n\\nΣταματήστε τον διακομιστή πριν '
                          'συνεχίσετε.\n'
                          '\n'
                          'Οι παλιοί χάρτες, τα αρχεία πλοήγησης και γλώσσας που λείπουν από αυτήν '
                          'την cache θα αφαιρεθούν.',
        'unpack_running': 'Αποσυσκευασία δεσμίδων πελατών και ανανέωση δεδομένων διακομιστή…',
        'unpack_progress': '{line}',
        'unpack_done': 'Αποσυσκευασία πελάτη και ενημέρωση δεδομένων διακομιστή ολοκληρώθηκε. '
                       'Κάντε επανεκκίνηση του διακομιστή πριν από την αναπαραγωγή.',
        'unpack_failed': 'Η αποσυσκευασία του πελάτη απέτυχε: {error}',
        'unpack_no_script': 'Το setup_client_assets.py δεν βρέθηκε δίπλα στον εκτοξευτή.',
        'settings_saved': 'Οι ρυθμίσεις αποθηκεύτηκαν.'},
 'es': {'subtitle': 'Lanzador de la comunidad DSOR',
        'server_ip': 'Dirección IP del servidor',
        'game_language': 'Idioma del juego',
        'err_ip': 'Introduzca una dirección IP de servidor válida.',
        'err_language': 'El idioma del juego seleccionado no es compatible.',
        'username': 'Nombre de usuario',
        'password': 'Contraseña',
        'password2': 'Contraseña (repetir)',
        'remember': 'Recordar cuenta',
        'old_look': 'Aspecto antiguo del elemento (marcos y fondos antiguos)',
        'inventory_vfx': 'Corrección de efectos visuales de inventario (esta sesión)',
        'err_inventory_vfx': 'La corrección de VFX del inventario no es compatible con esta '
                             'compilación de cliente.',
        'play_saved': 'JUGAR',
        'login': 'INICIAR SESIÓN',
        'register': 'CREAR CUENTA',
        'connecting': 'CONECTANDO...',
        'to_register': '¿Aún no tienes cuenta? Crea uno',
        'to_login': '¿Ya tienes una cuenta? Iniciar sesión',
        'version': 'DSOR • Servidor de la comunidad',
        'err_fields': 'Se requieren nombre de usuario y contraseña.',
        'err_pw_match': 'Las contraseñas no coinciden.',
        'status_creating': 'Creando cuenta...',
        'status_created': '¡Cuenta creada! Iniciando sesión...',
        'status_connecting': 'Conectándose al servidor...',
        'status_launching': 'Iniciando juego...',
        'err_conn': 'No se pudo conectar al servidor. Verifique la dirección del servidor y su '
                    'conexión, luego inténtelo nuevamente.',
        'err_no_client': '¡dro_client64.exe no encontrado! \n'
                         ' Verifique la carpeta Client\\dlcache o DSOClient\\dlcache.',
        'err_launch': 'Error al iniciar sesión (HTTP {code})',
        'err_register': 'Error en el registro (HTTP {code})',
        'language': 'Idioma',
        'feed_title': 'DSOR_SERVER • ÚLTIMO COMMIT EN MAIN',
        'feed_hint': 'Haga doble clic en una confirmación para abrirla en GitHub.',
        'feed_loading': 'Cargando confirmaciones desde main…',
        'feed_empty': 'No hay confirmaciones para mostrar.',
        'feed_error': 'No se pudo alcanzar GitHub: {error}',
        'update_checking': 'Comprobando la revisión del servidor GitHub…',
        'update_check': 'VERIFICAR ACTUALIZACIONES',
        'update_checking_button': 'COMPROBANDO…',
        'update_check_failed': 'Error en la verificación de actualización: {error}',
        'update_retry': 'REINTENTAR',
        'update_disconnected': 'Pago del servidor no vinculado • GitHub main: {sha}',
        'open_repo': 'REPOSITORIO ABIERTO',
        'up_to_date': 'Los archivos del servidor están actualizados • {sha}',
        'up_to_date_button': 'ACTUALIZADO',
        'update_available': 'Actualización del servidor disponible • {local} → {remote}',
        'update_button': 'ACTUALIZACIÓN',
        'update_title': 'Actualización del servidor',
        'update_confirm': 'Los archivos DSOR_Server main más recientes se descargarán de GitHub.\n'
                          '\n'
                          'Confirmar: {sha}\n'
                          '{message}\n'
                          '\n'
                          'Git es opcional. Una copia de Git limpia se actualiza mediante avance '
                          'rápido; las instalaciones sin Git descargan el archivo de GitHub.',
        'update_running': 'Obteniendo archivos del servidor de GitHub…',
        'update_running_button': 'ACTUALIZANDO…',
        'update_done': 'Servidor actualizado • {sha} • Reinicie el servidor para aplicar los '
                       'cambios.',
        'update_done_title': 'Actualización completa',
        'update_done_message': 'Los archivos del servidor están actualizados. Reinicie el '
                               'servidor, luego cierre y vuelva a abrir el iniciador.',
        'update_failed': 'No se pudo aplicar la actualización: {error}',
        'update_retry_check': 'COMPROBAR DE NUEVO',
        'settings': 'AJUSTES',
        'play': 'JUGAR',
        'settings_title': 'CONFIGURACIÓN DEL LANZADOR',
        'client_folder': 'Carpeta del cliente del juego',
        'browse': 'NAVEGAR…',
        'save_settings': 'GUARDAR AJUSTES',
        'test_connection': 'CONEXIÓN DE PRUEBA',
        'testing_connection': 'Probando la conexión del servidor…',
        'connection_ok': 'La conexión al servidor fue exitosa.',
        'connection_failed': 'No se pudo conectar al servidor: {error}',
        'client_path_required': 'Seleccione primero la carpeta del cliente del juego.',
        'client_path_invalid': 'La carpeta seleccionada no contiene un cliente DSO válido.',
        'unpack_client': 'DESEMBALAJE DEL CLIENTE',
        'checking_client': 'Comprobando la descarga del cliente…',
        'client_not_complete': 'Los archivos del cliente están incompletos o no se pudieron '
                               'verificar: {error}',
        'unpack_confirm': 'Se han verificado los paquetes de cliente (paquetes {bundles}). Se '
                          'descomprimirán en la carpeta del juego seleccionado y luego los activos '
                          'del servidor actual reemplazarán sus versiones anteriores en data/. '
                          'Esto puede utilizar unos 7 GB de espacio adicional. Se realiza una '
                          'copia de seguridad del índice dlcache del cliente antes de '
                          'reemplazarlo. También se actualizará el data/quests.xml existente. '
                          '¿Continuar?\\n\\nDetén el servidor antes de continuar.\n'
                          '\n'
                          'Se eliminarán los mapas y archivos de navegación e idioma antiguos que '
                          'no estén en esta caché.',
        'unpack_running': 'Desempaquetando paquetes de clientes y actualizando datos del '
                          'servidor...',
        'unpack_progress': '{line}',
        'unpack_done': 'Se completó el descomprimido del cliente y la actualización de los datos '
                       'del servidor. Reinicie el servidor antes de jugar.',
        'unpack_failed': 'Error al descomprimir el cliente: {error}',
        'unpack_no_script': 'No se encontró setup_client_assets.py al lado del iniciador.',
        'settings_saved': 'Ajustes guardados.'},
 'fa': {'subtitle': 'راه\u200cانداز انجمن DSOR',
        'server_ip': 'آدرس IP سرور',
        'game_language': 'زبان بازی',
        'err_ip': 'یک آدرس IP سرور معتبر وارد کنید.',
        'err_language': 'زبان بازی انتخاب شده پشتیبانی نمی شود.',
        'username': 'نام کاربری',
        'password': 'رمز عبور',
        'password2': 'رمز عبور (تکرار)',
        'remember': 'حساب کاربری را به خاطر بسپارید',
        'old_look': 'ظاهر آیتم قدیمی (قاب ها و پس زمینه های قدیمی)',
        'inventory_vfx': 'رفع VFX موجودی (این جلسه)',
        'err_inventory_vfx': 'تصحیح موجودی VFX از این ساخت کلاینت پشتیبانی نمی کند.',
        'play_saved': 'PLAY',
        'login': 'وارد شوید',
        'register': 'ایجاد حساب',
        'connecting': 'در حال اتصال...',
        'to_register': 'هنوز حساب کاربری ندارید؟ یکی ایجاد کنید',
        'to_login': 'از قبل حساب کاربری دارید؟ وارد شوید',
        'version': 'DSOR • سرور جامعه',
        'err_fields': 'نام کاربری و رمز عبور مورد نیاز است.',
        'err_pw_match': 'رمزهای عبور مطابقت ندارند.',
        'status_creating': 'ایجاد حساب کاربری...',
        'status_created': 'حساب ایجاد شد! ورود به سیستم...',
        'status_connecting': 'اتصال به سرور...',
        'status_launching': 'راه اندازی بازی...',
        'err_conn': 'نمی توان به سرور متصل شد. آدرس سرور و اتصال خود را بررسی کنید، سپس دوباره '
                    'امتحان کنید.',
        'err_no_client': 'dro_client64.exe یافت نشد! \n'
                         ' پوشه Client\\dlcache یا DSOClient\\dlcache را بررسی کنید.',
        'err_launch': 'ورود ناموفق بود (HTTP {code})',
        'err_register': 'ثبت نام انجام نشد (HTTP {code})',
        'language': 'زبان',
        'feed_title': 'DSOR_SERVER • آخرین commit در main',
        'feed_hint': 'روی یک commit دوبار کلیک کنید تا در GitHub باز شود.',
        'feed_loading': 'بارگیری تعهدات از main…',
        'feed_empty': 'هیچ تعهدی برای نمایش وجود ندارد.',
        'feed_error': 'نمی توان به GitHub رسید: {error}',
        'update_checking': 'بررسی ویرایش سرور GitHub…',
        'update_check': 'برای به روز رسانی ها بررسی کنید',
        'update_checking_button': 'در حال بررسی…',
        'update_check_failed': 'بررسی به روز رسانی انجام نشد: {error}',
        'update_retry': 'دوباره امتحان کنید',
        'update_disconnected': 'تسویه حساب سرور پیوند داده نشده است • GitHub main: {sha}',
        'open_repo': 'باز کردن مخزن',
        'up_to_date': 'فایل های سرور به روز هستند • {sha}',
        'up_to_date_button': 'به روز',
        'update_available': 'به روز رسانی سرور موجود است • {local} → {remote}',
        'update_button': 'به روز رسانی',
        'update_title': 'به روز رسانی سرور',
        'update_confirm': 'آخرین فایل های DSOR_Server main از GitHub دانلود می شود.\n'
                          '\n'
                          'تعهد: {sha}\n'
                          '{message}\n'
                          '\n'
                          'Git اختیاری است. پرداخت Git تمیز به سرعت ارسال می شود. نصب بدون Git '
                          'بایگانی GitHub را نصب کنید.',
        'update_running': 'واکشی فایل های سرور از GitHub…',
        'update_running_button': 'در حال به روز رسانی…',
        'update_done': 'سرور به روز شد • {sha} • سرور را راه اندازی مجدد کنید تا تغییرات اعمال '
                       'شود.',
        'update_done_title': 'به روز رسانی کامل شد',
        'update_done_message': 'فایل های سرور به روز می شوند. سرور را مجددا راه اندازی کنید، سپس '
                               'لانچر را ببندید و دوباره باز کنید.',
        'update_failed': 'به روز رسانی نمی تواند اعمال شود: {error}',
        'update_retry_check': 'دوباره چک کنید',
        'settings': 'تنظیمات',
        'play': 'PLAY',
        'settings_title': 'تنظیمات پرتاب کننده',
        'client_folder': 'پوشه کلاینت بازی',
        'browse': 'مرور…',
        'save_settings': 'ذخیره تنظیمات',
        'test_connection': 'اتصال تست',
        'testing_connection': 'تست اتصال سرور…',
        'connection_ok': 'اتصال به سرور با موفقیت انجام شد.',
        'connection_failed': 'نمی توان به سرور متصل شد: {error}',
        'client_path_required': 'ابتدا پوشه کلاینت بازی را انتخاب کنید.',
        'client_path_invalid': 'پوشه انتخاب شده حاوی یک سرویس گیرنده DSO معتبر نیست.',
        'unpack_client': 'بسته بندی مشتری',
        'checking_client': 'بررسی دانلود مشتری…',
        'client_not_complete': 'فایل های سرویس گیرنده ناقص هستند یا قابل تأیید نیستند: {error}',
        'unpack_confirm': 'بسته های مشتری تأیید شده اند (بسته های {bundles}). آنها در پوشه بازی '
                          'انتخابی باز می شوند، سپس دارایی های سرور فعلی جایگزین نسخه های قدیمی '
                          'خود در data/ می شوند. این ممکن است از حدود 7 گیگابایت فضای اضافی '
                          'استفاده کند. شاخص مشتری dlcache قبل از جایگزینی پشتیبان گیری می شود. '
                          'data/quests.xml موجود نیز به روز خواهد شد. ادامه دهید؟\\n\\nپیش از '
                          'ادامه سرور را متوقف کنید.\n'
                          '\n'
                          'نقشه\u200cها و فایل\u200cهای ناوبری و زبان قدیمی که در این کش نیستند '
                          'حذف می\u200cشوند.',
        'unpack_running': 'بسته\u200cبندی بسته\u200cهای مشتری و تازه\u200cسازی داده\u200cهای سرور…',
        'unpack_progress': '{line}',
        'unpack_done': 'بسته بندی کلاینت و به روز رسانی داده های سرور تکمیل شد. قبل از بازی سرور '
                       'را ریستارت کنید.',
        'unpack_failed': 'بسته کلاینت باز نشد: {error}',
        'unpack_no_script': 'setup_client_assets.py در کنار پرتابگر یافت نشد.',
        'settings_saved': 'تنظیمات ذخیره شد.'},
 'fr': {'subtitle': 'Lanceur de communauté DSOR',
        'server_ip': 'Adresse IP du serveur',
        'game_language': 'Langage du jeu',
        'err_ip': 'Entrez une adresse IP de serveur valide.',
        'err_language': "La langue du jeu sélectionnée n'est pas prise en charge.",
        'username': "Nom d'utilisateur",
        'password': 'Mot de passe',
        'password2': 'Mot de passe (répéter)',
        'remember': 'Mémoriser le compte',
        'old_look': "Ancien aspect de l'objet (anciens cadres et arrière-plans)",
        'inventory_vfx': "Correctif VFX d'inventaire (cette session)",
        'err_inventory_vfx': "Le correctif VFX d'inventaire ne prend pas en charge cette version "
                             'client.',
        'play_saved': 'JOUER',
        'login': 'CONNEXION',
        'register': 'CRÉER UN COMPTE',
        'connecting': 'CONNEXION...',
        'to_register': 'Pas encore de compte ? Créez-en un',
        'to_login': 'Vous avez déjà un compte ? Connectez-vous',
        'version': 'DSOR • Serveur communautaire',
        'err_fields': "Le nom d'utilisateur et le mot de passe sont requis.",
        'err_pw_match': 'Les mots de passe ne correspondent pas.',
        'status_creating': "Création d'un compte...",
        'status_created': 'Compte créé\xa0! Connexion...',
        'status_connecting': 'Connexion au serveur...',
        'status_launching': 'Lancement du jeu...',
        'err_conn': "Impossible de se connecter au serveur. Vérifiez l'adresse du serveur et votre "
                    'connexion, puis réessayez.',
        'err_no_client': 'dro_client64.exe introuvable\xa0! \n'
                         ' Vérifiez le dossier Client\\dlcache ou DSOClient\\dlcache.',
        'err_launch': 'Échec de la connexion (HTTP {code})',
        'err_register': "Échec de l'enregistrement (HTTP {code})",
        'language': 'Langue',
        'feed_title': 'DSOR_SERVER • DERNIER COMMIT SUR MAIN',
        'feed_hint': "Double-cliquez sur un commit pour l'ouvrir sur GitHub.",
        'feed_loading': 'Chargement des commits depuis main…',
        'feed_empty': 'Aucun commit à afficher.',
        'feed_error': "Impossible d'atteindre GitHub\xa0: {error}",
        'update_checking': 'Vérification de la révision du serveur GitHub…',
        'update_check': 'VÉRIFIER LES MISES À JOUR',
        'update_checking_button': 'VÉRIFICATION…',
        'update_check_failed': 'Échec de la vérification de la mise à jour\xa0: {error}',
        'update_retry': 'RÉESSAYER',
        'update_disconnected': 'Paiement du serveur non lié • GitHub main\xa0: {sha}',
        'open_repo': 'DÉPÔT OUVERT',
        'up_to_date': 'Les fichiers du serveur sont à jour • {sha}',
        'up_to_date_button': 'À JOUR',
        'update_available': 'Mise à jour du serveur disponible • {local} → {remote}',
        'update_button': 'MISE À JOUR',
        'update_title': 'Mise à jour du serveur',
        'update_confirm': 'Les derniers fichiers DSOR_Server main seront téléchargés depuis '
                          'GitHub.\n'
                          '\n'
                          'Validation\xa0: {sha}\n'
                          '{message}\n'
                          '\n'
                          'Git est facultatif. Une extraction Git propre est accélérée\xa0; les '
                          "installations sans Git installent l'archive GitHub.",
        'update_running': 'Récupération des fichiers du serveur depuis GitHub…',
        'update_running_button': 'MISE À JOUR…',
        'update_done': 'Serveur mis à jour • {sha} • Redémarrez le serveur pour appliquer les '
                       'modifications.',
        'update_done_title': 'Mise à jour terminée',
        'update_done_message': 'Les fichiers du serveur sont mis à jour. Redémarrez le serveur, '
                               'puis fermez et rouvrez le lanceur.',
        'update_failed': "Impossible d'appliquer la mise à jour\xa0: {error}",
        'update_retry_check': 'VÉRIFIER À NOUVEAU',
        'settings': 'RÉGLAGES',
        'play': 'JOUER',
        'settings_title': 'PARAMÈTRES DU LANCEUR',
        'client_folder': 'Dossier client du jeu',
        'browse': 'PARCOURIR…',
        'save_settings': 'ENREGISTRER LES PARAMÈTRES',
        'test_connection': 'TEST DE CONNEXION',
        'testing_connection': 'Test de la connexion au serveur…',
        'connection_ok': 'La connexion au serveur a réussi.',
        'connection_failed': 'Impossible de se connecter au serveur\xa0: {error}',
        'client_path_required': "Sélectionnez d'abord le dossier client du jeu.",
        'client_path_invalid': 'Le dossier sélectionné ne contient pas de client DSO valide.',
        'unpack_client': 'DÉBALLER LE CLIENT',
        'checking_client': 'Vérification du téléchargement du client…',
        'client_not_complete': "Les dossiers clients sont incomplets ou n'ont pas pu être "
                               'vérifiés\xa0: {error}',
        'unpack_confirm': 'Les bundles clients ont été vérifiés (bundles {bundles}). Ils seront '
                          'décompressés dans le dossier de jeu sélectionné, puis les ressources '
                          'actuelles du serveur remplaceront leurs anciennes versions dans data/. '
                          "Cela peut utiliser environ 7 Go d'espace supplémentaire. L'index client "
                          "dlcache est sauvegardé avant d'être remplacé. Le fichier "
                          'data/quests.xml existant sera également mis à jour. '
                          'Continuer?\\n\\nArrêtez le serveur avant de continuer.\n'
                          '\n'
                          'Les anciennes cartes et ressources de navigation et de langue absentes '
                          'de ce cache seront supprimées.',
        'unpack_running': 'Déballage des bundles clients et actualisation des données du serveur…',
        'unpack_progress': '{line}',
        'unpack_done': 'Déballage du client et mise à jour des données du serveur terminées. '
                       'Redémarrez le serveur avant de jouer.',
        'unpack_failed': 'Échec du décompression du client\xa0: {error}',
        'unpack_no_script': "setup_client_assets.py n'a pas été trouvé à côté du lanceur.",
        'settings_saved': 'Paramètres enregistrés.'},
 'hu': {'subtitle': 'DSOR Community Launcher',
        'server_ip': 'Szerver IP-címe',
        'game_language': 'Játék nyelve',
        'err_ip': 'Adjon meg egy érvényes szerver IP-címet.',
        'err_language': 'A kiválasztott játéknyelv nem támogatott.',
        'username': 'Felhasználónév',
        'password': 'Jelszó',
        'password2': 'Jelszó (ismétlés)',
        'remember': 'Emlékezzen fiókra',
        'old_look': 'Régi tétel megjelenése (régi keretek és hátterek)',
        'inventory_vfx': 'Készlet VFX javítása (ez a munkamenet)',
        'err_inventory_vfx': 'A leltár VFX javítása nem támogatja ezt a kliens buildet.',
        'play_saved': 'LEJÁTSZÁS',
        'login': 'BEJELENTKEZÉS',
        'register': 'FIÓK LÉTREHOZÁSA',
        'connecting': 'CSATLAKOZTATÁS...',
        'to_register': 'Még nincs fiókja? Hozzon létre egyet',
        'to_login': 'Már van fiókja? Jelentkezzen be',
        'version': 'DSOR • Közösségi szerver',
        'err_fields': 'Felhasználónév és jelszó szükséges.',
        'err_pw_match': 'A jelszavak nem egyeznek.',
        'status_creating': 'Fiók létrehozása...',
        'status_created': 'Fiók létrehozva! Bejelentkezés...',
        'status_connecting': 'Csatlakozás a szerverhez...',
        'status_launching': 'Játék indítása...',
        'err_conn': 'Nem sikerült csatlakozni a szerverhez. Ellenőrizze a szerver címét és a '
                    'kapcsolatot, majd próbálja újra.',
        'err_no_client': 'dro_client64.exe nem található! \n'
                         ' Ellenőrizze a Client\\dlcache vagy a DSOClient\\dlcache mappát.',
        'err_launch': 'Sikertelen bejelentkezés (HTTP {code})',
        'err_register': 'A regisztráció sikertelen (HTTP {code})',
        'language': 'Nyelv',
        'feed_title': 'DSOR_SERVER • LEGUTÓBBI COMMIT A MAIN ÁGON',
        'feed_hint': 'Kattintson duplán egy véglegesítésre, hogy megnyissa a GitHub-en.',
        'feed_loading': 'Végrehajtások betöltése a main-ről…',
        'feed_empty': 'Nincs megjelenítésre vonatkozó kötelezettség.',
        'feed_error': 'Nem sikerült elérni a következőt: GitHub: {error}',
        'update_checking': 'A GitHub szerver verziójának ellenőrzése…',
        'update_check': 'FRISSÍTÉSEK ELLENŐRZÉSE',
        'update_checking_button': 'ELLENŐRZÉS…',
        'update_check_failed': 'A frissítés ellenőrzése sikertelen: {error}',
        'update_retry': 'ÚJRAPRÓBÁLÁS',
        'update_disconnected': 'A kiszolgáló fizetése nincs összekapcsolva • GitHub main: {sha}',
        'open_repo': 'REPOSITORY NYITÁSA',
        'up_to_date': 'A szerverfájlok naprakészek • {sha}',
        'up_to_date_button': 'AKTUÁLIS',
        'update_available': 'Szerverfrissítés elérhető • {local} → {remote}',
        'update_button': 'FRISSÍTÉS',
        'update_title': 'Szerverfrissítés',
        'update_confirm': 'A legújabb DSOR_Server main fájlok letöltése a GitHub webhelyről '
                          'történik.\n'
                          '\n'
                          'Kötelezettség: {sha}\n'
                          '{message}\n'
                          '\n'
                          'A Git opcionális. A tiszta Git-pénztár gyors előreküldhető; a Git '
                          'nélküli telepítések telepítik a GitHub archívumot.',
        'update_running': 'Szerverfájlok letöltése a GitHub-ből…',
        'update_running_button': 'FRISSÍTÉS…',
        'update_done': 'Szerver frissítve • {sha} • Indítsa újra a kiszolgálót a változtatások '
                       'alkalmazásához.',
        'update_done_title': 'Frissítés kész',
        'update_done_message': 'A szerverfájlok frissülnek. Indítsa újra a szervert, majd zárja be '
                               'és nyissa meg újra az indítót.',
        'update_failed': 'Nem sikerült alkalmazni a frissítést: {error}',
        'update_retry_check': 'ELLENŐRIZZE ÚJRA',
        'settings': 'BEÁLLÍTÁSOK',
        'play': 'LEJÁTSZÁS',
        'settings_title': 'INDÍTÓ BEÁLLÍTÁSAI',
        'client_folder': 'Játékkliens mappa',
        'browse': 'Böngészés…',
        'save_settings': 'BEÁLLÍTÁSOK MENTÉSE',
        'test_connection': 'TESZT CSATLAKOZÁS',
        'testing_connection': 'Szerverkapcsolat tesztelése…',
        'connection_ok': 'A szerverhez való kapcsolódás sikerült.',
        'connection_failed': 'Nem sikerült csatlakozni a szerverhez: {error}',
        'client_path_required': 'Először válassza ki a játékkliens mappáját.',
        'client_path_invalid': 'A kiválasztott mappa nem tartalmaz érvényes DSO klienst.',
        'unpack_client': 'KICSOMAGOLÓ KLIENS',
        'checking_client': 'Az ügyfél letöltésének ellenőrzése…',
        'client_not_complete': 'Az ügyfélfájlok hiányosak vagy nem ellenőrizhetők: {error}',
        'unpack_confirm': 'Az ügyfélcsomagok ellenőrzése megtörtént ({bundles} kötegek). '
                          'Kicsomagolásra kerülnek a kiválasztott játékmappába, majd a jelenlegi '
                          'szervereszközök lecserélik a régebbi verzióikat a data/-ben. Ez '
                          'körülbelül 7 GB további helyet foglalhat el. Az ügyfél-dlcache indexről '
                          'biztonsági másolat készül, mielőtt lecserélné. A meglévő '
                          'data/quests.xml fájl is frissül. Folytatja?\\n\\nFolytatás előtt '
                          'állítsd le a szervert.\n'
                          '\n'
                          'A gyorsítótárban nem szereplő régi térképek, navigációs és nyelvi '
                          'fájlok törlődnek.',
        'unpack_running': 'Klienscsomagok kicsomagolása és szerveradatok frissítése…',
        'unpack_progress': '{line}',
        'unpack_done': 'Az ügyfél kicsomagolása és a szerveradatok frissítése befejeződött. '
                       'Indítsa újra a szervert játék előtt.',
        'unpack_failed': 'Az ügyfél kicsomagolása nem sikerült: {error}',
        'unpack_no_script': 'setup_client_assets.py nem található az indító mellett.',
        'settings_saved': 'Beállítások mentve.'},
 'it': {'subtitle': 'Avvio comunità DSOR',
        'server_ip': 'Indirizzo IP del server',
        'game_language': 'Lingua del gioco',
        'err_ip': 'Immettere un indirizzo IP del server valido.',
        'err_language': 'La lingua del gioco selezionata non è supportata.',
        'username': 'Nome utente',
        'password': "Parola d'ordine",
        'password2': 'Password (ripetere)',
        'remember': 'Ricorda account',
        'old_look': "Aspetto vecchio dell'oggetto (vecchie cornici e sfondi)",
        'inventory_vfx': "Correzione degli effetti grafici dell'inventario (questa sessione)",
        'err_inventory_vfx': "La correzione degli effetti visivi dell'inventario non supporta "
                             'questa build del client.',
        'play_saved': 'GIOCA',
        'login': 'ACCEDI',
        'register': 'CREA ACCOUNT',
        'connecting': 'COLLEGAMENTO...',
        'to_register': 'Ancora nessun account? Creane uno',
        'to_login': 'Hai già un account? Accedi',
        'version': 'DSOR • Server comunitario',
        'err_fields': 'Sono richiesti nome utente e password.',
        'err_pw_match': 'Le password non corrispondono.',
        'status_creating': 'Creazione account...',
        'status_created': 'Account creato! Accesso...',
        'status_connecting': 'Connessione al server...',
        'status_launching': 'Avvio del gioco...',
        'err_conn': "Impossibile connettersi al server. Controlla l'indirizzo del server e la "
                    'connessione, quindi riprova.',
        'err_no_client': 'ZXCLIENTEEXEZX non trovato! \n'
                         ' Controllare la cartella Client\\dlcache o DSOClient\\dlcache.',
        'err_launch': 'Accesso non riuscito (HTTP {code})',
        'err_register': 'Registrazione non riuscita (HTTP {code})',
        'language': 'Lingua',
        'feed_title': 'DSOR_SERVER • ULTIMO COMMIT SU MAIN',
        'feed_hint': 'Fare doppio clic su un commit per aprirlo su GitHub.',
        'feed_loading': 'Caricamento commit da main…',
        'feed_empty': 'Nessun commit da mostrare.',
        'feed_error': 'Impossibile raggiungere GitHub: {error}',
        'update_checking': 'Controllo della revisione del server GitHub…',
        'update_check': 'VERIFICA AGGIORNAMENTI',
        'update_checking_button': 'VERIFICA...',
        'update_check_failed': 'Controllo aggiornamento non riuscito: {error}',
        'update_retry': 'RIPROVA',
        'update_disconnected': 'Checkout del server non collegato • GitHub main: {sha}',
        'open_repo': 'REPOSITORY APERTO',
        'up_to_date': 'I file del server sono aggiornati • {sha}',
        'up_to_date_button': 'AGGIORNATO',
        'update_available': 'Aggiornamento server disponibile • {local} → {remote}',
        'update_button': 'AGGIORNAMENTO',
        'update_title': 'Aggiornamento del server',
        'update_confirm': 'Gli ultimi file DSOR_Server main verranno scaricati da GitHub.\n'
                          '\n'
                          'Impegno: {sha}\n'
                          '{message}\n'
                          '\n'
                          'Git è facoltativo. Un checkout Git pulito viene eseguito rapidamente; '
                          "le installazioni senza Git installano l'archivio GitHub.",
        'update_running': 'Recupero dei file del server da GitHub…',
        'update_running_button': 'AGGIORNAMENTO…',
        'update_done': 'Server aggiornato • {sha} • Riavviare il server per applicare le '
                       'modifiche.',
        'update_done_title': 'Aggiornamento completato',
        'update_done_message': 'I file del server sono aggiornati. Riavvia il server, quindi '
                               'chiudi e riapri il programma di avvio.',
        'update_failed': "Impossibile applicare l'aggiornamento: {error}",
        'update_retry_check': 'VERIFICA ANCORA',
        'settings': 'IMPOSTAZIONI',
        'play': 'GIOCA',
        'settings_title': 'IMPOSTAZIONI DEL LANCIATORE',
        'client_folder': 'Cartella del client di gioco',
        'browse': 'SFOGLIA…',
        'save_settings': 'SALVA IMPOSTAZIONI',
        'test_connection': 'CONNESSIONE DI PROVA',
        'testing_connection': 'Test della connessione al server…',
        'connection_ok': 'Connessione al server riuscita.',
        'connection_failed': 'Impossibile connettersi al server: {error}',
        'client_path_required': 'Seleziona prima la cartella del client di gioco.',
        'client_path_invalid': 'La cartella selezionata non contiene un client DSO valido.',
        'unpack_client': 'DISIMBALLA CLIENT',
        'checking_client': 'Controllo del download del client…',
        'client_not_complete': 'I file del cliente sono incompleti o non possono essere '
                               'verificati: {error}',
        'unpack_confirm': 'I bundle client sono stati verificati (bundle {bundles}). Verranno '
                          'decompressi nella cartella del gioco selezionata, quindi le risorse del '
                          'server corrente sostituiranno le loro versioni precedenti in data/. Ciò '
                          'potrebbe utilizzare circa 7 GB di spazio aggiuntivo. Viene eseguito il '
                          "backup dell'indice dlcache del client prima di essere sostituito. Verrà "
                          'aggiornato anche il file data/quests.xml esistente. '
                          'Continuare?\\n\\nArresta il server prima di continuare.\n'
                          '\n'
                          'Le vecchie mappe e i file di navigazione e lingua assenti da questa '
                          'cache verranno rimossi.',
        'unpack_running': 'Disimballaggio dei bundle client e aggiornamento dei dati del server…',
        'unpack_progress': '{line}',
        'unpack_done': 'Disimballaggio del client e aggiornamento dei dati del server completati. '
                       'Riavvia il server prima di giocare.',
        'unpack_failed': 'Decompressione del client non riuscita: {error}',
        'unpack_no_script': 'setup_client_assets.py non è stato trovato accanto al launcher.',
        'settings_saved': 'Impostazioni salvate.'},
 'ko': {'subtitle': 'DSOR 커뮤니티 런처',
        'server_ip': '서버 IP 주소',
        'game_language': '게임 언어',
        'err_ip': '유효한 서버 IP 주소를 입력하세요.',
        'err_language': '선택한 게임 언어는 지원되지 않습니다.',
        'username': '사용자 이름',
        'password': '비밀번호',
        'password2': '비밀번호(반복)',
        'remember': '계정 기억',
        'old_look': '기존 아이템 모습(기존 프레임 및 배경)',
        'inventory_vfx': '인벤토리 VFX 수정(이 세션)',
        'err_inventory_vfx': '인벤토리 VFX 수정 사항은 이 클라이언트 빌드를 지원하지 않습니다.',
        'play_saved': '플레이',
        'login': '로그인',
        'register': '계정 만들기',
        'connecting': '연결 중...',
        'to_register': '아직 계정이 없나요? 하나 만들기',
        'to_login': '이미 계정이 있습니까? 로그인',
        'version': 'DSOR • 커뮤니티 서버',
        'err_fields': '사용자 이름과 비밀번호가 필요합니다.',
        'err_pw_match': '비밀번호가 일치하지 않습니다.',
        'status_creating': '계정 생성 중...',
        'status_created': '계정이 생성되었습니다! 로그인 중...',
        'status_connecting': '서버에 연결 중...',
        'status_launching': '게임 실행 중...',
        'err_conn': '서버에 연결할 수 없습니다. 서버 주소와 연결을 확인한 후 다시 시도하세요.',
        'err_no_client': 'dro_client64.exe를 찾을 수 없습니다! \n'
                         ' Client\\dlcache 또는 DSOClient\\dlcache 폴더를 확인하세요.',
        'err_launch': '로그인 실패(HTTP {code})',
        'err_register': '등록 실패(HTTP {code})',
        'language': '언어',
        'feed_title': 'DSOR_SERVER • MAIN의 최신 커밋',
        'feed_hint': '커밋을 두 번 클릭하여 GitHub에서 엽니다.',
        'feed_loading': 'main에서 커밋 로드 중…',
        'feed_empty': '표시할 커밋이 없습니다.',
        'feed_error': 'GitHub에 연결할 수 없습니다: {error}',
        'update_checking': 'GitHub 서버 개정판을 확인하는 중…',
        'update_check': '업데이트 확인',
        'update_checking_button': '확인 중…',
        'update_check_failed': '업데이트 확인 실패: {error}',
        'update_retry': '재시도',
        'update_disconnected': '서버 체크아웃이 연결되지 않음 • GitHub main: {sha}',
        'open_repo': '오픈 저장소',
        'up_to_date': '서버 파일이 최신 상태입니다. • {sha}',
        'up_to_date_button': '최신 상태',
        'update_available': '서버 업데이트 가능 • {local} → {remote}',
        'update_button': '업데이트',
        'update_title': '서버 업데이트',
        'update_confirm': '최신 DSOR_Server main 파일이 GitHub에서 다운로드됩니다.\n'
                          '\n'
                          '커밋: {sha}\n'
                          '{message}\n'
                          '\n'
                          'Git은 선택사항입니다. 깔끔한 Git 체크아웃은 빨리 감깁니다. Git 없이 설치하면 GitHub 아카이브를 설치합니다.',
        'update_running': 'GitHub에서 서버 파일을 가져오는 중…',
        'update_running_button': '업데이트 중…',
        'update_done': '서버 업데이트됨 • {sha} • 변경 사항을 적용하려면 서버를 다시 시작하세요.',
        'update_done_title': '업데이트 완료',
        'update_done_message': '서버 파일이 업데이트되었습니다. 서버를 다시 시작한 다음 실행 프로그램을 닫았다가 다시 엽니다.',
        'update_failed': '업데이트를 적용할 수 없음: {error}',
        'update_retry_check': '다시 확인하세요',
        'settings': '설정',
        'play': '플레이',
        'settings_title': '실행기 설정',
        'client_folder': '게임 클라이언트 폴더',
        'browse': '찾아보기…',
        'save_settings': '설정 저장',
        'test_connection': '테스트 연결',
        'testing_connection': '서버 연결 테스트 중…',
        'connection_ok': '서버 연결에 성공했습니다.',
        'connection_failed': '서버에 연결할 수 없습니다: {error}',
        'client_path_required': '먼저 게임 클라이언트 폴더를 선택하세요.',
        'client_path_invalid': '선택한 폴더에 유효한 DSO 클라이언트가 포함되어 있지 않습니다.',
        'unpack_client': '클라이언트 압축 풀기',
        'checking_client': '클라이언트 다운로드 확인 중…',
        'client_not_complete': '클라이언트 파일이 불완전하거나 확인할 수 없습니다: {error}',
        'unpack_confirm': '클라이언트 번들이 검증되었습니다({bundles} 번들). 선택한 게임 폴더에 압축이 풀리면 현재 서버 자산이 data/의 이전 '
                          '버전을 대체합니다. 약 7GB의 추가 공간이 필요할 수 있습니다. 클라이언트 dlcache 인덱스는 교체되기 전에 백업됩니다. '
                          '기존 data/quests.xml도 업데이트됩니다. 계속하다?\\n\\n계속하기 전에 서버를 중지하세요.\n'
                          '\n'
                          '이 캐시에 없는 이전 지도, 내비게이션 및 언어 파일은 제거됩니다.',
        'unpack_running': '클라이언트 번들 압축을 풀고 서버 데이터를 새로 고치는 중...',
        'unpack_progress': '{line}',
        'unpack_done': '클라이언트 압축해제 및 서버 데이터 업데이트가 완료되었습니다. 플레이하기 전에 서버를 다시 시작하세요.',
        'unpack_failed': '클라이언트 압축 풀기 실패: {error}',
        'unpack_no_script': '런처 옆에 setup_client_assets.py가 없습니다.',
        'settings_saved': '설정이 저장되었습니다.'},
 'pl': {'subtitle': 'Program uruchamiający społeczność DSOR',
        'server_ip': 'Adres IP serwera',
        'game_language': 'Język gry',
        'err_ip': 'Wprowadź prawidłowy adres IP serwera.',
        'err_language': 'Wybrany język gry nie jest obsługiwany.',
        'username': 'Nazwa użytkownika',
        'password': 'Hasło',
        'password2': 'Hasło (powtórz)',
        'remember': 'Zapamiętaj konto',
        'old_look': 'Stary wygląd przedmiotu (stare ramki i tła)',
        'inventory_vfx': 'Poprawka efektów wizualnych ekwipunku (w tej sesji)',
        'err_inventory_vfx': 'Poprawka efektów wizualnych wyposażenia nie obsługuje tej kompilacji '
                             'klienta.',
        'play_saved': 'GRAJ',
        'login': 'ZALOGUJ SIĘ',
        'register': 'UTWÓRZ KONTO',
        'connecting': 'ŁĄCZENIE...',
        'to_register': 'Nie masz jeszcze konta? Utwórz jeden',
        'to_login': 'Masz już konto? Zaloguj się',
        'version': 'DSOR • Serwer społeczności',
        'err_fields': 'Wymagana jest nazwa użytkownika i hasło.',
        'err_pw_match': 'Hasła nie pasują.',
        'status_creating': 'Tworzenie konta...',
        'status_created': 'Konto utworzone! Logowanie...',
        'status_connecting': 'Łączenie z serwerem...',
        'status_launching': 'Uruchamianie gry...',
        'err_conn': 'Nie można połączyć się z serwerem. Sprawdź adres serwera i połączenie, a '
                    'następnie spróbuj ponownie.',
        'err_no_client': 'Nie znaleziono dro_client64.exe! \n'
                         ' Sprawdź folder Client\\dlcache lub DSOClient\\dlcache.',
        'err_launch': 'Logowanie nie powiodło się (HTTP {code})',
        'err_register': 'Rejestracja nie powiodła się (HTTP {code})',
        'language': 'Język',
        'feed_title': 'DSOR_SERVER • OSTATNI COMMIT NA MAIN',
        'feed_hint': 'Kliknij dwukrotnie zatwierdzenie, aby otworzyć je w GitHub.',
        'feed_loading': 'Ładowanie zatwierdzeń z main…',
        'feed_empty': 'Brak zatwierdzeń do pokazania.',
        'feed_error': 'Nie udało się połączyć z GitHub: {error}',
        'update_checking': 'Sprawdzanie wersji serwera GitHub…',
        'update_check': 'SPRAWDŹ AKTUALIZACJE',
        'update_checking_button': 'SPRAWDZANIE…',
        'update_check_failed': 'Sprawdzenie aktualizacji nie powiodło się: {error}',
        'update_retry': 'PRÓBUJ PONOWNIE',
        'update_disconnected': 'Pobranie serwera nie jest połączone • GitHub main: {sha}',
        'open_repo': 'OTWÓRZ REPOZYTORIUM',
        'up_to_date': 'Pliki serwera są aktualne • {sha}',
        'up_to_date_button': 'AKTUALNE',
        'update_available': 'Dostępna aktualizacja serwera • {local} → {remote}',
        'update_button': 'AKTUALIZACJA',
        'update_title': 'Aktualizacja serwera',
        'update_confirm': 'Najnowsze pliki DSOR_Server main zostaną pobrane z GitHub.\n'
                          '\n'
                          'Zatwierdź: {sha}\n'
                          '{message}\n'
                          '\n'
                          'Git jest opcjonalny. Czysta transakcja Git jest przesyłana szybko do '
                          'przodu; instalacje bez Git zainstaluj archiwum GitHub.',
        'update_running': 'Pobieranie plików serwera z GitHub…',
        'update_running_button': 'AKTUALIZACJA…',
        'update_done': 'Serwer zaktualizowany • {sha} • Uruchom ponownie serwer, aby zastosować '
                       'zmiany.',
        'update_done_title': 'Aktualizacja zakończona',
        'update_done_message': 'Pliki serwera zostały zaktualizowane. Uruchom ponownie serwer, a '
                               'następnie zamknij i otwórz ponownie program uruchamiający.',
        'update_failed': 'Nie można zastosować aktualizacji: {error}',
        'update_retry_check': 'SPRAWDŹ PONOWNIE',
        'settings': 'USTAWIENIA',
        'play': 'GRAJ',
        'settings_title': 'USTAWIENIA URUCHOMIENIA',
        'client_folder': 'Folder klienta gry',
        'browse': 'PRZEGLĄDAJ…',
        'save_settings': 'ZAPISZ USTAWIENIA',
        'test_connection': 'POŁĄCZENIE TESTOWE',
        'testing_connection': 'Testowanie połączenia z serwerem…',
        'connection_ok': 'Połączenie z serwerem powiodło się.',
        'connection_failed': 'Nie można połączyć się z serwerem: {error}',
        'client_path_required': 'Wybierz najpierw folder klienta gry.',
        'client_path_invalid': 'Wybrany folder nie zawiera prawidłowego klienta DSO.',
        'unpack_client': 'ROZPAKUJ KLIENTA',
        'checking_client': 'Sprawdzam pobieranie klienta…',
        'client_not_complete': 'Pliki klienta są niekompletne lub nie można ich zweryfikować: '
                               '{error}',
        'unpack_confirm': 'Pakiety klienta zostały zweryfikowane (pakiety {bundles}). Zostaną '
                          'rozpakowane do wybranego folderu gry, następnie aktualne zasoby serwera '
                          'zastąpią ich starsze wersje w data/. Może to zająć około 7 GB '
                          'dodatkowej przestrzeni. Przed zastąpieniem tworzona jest kopia zapasowa '
                          'indeksu klienta dlcache. Istniejący plik data/quests.xml również '
                          'zostanie zaktualizowany. Kontynuować?\\n\\nZatrzymaj serwer przed '
                          'kontynuowaniem.\n'
                          '\n'
                          'Stare mapy oraz pliki nawigacji i językowe nieobecne w tej pamięci '
                          'podręcznej zostaną usunięte.',
        'unpack_running': 'Rozpakowywanie pakietów klientów i odświeżanie danych serwera…',
        'unpack_progress': '{line}',
        'unpack_done': 'Zakończono rozpakowywanie klienta i aktualizację danych serwera. '
                       'Zrestartuj serwer przed rozpoczęciem gry.',
        'unpack_failed': 'Rozpakowanie klienta nie powiodło się: {error}',
        'unpack_no_script': 'setup_client_assets.py nie został znaleziony obok programu '
                            'uruchamiającego.',
        'settings_saved': 'Ustawienia zapisane.'},
 'pt': {'subtitle': 'Lançador da comunidade DSOR',
        'server_ip': 'Endereço IP do servidor',
        'game_language': 'Linguagem do Jogo',
        'err_ip': 'Insira um endereço IP de servidor válido.',
        'err_language': 'O idioma do jogo selecionado não é suportado.',
        'username': 'Nome de usuário',
        'password': 'Senha',
        'password2': 'Senha (repetir)',
        'remember': 'Lembrar conta',
        'old_look': 'Aparência de item antigo (molduras e planos de fundo antigos)',
        'inventory_vfx': 'Correção de efeitos visuais do inventário (esta sessão)',
        'err_inventory_vfx': 'A correção de efeitos visuais do inventário não suporta esta '
                             'compilação de cliente.',
        'play_saved': 'JOGAR',
        'login': 'ENTRAR',
        'register': 'CRIAR CONTA',
        'connecting': 'CONECTANDO...',
        'to_register': 'Ainda não tem conta? Crie um',
        'to_login': 'Já possui uma conta? Faça login',
        'version': 'DSOR • Servidor da comunidade',
        'err_fields': 'Nome de usuário e senha são obrigatórios.',
        'err_pw_match': 'As senhas não coincidem.',
        'status_creating': 'Criando conta...',
        'status_created': 'Conta criada! Fazendo login...',
        'status_connecting': 'Conectando ao servidor...',
        'status_launching': 'Iniciando jogo...',
        'err_conn': 'Não foi possível conectar ao servidor. Verifique o endereço do servidor e sua '
                    'conexão e tente novamente.',
        'err_no_client': 'dro_client64.exe não encontrado! \n'
                         ' Verifique a pasta Client\\dlcache ou DSOClient\\dlcache.',
        'err_launch': 'Falha no login (HTTP {code})',
        'err_register': 'Falha no registro (HTTP {code})',
        'language': 'Idioma',
        'feed_title': 'DSOR_SERVER • ÚLTIMO COMMIT EM MAIN',
        'feed_hint': 'Clique duas vezes em um commit para abri-lo no GitHub.',
        'feed_loading': 'Carregando commits de main…',
        'feed_empty': 'Não há commits para mostrar.',
        'feed_error': 'Não foi possível acessar GitHub: {error}',
        'update_checking': 'Verificando a revisão do servidor GitHub…',
        'update_check': 'VERIFICAR ATUALIZAÇÕES',
        'update_checking_button': 'VERIFICANDO…',
        'update_check_failed': 'Falha na verificação de atualização: {error}',
        'update_retry': 'TENTAR NOVAMENTE',
        'update_disconnected': 'Checkout do servidor não vinculado • GitHub main: {sha}',
        'open_repo': 'REPOSITÓRIO ABERTO',
        'up_to_date': 'Os arquivos do servidor estão atualizados • {sha}',
        'up_to_date_button': 'ATUALIZADO',
        'update_available': 'Atualização do servidor disponível • {local} → {remote}',
        'update_button': 'ATUALIZAÇÃO',
        'update_title': 'Atualização do servidor',
        'update_confirm': 'Os arquivos DSOR_Server main mais recentes serão baixados do GitHub.\n'
                          '\n'
                          'Confirmar: {sha}\n'
                          '{message}\n'
                          '\n'
                          'Git é opcional. Uma verificação limpa do Git é avançada; instalações '
                          'sem Git instalam o arquivo GitHub.',
        'update_running': 'Buscando arquivos do servidor de GitHub…',
        'update_running_button': 'ATUALIZANDO…',
        'update_done': 'Servidor atualizado • {sha} • Reinicie o servidor para aplicar as '
                       'alterações.',
        'update_done_title': 'Atualização concluída',
        'update_done_message': 'Os arquivos do servidor estão atualizados. Reinicie o servidor, '
                               'feche e reabra o inicializador.',
        'update_failed': 'Não foi possível aplicar a atualização: {error}',
        'update_retry_check': 'VERIFICAR NOVAMENTE',
        'settings': 'CONFIGURAÇÕES',
        'play': 'JOGAR',
        'settings_title': 'CONFIGURAÇÕES DO LANÇADOR',
        'client_folder': 'Pasta do cliente do jogo',
        'browse': 'NAVEGAR…',
        'save_settings': 'SALVAR CONFIGURAÇÕES',
        'test_connection': 'CONEXÃO DE TESTE',
        'testing_connection': 'Testando conexão do servidor…',
        'connection_ok': 'Conexão ao servidor bem sucedida.',
        'connection_failed': 'Não foi possível conectar ao servidor: {error}',
        'client_path_required': 'Selecione primeiro a pasta do cliente do jogo.',
        'client_path_invalid': 'A pasta selecionada não contém um cliente DSO válido.',
        'unpack_client': 'DESEMBALAR CLIENTE',
        'checking_client': 'Verificando download do cliente…',
        'client_not_complete': 'Os arquivos do cliente estão incompletos ou não puderam ser '
                               'verificados: {error}',
        'unpack_confirm': 'Os pacotes do cliente foram verificados (pacotes {bundles}). Eles serão '
                          'descompactados na pasta do jogo selecionada e os ativos atuais do '
                          'servidor substituirão suas versões mais antigas em data/. Isso pode '
                          'consumir cerca de 7 GB de espaço adicional. É feito backup do índice '
                          'dlcache do cliente antes de ser substituído. O data/quests.xml '
                          'existente também será atualizado. Continuar?\\n\\nPare o servidor antes '
                          'de continuar.\n'
                          '\n'
                          'Mapas e arquivos antigos de navegação e idioma ausentes deste cache '
                          'serão removidos.',
        'unpack_running': 'Descompactando pacotes de clientes e atualizando dados do servidor…',
        'unpack_progress': '{line}',
        'unpack_done': 'Descompactação do cliente e atualização dos dados do servidor concluídas. '
                       'Reinicie o servidor antes de jogar.',
        'unpack_failed': 'Falha na descompactação do cliente: {error}',
        'unpack_no_script': 'setup_client_assets.py não foi encontrado ao lado do inicializador.',
        'settings_saved': 'Configurações salvas.'},
 'ro': {'subtitle': 'Lansatorul comunității DSOR',
        'server_ip': 'Adresa IP a serverului',
        'game_language': 'limbajul jocului',
        'err_ip': 'Introduceți o adresă IP validă a serverului.',
        'err_language': 'Limba de joc selectată nu este acceptată.',
        'username': 'Nume de utilizator',
        'password': 'Parolă',
        'password2': 'Parola (repetată)',
        'remember': 'Amintiți-vă contul',
        'old_look': 'aspect vechi al articolului (cadre și fundaluri vechi)',
        'inventory_vfx': 'Remediere VFX a inventarului (această sesiune)',
        'err_inventory_vfx': 'Remedierea VFX a inventarului nu acceptă această versiune client.',
        'play_saved': 'JOCARE',
        'login': 'CONECTARE',
        'register': 'CREAȚI CONT',
        'connecting': 'CONECTARE...',
        'to_register': 'Nu există încă un cont? Creați unul',
        'to_login': 'Aveți deja un cont? Conectați-vă',
        'version': 'DSOR • Server comunitar',
        'err_fields': 'Numele de utilizator și parola sunt necesare.',
        'err_pw_match': 'Parolele nu se potrivesc.',
        'status_creating': 'Se creează contul...',
        'status_created': 'Cont creat! Conectare...',
        'status_connecting': 'Se conectează la server...',
        'status_launching': 'Se lansează jocul...',
        'err_conn': 'Nu s-a putut conecta la server. Verificați adresa serverului și conexiunea '
                    'dvs., apoi încercați din nou.',
        'err_no_client': 'dro_client64.exe nu a fost găsit! \n'
                         ' Verificați folderul Client\\dlcache sau DSOClient\\dlcache.',
        'err_launch': 'Conectarea eșuată (HTTP {code})',
        'err_register': 'Înregistrarea eșuată (HTTP {code})',
        'language': 'Limbă',
        'feed_title': 'DSOR_SERVER • ULTIMUL COMMIT PE MAIN',
        'feed_hint': 'Faceți dublu clic pe un commit pentru a-l deschide pe GitHub.',
        'feed_loading': 'Se încarcă comiterile de la main...',
        'feed_empty': 'Nicio comitere de afișat.',
        'feed_error': 'Nu s-a putut accesa GitHub: {error}',
        'update_checking': 'Se verifică versiunea serverului GitHub...',
        'update_check': 'VERIFICAȚI ACTUALIZĂRI',
        'update_checking_button': 'SE VERIFICA...',
        'update_check_failed': 'Verificarea actualizării a eșuat: {error}',
        'update_retry': 'REÎNCERCAȚI',
        'update_disconnected': 'Procesarea serverului nu este conectată • GitHub main: {sha}',
        'open_repo': 'DEPOZIT DESCHIS',
        'up_to_date': 'Fișierele serverului sunt actualizate • {sha}',
        'up_to_date_button': 'ACTUALIZĂ',
        'update_available': 'Actualizare server disponibilă • {local} → {remote}',
        'update_button': 'ACTUALIZARE',
        'update_title': 'Actualizare server',
        'update_confirm': 'Cele mai recente fișiere DSOR_Server main vor fi descărcate de pe '
                          'GitHub.\n'
                          '\n'
                          'Se angajează: {sha}\n'
                          '{message}\n'
                          '\n'
                          'Git este opțional. O finalizare curată Git este redirecționată rapid; '
                          'instalările fără Git instalează arhiva GitHub.',
        'update_running': 'Se preiau fișiere server de la GitHub...',
        'update_running_button': 'ACTUALIZARE...',
        'update_done': 'Server actualizat • {sha} • Reporniți serverul pentru a aplica '
                       'modificările.',
        'update_done_title': 'Actualizare finalizată',
        'update_done_message': 'Fișierele serverului sunt actualizate. Reporniți serverul, apoi '
                               'închideți și redeschideți lansatorul.',
        'update_failed': 'Nu s-a putut aplica actualizarea: {error}',
        'update_retry_check': 'VERIFICAȚI DIN NOU',
        'settings': 'SETĂRI',
        'play': 'JOCARE',
        'settings_title': 'SETĂRI LAUNCATOR',
        'client_folder': 'Dosarul client al jocului',
        'browse': 'RAUȚIȚI...',
        'save_settings': 'SALVARE SETĂRI',
        'test_connection': 'TESTARE CONEXIUNE',
        'testing_connection': 'Testarea conexiunii la server...',
        'connection_ok': 'Conexiunea la server a reușit.',
        'connection_failed': 'Nu s-a putut conecta la server: {error}',
        'client_path_required': 'Selectați mai întâi folderul client de joc.',
        'client_path_invalid': 'Dosarul selectat nu conține un client DSO valid.',
        'unpack_client': 'DESPACHET CLIENT',
        'checking_client': 'Se verifică descărcarea clientului...',
        'client_not_complete': 'Fișierele client sunt incomplete sau nu au putut fi verificate: '
                               '{error}',
        'unpack_confirm': 'Pachetele de clienți au fost verificate (pachetele {bundles}). Acestea '
                          'vor fi despachetate în folderul de joc selectat, apoi activele actuale '
                          'ale serverului vor înlocui versiunile lor mai vechi în data/. Acest '
                          'lucru poate folosi aproximativ 7 GB de spațiu suplimentar. Indexul '
                          'client dlcache este copiat de rezervă înainte de a fi înlocuit. '
                          'data/quests.xml existent va fi, de asemenea, actualizat. '
                          'Continua?\\n\\nOprește serverul înainte de a continua.\n'
                          '\n'
                          'Hărțile și fișierele vechi de navigare și limbă care lipsesc din acest '
                          'cache vor fi eliminate.',
        'unpack_running': 'Se despachetează pachetele de clienți și se reîmprospătează datele '
                          'serverului...',
        'unpack_progress': '{line}',
        'unpack_done': 'Dezambalarea clientului și actualizarea datelor serverului au fost '
                       'finalizate. Reporniți serverul înainte de a juca.',
        'unpack_failed': 'Dezambalarea clientului a eșuat: {error}',
        'unpack_no_script': 'setup_client_assets.py nu a fost găsit lângă lansator.',
        'settings_saved': 'Setările au fost salvate.'},
 'ru': {'subtitle': 'Программа запуска сообщества DSOR',
        'server_ip': 'IP-адрес сервера',
        'game_language': 'Язык игры',
        'err_ip': 'Введите действительный IP-адрес сервера.',
        'err_language': 'Выбранный язык игры не поддерживается.',
        'username': 'Имя пользователя',
        'password': 'Пароль',
        'password2': 'Пароль (повторить)',
        'remember': 'Запомнить учетную запись',
        'old_look': 'Старый внешний вид предмета (старые рамки и фон)',
        'inventory_vfx': 'Исправление визуальных эффектов инвентаря (эта сессия)',
        'err_inventory_vfx': 'Исправление визуальных эффектов инвентаря не поддерживает эту сборку '
                             'клиента.',
        'play_saved': 'ИГРАТЬ',
        'login': 'ВОЙТИ',
        'register': 'СОЗДАТЬ АККАУНТ',
        'connecting': 'ПОДКЛЮЧЕНИЕ...',
        'to_register': 'Еще нет учетной записи? Создать один',
        'to_login': 'У вас уже есть учетная запись? Войти',
        'version': 'DSOR • Сервер сообщества',
        'err_fields': 'Требуется имя пользователя и пароль.',
        'err_pw_match': 'Пароли не совпадают.',
        'status_creating': 'Создание учетной записи...',
        'status_created': 'Аккаунт создан! Вход в систему...',
        'status_connecting': 'Подключение к серверу...',
        'status_launching': 'Запуск игры...',
        'err_conn': 'Не удалось подключиться к серверу. Проверьте адрес сервера и ваше соединение, '
                    'затем повторите попытку.',
        'err_no_client': 'dro_client64.exe не \u200b\u200bнайден! \n'
                         ' Проверьте папку Client\\dlcache или DSOClient\\dlcache.',
        'err_launch': 'Не удалось войти в систему (HTTP {code})',
        'err_register': 'Регистрация не удалась (HTTP {code})',
        'language': 'Язык',
        'feed_title': 'DSOR_SERVER • ПОСЛЕДНИЙ КОММИТ В MAIN',
        'feed_hint': 'Дважды щелкните коммит, чтобы открыть его на GitHub.',
        'feed_loading': 'Загрузка коммитов из main…',
        'feed_empty': 'Никаких коммитов для отображения нет.',
        'feed_error': 'Не удалось связаться с GitHub: {error}',
        'update_checking': 'Проверка версии сервера GitHub…',
        'update_check': 'ПРОВЕРИТЬ ОБНОВЛЕНИЯ',
        'update_checking_button': 'ПРОВЕРКА…',
        'update_check_failed': 'Проверка обновления не удалась: {error}',
        'update_retry': 'ПОВТОРНАЯ ПОПЫТКА',
        'update_disconnected': 'Проверка сервера не связана • GitHub main: {sha}',
        'open_repo': 'ОТКРЫТЬ РЕПОЗИТАРИЙ',
        'up_to_date': 'Файлы сервера обновлены • {sha}',
        'up_to_date_button': 'АКТУАЛЬНО',
        'update_available': 'Доступно обновление сервера • {local} → {remote}',
        'update_button': 'ОБНОВЛЕНИЕ',
        'update_title': 'Обновление сервера',
        'update_confirm': 'Последние файлы DSOR_Server main будут загружены с GitHub.\n'
                          '\n'
                          'Зафиксировать: {sha}\n'
                          '{message}\n'
                          '\n'
                          'Git не является обязательным. Чистая проверка Git выполняется быстро; '
                          'установки без Git устанавливают архив GitHub.',
        'update_running': 'Получение файлов сервера из GitHub…',
        'update_running_button': 'ОБНОВЛЕНИЕ…',
        'update_done': 'Сервер обновлен • {sha} • Перезапустите сервер, чтобы применить изменения.',
        'update_done_title': 'Обновление завершено',
        'update_done_message': 'Файлы сервера обновлены. Перезапустите сервер, затем закройте и '
                               'снова откройте панель запуска.',
        'update_failed': 'Не удалось применить обновление: {error}',
        'update_retry_check': 'ПРОВЕРЬТЕ ЕЩЕ РАЗ',
        'settings': 'НАСТРОЙКИ',
        'play': 'ИГРАТЬ',
        'settings_title': 'НАСТРОЙКИ ЗАПУСКА',
        'client_folder': 'Папка клиента игры',
        'browse': 'ПРОСМОТР…',
        'save_settings': 'СОХРАНИТЬ НАСТРОЙКИ',
        'test_connection': 'ТЕСТОВОЕ СОЕДИНЕНИЕ',
        'testing_connection': 'Проверка подключения к серверу…',
        'connection_ok': 'Соединение с сервером выполнено.',
        'connection_failed': 'Не удалось подключиться к серверу: {error}',
        'client_path_required': 'Сначала выберите папку игрового клиента.',
        'client_path_invalid': 'Выбранная папка не содержит допустимого клиента DSO.',
        'unpack_client': 'РАСПАКОВАТЬ КЛИЕНТ',
        'checking_client': 'Проверка загрузки клиента…',
        'client_not_complete': 'Файлы клиента неполные или не могут быть проверены: {error}',
        'unpack_confirm': 'Клиентские пакеты проверены (пакеты {bundles}). Они будут распакованы в '
                          'выбранную папку с игрой, после чего текущие ресурсы сервера заменят их '
                          'старые версии в data/. Для этого может потребоваться около 7 ГБ '
                          'дополнительного пространства. Перед заменой индекса клиента dlcache '
                          'создается резервная копия. Существующий data/quests.xml также будет '
                          'обновлен. Продолжать?\\n\\nПеред продолжением остановите сервер.\n'
                          '\n'
                          'Старые карты, навигационные и языковые файлы, отсутствующие в этом '
                          'кеше, будут удалены.',
        'unpack_running': 'Распаковка клиентских пакетов и обновление данных сервера…',
        'unpack_progress': '{line}',
        'unpack_done': 'Распаковка клиента и обновление данных сервера завершены. Перезагрузите '
                       'сервер перед игрой.',
        'unpack_failed': 'Не удалось распаковать клиент: {error}',
        'unpack_no_script': 'setup_client_assets.py не найден рядом с лаунчером.',
        'settings_saved': 'Настройки сохранены.'},
 'sk': {'subtitle': 'Spúšťač komunity DSOR',
        'server_ip': 'IP adresa servera',
        'game_language': 'Jazyk hry',
        'err_ip': 'Zadajte platnú IP adresu servera.',
        'err_language': 'Zvolený jazyk hry nie je podporovaný.',
        'username': 'Používateľské meno',
        'password': 'heslo',
        'password2': 'Heslo (opakovať)',
        'remember': 'Zapamätať si účet',
        'old_look': 'Starý vzhľad položky (staré rámy a pozadia)',
        'inventory_vfx': 'Oprava VFX inventára (táto relácia)',
        'err_inventory_vfx': 'Oprava inventára VFX nepodporuje túto zostavu klienta.',
        'play_saved': 'HRAŤ',
        'login': 'PRIHLÁSIŤ SA',
        'register': 'VYTVORIŤ ÚČET',
        'connecting': 'PRIPOJENIE...',
        'to_register': 'Ešte nemáte účet? Vytvorte si ho',
        'to_login': 'Už máte účet? Prihláste sa',
        'version': 'DSOR • Komunitný server',
        'err_fields': 'Vyžaduje sa používateľské meno a heslo.',
        'err_pw_match': 'Heslá sa nezhodujú.',
        'status_creating': 'Vytvára sa účet...',
        'status_created': 'Účet vytvorený! Prihlasovanie...',
        'status_connecting': 'Pripája sa k serveru...',
        'status_launching': 'Spúšťa sa hra...',
        'err_conn': 'Nepodarilo sa pripojiť k serveru. Skontrolujte adresu servera a svoje '
                    'pripojenie a skúste to znova.',
        'err_no_client': 'dro_client64.exe sa nenašiel! \n'
                         ' Skontrolujte priečinok Client\\dlcache alebo DSOClient\\dlcache.',
        'err_launch': 'Prihlásenie zlyhalo (HTTP {code})',
        'err_register': 'Registrácia zlyhala (HTTP {code})',
        'language': 'Jazyk',
        'feed_title': 'DSOR_SERVER • POSLEDNÝ COMMIT NA MAIN',
        'feed_hint': 'Dvakrát kliknite na potvrdenie, aby ste ho otvorili na GitHub.',
        'feed_loading': 'Načítava sa potvrdenia zo main…',
        'feed_empty': 'Žiadne záväzky na zobrazenie.',
        'feed_error': 'Nepodarilo sa dosiahnuť GitHub: {error}',
        'update_checking': 'Kontroluje sa revízia servera GitHub...',
        'update_check': 'SKONTROLUJTE AKTUALIZÁCIE',
        'update_checking_button': 'KONTROLA...',
        'update_check_failed': 'Kontrola aktualizácie zlyhala: {error}',
        'update_retry': 'OPAKOVAŤ',
        'update_disconnected': 'Pokladňa na serveri nie je prepojená • GitHub main: {sha}',
        'open_repo': 'OTVORENÉ ÚLOŽISKO',
        'up_to_date': 'Súbory servera sú aktuálne • {sha}',
        'up_to_date_button': 'AKTUÁLNE',
        'update_available': 'Dostupná aktualizácia servera • {local} → {remote}',
        'update_button': 'AKTUALIZÁCIA',
        'update_title': 'Aktualizácia servera',
        'update_confirm': 'Najnovšie súbory DSOR_Server main budú stiahnuté zo GitHub.\n'
                          '\n'
                          'Záväzok: {sha}\n'
                          '{message}\n'
                          '\n'
                          'Git je voliteľný. Čistá pokladňa Git sa rýchlo posúva dopredu; '
                          'inštalácie bez Gitu nainštalujte archív GitHub.',
        'update_running': 'Načítavajú sa serverové súbory zo GitHub…',
        'update_running_button': 'AKTUALIZUJE SA…',
        'update_done': 'Server aktualizovaný • {sha} • Reštartujte server, aby sa zmeny prejavili.',
        'update_done_title': 'Aktualizácia dokončená',
        'update_done_message': 'Súbory servera sú aktualizované. Reštartujte server, potom '
                               'zatvorte a znova otvorte spúšťač.',
        'update_failed': 'Nepodarilo sa použiť aktualizáciu: {error}',
        'update_retry_check': 'ZNOVU SKONTROLUJTE',
        'settings': 'NASTAVENIA',
        'play': 'PLAY',
        'settings_title': 'NASTAVENIA SPÚŠŤAČA',
        'client_folder': 'Priečinok herného klienta',
        'browse': 'PREHĽADÁVAŤ…',
        'save_settings': 'ULOŽIŤ NASTAVENIA',
        'test_connection': 'SKÚŠOBNÉ PRIPOJENIE',
        'testing_connection': 'Testuje sa pripojenie k serveru…',
        'connection_ok': 'Pripojenie k serveru bolo úspešné.',
        'connection_failed': 'Nepodarilo sa pripojiť k serveru: {error}',
        'client_path_required': 'Najprv vyberte priečinok herného klienta.',
        'client_path_invalid': 'Zvolený priečinok neobsahuje platného klienta DSO.',
        'unpack_client': 'ROZBALENIE KLIENTA',
        'checking_client': 'Kontroluje sa stiahnutie klienta...',
        'client_not_complete': 'Klientske súbory sú neúplné alebo ich nebolo možné overiť: {error}',
        'unpack_confirm': 'Klientske balíky boli overené (balíky {bundles}). Rozbalia sa do '
                          'vybratého priečinka s hrou a potom súčasné aktíva servera nahradia ich '
                          'staršie verzie v data/. To môže použiť približne 7 GB dodatočného '
                          'priestoru. Klientsky index dlcache sa pred nahradením zálohuje. '
                          'Existujúci súbor data/quests.xml bude tiež aktualizovaný. '
                          'Pokračovať?\\n\\nPred pokračovaním zastavte server.\n'
                          '\n'
                          'Staré mapy a navigačné a jazykové súbory, ktoré v tejto vyrovnávacej '
                          'pamäti chýbajú, sa odstránia.',
        'unpack_running': 'Rozbaľujú sa balíky klientov a obnovujú sa údaje servera…',
        'unpack_progress': '{line}',
        'unpack_done': 'Rozbalenie klienta a aktualizácia údajov servera dokončená. Pred hraním '
                       'reštartujte server.',
        'unpack_failed': 'Rozbalenie klienta zlyhalo: {error}',
        'unpack_no_script': 'setup_client_assets.py sa nenašiel vedľa spúšťača.',
        'settings_saved': 'Nastavenia boli uložené.'},
 'tw': {'subtitle': 'DSOR Mpɔtam Hɔ Nnwumayɛfoɔ',
        'server_ip': 'Server IP Address',
        'game_language': 'Agodie Kasa',
        'err_ip': 'Hyehyɛ server IP address a ɛfata.',
        'err_language': 'Wɔnnye agodie kasa a wɔapaw no ntom.',
        'username': 'Ɔdefoɔ din',
        'password': 'Asɛmfua a wɔde hyɛ mu',
        'password2': 'Asɛmfua (san yɛ bio) .',
        'remember': 'Kae akontaabu',
        'old_look': 'Ade dedaw hwɛ (frama dedaw ne akyi mfonini) .',
        'inventory_vfx': 'Inventory VFX siesie (nhyiam yi) .',
        'err_inventory_vfx': 'Inventory VFX fix no ntumi mmoa saa client build yi.',
        'play_saved': 'AGYINATUO',
        'login': 'NKYEKYƐMU MU',
        'register': 'WƆYƐ AKWANKYERƐ',
        'connecting': 'NKYERƐKYERƐMU...',
        'to_register': 'Akontaabu biara nni hɔ de besi nnɛ? Bɔ biako',
        'to_login': 'Wowɔ akontaabu dedaw? Fa wo nsa hyɛ mu',
        'version': 'DSOR • Mpɔtam hɔ Somfo',
        'err_fields': 'Ɔdefoɔ din ne asɛmfua a wɔde hyɛ mu no ho hia.',
        'err_pw_match': 'Asɛmfua a wɔde hyɛ mu no nhyia.',
        'status_creating': 'Wɔreyɛ akontaabu...',
        'status_created': 'Wɔabɔ akontaabu! Wode wo nsa hyɛ ase...',
        'status_connecting': 'Nkitahodi a ɛkɔ server so...',
        'status_launching': 'Wɔrefi ase agodie...',
        'err_conn': 'Entumi nkɔ server no so. Hwɛ server address ne wo connection no, afei sɔ hwɛ '
                    'bio.',
        'err_no_client': 'dro_client64.exe wɔanhu! \n'
                         ' Hwɛ Client\\dlcache anaa DSOClient\\dlcache folda no mu.',
        'err_launch': 'Nkyerɛwde no adi nkogu (HTTP {code}) .',
        'err_register': 'Nkyerɛwde no adi nkogu (HTTP {code}) .',
        'language': 'Kasa a wɔka',
        'feed_title': 'DSOR_SERVER • LAST COMMIT ON MAIN',
        'feed_hint': 'Klik commit bi so mprenu na bue no wɔ GitHub so.',
        'feed_loading': 'Wɔrefa commits afiri main...',
        'feed_empty': 'No commits sɛ ɛbɛkyerɛ.',
        'feed_error': 'Entumi nkɔ GitHub: {error}',
        'update_checking': 'Worehwɛ GitHub somfo nsakrae no...',
        'update_check': 'HWEHWƐ NSƐM A ƐFA NSƐM HO',
        'update_checking_button': 'NHWEHWƐMU...',
        'update_check_failed': 'Nsakraeɛ nhwehwɛmu no adi nkogu: {error}',
        'update_retry': 'SAN SAN SƆHWƐ',
        'update_disconnected': 'Server checkout no nnya nkitahodi • GitHub main: {sha}',
        'open_repo': 'AKWANKYERƐ A WƆABUE',
        'up_to_date': 'Server fael ahorow no yɛ foforo • {sha}',
        'up_to_date_button': 'NKYEKYƐMU',
        'update_available': 'Server update a ɛwɔ hɔ • {local} → {remote}',
        'update_button': 'NKYERƐKYERƐMU',
        'update_title': 'Server no foforo',
        'update_confirm': 'Wɔbɛtwe DSOR_Server main fael a ɛtwa toɔ no afiri GitHub.\n'
                          '\n'
                          'Fa wo ho to so: {sha}\n'
                          '{message} na ɔkyerɛwee\n'
                          '\n'
                          'Git yɛ nea wobetumi apaw. Git checkout a ɛho tew no yɛ ntɛmntɛm; '
                          'instɔlehyɛn a Git nni mu no instɔl GitHub archive no.',
        'update_running': 'Woregye server fael afi GitHub...',
        'update_running_button': 'NKYERƐKYERƐMU...',
        'update_done': 'Server updated • {sha} • San hyɛ server no ase na fa nsakraeɛ no di dwuma.',
        'update_done_title': 'Nsakraeɛ awie',
        'update_done_message': 'Wɔayɛ server fael ahorow no foforo. San hyɛ server no ase, afei to '
                               'mu na san bue launcher no.',
        'update_failed': 'Entumi mfa update nni dwuma: {error}',
        'update_retry_check': 'HWEHWƐ BIARA',
        'settings': 'NHYEHYƐEƐ',
        'play': 'AGYINATUO',
        'settings_title': 'LAUNCHER NHYEHYƐEƐ',
        'client_folder': 'Game Client Folda',
        'browse': 'NKYEKYƐMU...',
        'save_settings': 'SAVE NHYEHYƐEƐ',
        'test_connection': 'SƆHWƐ NKYERƐKYERƐMU',
        'testing_connection': 'Wɔresɔ server nkitahodi ahwɛ...',
        'connection_ok': 'Nkitahodi a ɛkɔ server no so no dii nkonim.',
        'connection_failed': 'Entumi nkɔ server no so: {error}',
        'client_path_required': 'Paw agodie afɛfo folda no kan.',
        'client_path_invalid': 'Folda a wɔapaw no nni DSO afɛfo a ɛfata.',
        'unpack_client': 'UNPACK AKWANKYERƐFO',
        'checking_client': 'Worehwɛ sɛnea client download...',
        'client_not_complete': 'Client fael no nwie pɛyɛ anaasɛ wɔantumi annye antom: {error}',
        'unpack_confirm': 'Wɔagye akraman bundles no atom ({bundles} bundles). Wɔbɛyi wɔn akɔ game '
                          'folda a wɔapaw no mu, afei mprempren server agyapadeɛ bɛsesa wɔn dedaw '
                          'no wɔ data/ mu. Eyi betumi de bɛyɛ 7 GB ahunmu foforo adi dwuma. Wɔayɛ '
                          'client dlcache index no backup ansa na wɔde asi ananmu. Wɔbɛyɛ '
                          'data/quests.xml a ɛwɔ hɔ dedaw no nso foforo. Toa so?\\n\\n繼續前請先停止伺服器。\n'
                          '\n'
                          '此快取中不存在的舊地圖、導航和語言檔案將會移除。',
        'unpack_running': 'Worebue akraman bundles mu na wɔresan ayɛ server data...',
        'unpack_progress': '{line} na ɛwɔ hɔ',
        'unpack_done': 'Client unpack ne server data update awie. San hyɛ server no ase ansa na '
                       'woabɔ.',
        'unpack_failed': 'Client unpack no adi nkogu: {error}',
        'unpack_no_script': 'Wɔanhu setup_client_assets.py wɔ launcher no nkyɛn.',
        'settings_saved': '設定已儲存。'},
 'zh': {'subtitle': 'DSOR 社区启动器',
        'server_ip': '服务器 IP 地址',
        'game_language': '游戏语言',
        'err_ip': '输入有效的服务器 IP 地址。',
        'err_language': '不支持所选的游戏语言。',
        'username': '用户名',
        'password': '密码',
        'password2': '密码（重复）',
        'remember': '记住帐户',
        'old_look': '旧项目外观（旧框架和背景）',
        'inventory_vfx': '库存视觉特效修复（本次会议）',
        'err_inventory_vfx': '库存 VFX 修复不支持此客户端版本。',
        'play_saved': '玩',
        'login': '登录',
        'register': '创建帐户',
        'connecting': '正在连接...',
        'to_register': '还没有帐户？创建一个',
        'to_login': '已有帐户？登录',
        'version': 'DSOR • 社区服务器',
        'err_fields': '需要用户名和密码。',
        'err_pw_match': '密码不匹配。',
        'status_creating': '正在创建帐户...',
        'status_created': '帐户已创建！正在登录...',
        'status_connecting': '正在连接到服务器...',
        'status_launching': '正在启动游戏...',
        'err_conn': '无法连接到服务器。检查服务器地址和您的连接，然后重试。',
        'err_no_client': 'dro_client64.exe 未找到！ \n 检查 Client\\dlcache 或 DSOClient\\dlcache 文件夹。',
        'err_launch': '登录失败 (HTTP {code})',
        'err_register': '注册失败 (HTTP {code})',
        'language': '语言',
        'feed_title': 'DSOR_SERVER • main 分支上的最新提交',
        'feed_hint': '双击提交以在 GitHub 上打开它。',
        'feed_loading': '正在从 main 加载提交...',
        'feed_empty': '没有可显示的提交。',
        'feed_error': '无法到达 GitHub：{error}',
        'update_checking': '检查 GitHub 服务器版本...',
        'update_check': '检查更新',
        'update_checking_button': '正在检查……',
        'update_check_failed': '更新检查失败：{error}',
        'update_retry': '重试',
        'update_disconnected': '服务器结账未链接 • GitHub main：{sha}',
        'open_repo': '打开存储库',
        'up_to_date': '服务器文件是最新的 • {sha}',
        'up_to_date_button': '最新',
        'update_available': '服务器更新可用 • {local} → {remote}',
        'update_button': '更新',
        'update_title': '服务器更新',
        'update_confirm': '将从 GitHub 下载最新的 DSOR_Server main 文件。\n'
                          '\n'
                          '提交：{sha}\n'
                          '{message}\n'
                          '\n'
                          'Git 是可选的。干净的 Git 签出是快进的；没有 Git 的安装会安装 GitHub 存档。',
        'update_running': '正在从 GitHub 获取服务器文件...',
        'update_running_button': '更新中……',
        'update_done': '服务器已更新 • {sha} • 重新启动服务器以应用更改。',
        'update_done_title': '更新完成',
        'update_done_message': '服务器文件已更新。重新启动服务器，然后关闭并重新打开启动器。',
        'update_failed': '无法应用更新：{error}',
        'update_retry_check': '再次检查',
        'settings': '设置',
        'play': '开始',
        'settings_title': '启动器设置',
        'client_folder': '游戏客户端文件夹',
        'browse': '浏览...',
        'save_settings': '保存设置',
        'test_connection': '测试连接',
        'testing_connection': '测试服务器连接...',
        'connection_ok': '与服务器的连接成功。',
        'connection_failed': '无法连接到服务器：{error}',
        'client_path_required': '首先选择游戏客户端文件夹。',
        'client_path_invalid': '所选文件夹不包含有效的 DSO 客户端。',
        'unpack_client': '解压客户端',
        'checking_client': '正在检查客户端下载...',
        'client_not_complete': '客户端文件不完整或无法验证：{error}',
        'unpack_confirm': '客户端捆绑包已被验证（{bundles} 捆绑包）。它们将被解压到选定的游戏文件夹中，然后当前服务器资源将替换 data/ '
                          '中的旧版本。这可能会使用大约 7 GB 的额外空间。客户端 dlcache 索引在替换之前会进行备份。现有的 data/quests.xml '
                          '也将更新。继续？\\n\\n继续前请先停止服务器。\n'
                          '\n'
                          '此缓存中不存在的旧地图、导航和语言文件将被删除。',
        'unpack_running': '解压客户端包并刷新服务器数据......',
        'unpack_progress': '{line}',
        'unpack_done': '客户端解包和服务器数据更新完成。玩之前请重新启动服务器。',
        'unpack_failed': '客户端解包失败：{error}',
        'unpack_no_script': '在启动器旁边找不到 setup_client_assets.py。',
        'settings_saved': '设置已保存。'}}

SERVER_FOLDER_LABELS = {
    'ar': 'مجلد الخادم', 'bg': 'Папка на сървъра', 'cs': 'Složka serveru',
    'de': 'Serverordner', 'el': 'Φάκελος διακομιστή', 'en': 'Server Folder',
    'es': 'Carpeta del servidor', 'fa': 'پوشه سرور', 'fr': 'Dossier du serveur',
    'hu': 'Szervermappa', 'it': 'Cartella server', 'ko': '서버 폴더',
    'pl': 'Folder serwera', 'pt': 'Pasta do servidor', 'ro': 'Dosar server',
    'ru': 'Папка сервера', 'sk': 'Priečinok servera', 'tr': 'Sunucu klasörü',
    'tw': '伺服器資料夾', 'zh': '服务器文件夹',
}

SERVER_FOLDER_ERRORS = {
    'ar': 'اختر مجلد خادم DSOR صالحًا.',
    'bg': 'Изберете валидна папка на DSOR сървъра.',
    'cs': 'Vyberte platnou složku serveru DSOR.',
    'de': 'Wähle einen gültigen DSOR-Serverordner aus.',
    'el': 'Επιλέξτε έναν έγκυρο φάκελο διακομιστή DSOR.',
    'en': 'Select a valid DSOR server folder.',
    'es': 'Selecciona una carpeta válida del servidor DSOR.',
    'fa': 'یک پوشه معتبر برای سرور DSOR انتخاب کنید.',
    'fr': 'Sélectionnez un dossier de serveur DSOR valide.',
    'hu': 'Válassz érvényes DSOR-szervermappát.',
    'it': 'Seleziona una cartella server DSOR valida.',
    'ko': '올바른 DSOR 서버 폴더를 선택하세요.',
    'pl': 'Wybierz prawidłowy folder serwera DSOR.',
    'pt': 'Selecione uma pasta de servidor DSOR válida.',
    'ro': 'Selectați un folder valid pentru serverul DSOR.',
    'ru': 'Выберите папку действующего сервера DSOR.',
    'sk': 'Vyberte platný priečinok servera DSOR.',
    'tr': 'Geçerli bir DSOR sunucu klasörü seç.',
    'tw': '請選擇有效的 DSOR 伺服器資料夾。',
    'zh': '请选择有效的 DSOR 服务器文件夹。',
}

for _language_code, _strings in STRINGS.items():
    _strings['server_folder'] = SERVER_FOLDER_LABELS[_language_code]
    _strings['server_folder_invalid'] = SERVER_FOLDER_ERRORS[_language_code]


LANGUAGE_NAMES = {'ar': 'العربية',
 'bg': 'Български',
 'cs': 'Čeština',
 'de': 'Deutsch',
 'el': 'Ελληνικά',
 'en': 'English',
 'es': 'Español',
 'fa': 'فارسی',
 'fr': 'Français',
 'hu': 'Magyar',
 'it': 'Italiano',
 'ko': '한국어',
 'pl': 'Polski',
 'pt': 'Português',
 'ro': 'Română',
 'ru': 'Русский',
 'sk': 'Slovenčina',
 'tr': 'Türkçe',
 'tw': '繁體中文',
 'zh': '简体中文'}
