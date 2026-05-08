# Полное руководство: модификация APK видео-брелка

**Приложение:** `com.legend.smartwatch.electronicbadge.android`  
**Цель:** снять ограничение на длину загружаемого видео (5 сек → без лимита) и убрать принудительное снижение FPS (5 fps → 24 fps)

---

## Инструменты

| Инструмент | Путь | Для чего |
|---|---|---|
| `apktool.bat` | `D:\Programs\apktool\` | Декомпиляция и сборка APK |
| `adb.exe` | `D:\Programs\platform-tools\` | Работа с устройством |
| `keytool.exe` + `jarsigner.exe` | Eclipse Adoptium JDK 25 (в PATH) | Создание keystore и v1-подпись |
| `uber-apk-signer.jar` | `D:\Programs\apktool\work\` | v2+v3 подпись + zipalign |
| `python` | в PATH (3.14) | Кастомный zipalign.py |
| `zipalign.py` | `D:\Programs\apktool\work\` | Выравнивание ZIP-записей |

---

## Шаг 1 — Скачать APK с телефона

Телефон подключён по USB в режиме отладки (USB debugging включён в настройках разработчика).

```powershell
# Найти путь к APK на устройстве
adb shell pm path com.legend.smartwatch.electronicbadge.android
```

Вывод будет примерно такой (хэши в пути у каждого устройства свои):
```
package:/data/app/~~d9vg0B8ZnC4w3jVFxL9PzA==/com.legend.smartwatch.electronicbadge.android-zT3px78WGJpIj0Dr5Axzwg==/base.apk
package:/data/app/~~d9vg.../.../ split_config.arm64_v8a.apk
package:/data/app/~~d9vg.../.../ split_config.en.apk
package:/data/app/~~d9vg.../.../ split_config.ru.apk
package:/data/app/~~d9vg.../.../ split_config.xxhdpi.apk
```

Это **split APK** — приложение состоит из 5 файлов. Скачиваем все:

```powershell
$base = "/data/app/~~d9vg0B8ZnC4w3jVFxL9PzA==/com.legend.smartwatch.electronicbadge.android-zT3px78WGJpIj0Dr5Axzwg=="

adb pull "$base/base.apk"                       "D:\Programs\apktool\work\base.apk"
adb pull "$base/split_config.arm64_v8a.apk"     "D:\Programs\apktool\work\split_config.arm64_v8a.apk"
adb pull "$base/split_config.en.apk"            "D:\Programs\apktool\work\split_config.en.apk"
adb pull "$base/split_config.ru.apk"            "D:\Programs\apktool\work\split_config.ru.apk"
adb pull "$base/split_config.xxhdpi.apk"        "D:\Programs\apktool\work\split_config.xxhdpi.apk"
```

> **Важно:** split APK — это не просто несколько файлов. Это один пакет, разделённый на части:
> - `base.apk` — основной код и ресурсы
> - `split_config.arm64_v8a.apk` — нативные библиотеки для ARM64
> - `split_config.en/ru.apk` — строки на английском/русском
> - `split_config.xxhdpi.apk` — картинки для экранов высокой плотности
>
> Все 5 файлов должны быть установлены **одновременно** и **подписаны одним ключом**.

---

## Шаг 2 — Декомпилировать APK

```powershell
Set-Location "D:\Programs\apktool\work"
echo "" | apktool d base.apk -o app_decompiled --force
```

> **Почему `echo "" |`?**  
> apktool 3.0.2 в конце работы выводит "Press any key to continue..." и ждёт ввода.
> Если запустить без `echo ""`, процесс зависнет навсегда. `echo ""` автоматически "нажимает" Enter.

После этого появится папка `app_decompiled/` со структурой:
```
app_decompiled/
  AndroidManifest.xml       ← манифест приложения
  apktool.yml               ← мета-информация для сборки
  smali/                    ← основной код (Java → Dalvik bytecode → текст)
  smali_classes2/           ← второй dex-файл
  smali_classes3/           ← третий dex-файл  ← ТУТ лежит VideoCutActivity
  res/                      ← ресурсы (картинки, строки, xml)
  assets/                   ← ассеты приложения
```

---

## Шаг 3 — Найти нужный файл

Целевой файл:
```
app_decompiled/smali_classes3/xfkj/fitpro/ui/activities/device/electronicBadgeDevice/VideoCutActivity.smali
```

Smali — это текстовое представление байткода Dalvik (как ассемблер для Android).

---

## Шаг 4 — Внести изменения в VideoCutActivity.smali

### 4.1 — Лимит длины видео в конструкторе (строка 114)

**Было:**
```smali
const-wide/16 v4, 0x1388
iput-wide v4, p0, Lxfkj/.../VideoCutActivity;->j:J
```

**Стало:**
```smali
const-wide/32 v4, 0x36EE80
iput-wide v4, p0, Lxfkj/.../VideoCutActivity;->j:J
```

**Объяснение:**  
`0x1388` = 5000 мс = 5 секунд. Поле `j` — правая граница слайдера обрезки.  
`0x36EE80` = 3 600 000 мс = 60 минут.  
`const-wide/16` заменяется на `const-wide/32` потому что значение 0x36EE80 не помещается в 16-битный литерал.

---

### 4.2 — Убрать ограничение количества превью-фреймов (строки ~1280–1284)

**Было (4 строки):**
```smali
const/4 v3, 0x5

if-le v0, v3, :cond_0

move v0, v3

:cond_0
iput v0, p0, Lxfkj/.../VideoCutActivity;->d:I
```

**Стало (убраны 3 строки):**
```smali
:cond_0
iput v0, p0, Lxfkj/.../VideoCutActivity;->d:I
```

**Объяснение:**  
Этот блок ограничивал количество превью-кадров на слайдере максимум 5 штуками (1 кадр = 1 сек → max 5 секунд).  
Убираем `const/4 v3, 0x5`, `if-le v0, v3, :cond_0` и `move v0, v3` — остаётся только метка и сохранение реального значения.

---

### 4.3 — Второй лимит в методе J0() (строка ~1289)

**Было:**
```smali
const-wide/16 v3, 0x1388
iput-wide v3, p0, Lxfkj/.../VideoCutActivity;->j:J
```

**Стало:**
```smali
const-wide/32 v3, 0x36EE80
iput-wide v3, p0, Lxfkj/.../VideoCutActivity;->j:J
```

**Объяснение:**  
Метод `J0()` вызывается при загрузке GIF-версии контента и заново выставляет лимит 5 сек. Та же замена, что и в конструкторе.

---

### 4.4 — Добавить обновление лимита из реальной длины видео (метод N0)

Метод `N0(Landroid/media/MediaPlayer;)V` — это обработчик `onPrepared`, вызывается когда MediaPlayer загрузил видеофайл. В нём уже читались ширина и высота видео, но не длина. Добавляем обновление поля `j` сразу после сохранения высоты:

**Было (строка ~1469–1471):**
```smali
iput p1, p0, Lxfkj/.../VideoCutActivity;->y:I

new-instance p1, Ljava/lang/StringBuilder;
```

**Стало:**
```smali
iput p1, p0, Lxfkj/.../VideoCutActivity;->y:I

iget-object v0, p0, Lxfkj/.../VideoCutActivity;->c:Landroid/media/MediaPlayer;
invoke-virtual {v0}, Landroid/media/MediaPlayer;->getDuration()I
move-result v0
int-to-long v0, v0
iput-wide v0, p0, Lxfkj/.../VideoCutActivity;->j:J

new-instance p1, Ljava/lang/StringBuilder;
```

**Объяснение:**  
Поле `c` — это ссылка на MediaPlayer (была сохранена выше в том же методе).  
`getDuration()` возвращает int (миллисекунды).  
`int-to-long` конвертирует в long (поле `j` — тип `J` = long).  
`iput-wide` сохраняет 64-битное значение в поле.  
Это позволяет слайдеру знать реальную длину видео — без этого даже после увеличения лимита слайдер всё равно ограничен 5 секундами при первом открытии.

---

### 4.5 — FPS в FFmpeg-командах (4 места)

apptool нашёл 4 строки с `fps=5` и `-r 5`:

**Место 1 (~строка 2554) — ветка кадрирования с crop:**
```smali
# Было:
const-string v13, "crop=%d:%d:%d:%d,scale=%s:%s:...,fps=5"
# Стало:
const-string v13, "crop=%d:%d:%d:%d,scale=%s:%s:...,fps=24"
```

**Место 2 (~строка 2629) — ветка кадрирования scale+crop:**
```smali
# Было:
const-string v7, "scale=%s:%s:...,crop=%s:%s:...,fps=5"
# Стало:
const-string v7, "scale=%s:%s:...,crop=%s:%s:...,fps=24"
```

**Место 3 (~строка 2855) — FFmpeg-команда с кастомным vf-фильтром:**
```smali
# Было:  -r 5 ... -video_track_timescale 5 ...
# Стало: -r 24 ... -video_track_timescale 24 ...
```

**Место 4 (~строка 3052) — основная FFmpeg-команда:**
```smali
# Было:  fps=5" -r 5 ... -video_track_timescale 5 ...
# Стало: fps=24" -r 24 ... -video_track_timescale 24 ...
```

**Объяснение:**  
Приложение вызывает FFmpeg для конвертации видео в AVI/MJPEG перед отправкой на брелок.  
`fps=5` в `-vf` фильтре и `-r 5` вместе принудительно снижают частоту кадров до 5.  
`-video_track_timescale` задаёт временную шкалу дорожки — должен совпадать с `-r`.

---

## Шаг 5 — Исправить AndroidManifest.xml

**Файл:** `app_decompiled/AndroidManifest.xml`

**Было:**
```xml
android:extractNativeLibs="false"
```

**Стало:**
```xml
android:extractNativeLibs="true"
```

**Почему это нужно:**  
Оригинал имеет `extractNativeLibs="false"` — это значит нативные `.so` библиотеки остаются в APK и загружаются прямо из ZIP (не копируются на диск). Для этого они должны быть **выровнены по 4096 байт (page-aligned)** внутри ZIP.

Когда мы пересобираем split APK и переподписываем — выравнивание нарушается (см. историю ошибок ниже). Установка `extractNativeLibs="true"` означает "распакуй `.so` на диск при установке" — тогда выравнивание в ZIP не важно.

> Это применяется к `base.apk`. Нативные библиотеки физически находятся в `split_config.arm64_v8a.apk`, но флаг из манифеста `base.apk` управляет поведением всего пакета.

---

## Шаг 6 — Пересобрать APK

```powershell
Set-Location "D:\Programs\apktool\work"
echo "" | apktool b app_decompiled -o app_modified.apk --no-crunch
```

### Почему `--no-crunch`?

Без этого флага apktool падает с ошибкой:
```
W: ic_video_thumb_handle.png: error: failed to read PNG signature: file does not start with PNG signature.
```

**В чём проблема:**  
Файл `app_decompiled/res/drawable/ic_video_thumb_handle.png` на самом деле **не PNG** — это **WEBP** с неправильным расширением. Разработчики приложения переименовали WEBP в .png (вероятно, намеренно или это ошибка их сборки). Первые байты файла: `52 49 46 46` = `RIFF` — это сигнатура WEBP-контейнера, а не PNG (`89 50 4E 47`).

`aapt2` (которым apktool собирает ресурсы) строго проверяет формат и отказывается компилировать "PNG" с неправильной сигнатурой.

**Флаг `--no-crunch`** отключает обработку (crunching) PNG-ресурсов, и aapt2 копирует файл как есть, без валидации.

> **Примечание:** В apktool 3.0.2 нет флага `--use-aapt1` (был в старых версиях). Только `--no-crunch`.

---

## Шаг 7 — Подготовить split APK к переустановке

Это самая сложная часть. Нельзя просто переустановить `base.apk` — Android требует, чтобы все части split APK были подписаны **одним и тем же сертификатом**.

Оригинальные split APK подписаны ключом разработчика через Google Play. Нам нужно переподписать их **нашим** тестовым ключом.

### 7.1 — Создать тестовый keystore (один раз)

```powershell
keytool -genkey -v `
  -keystore D:\Programs\apktool\work\testkey.jks `
  -alias testkey `
  -keyalg RSA -keysize 2048 -validity 10000 `
  -storepass android -keypass android `
  -dname "CN=Test, OU=Test, O=Test, L=Test, S=Test, C=US"
```

### 7.2 — Проблема: нельзя просто переподписать split APK через jarsigner

Оригинальные split APK имеют в `META-INF/`:
- `BNDLTOOL.SF` — файл подписи (указывает что APK подписан по **APK Signature Scheme v2**)
- `BNDLTOOL.RSA` — сертификат

Если попытаться установить APK, где `BNDLTOOL.SF` указывает на v2-подпись, но v2-блок отсутствует или подписан другим ключом — Android выдаёт:
```
INSTALL_PARSE_FAILED_NO_CERTIFICATES: META-INF/BNDLTOOL.SF indicates APK is signed using APK Signature Scheme v2, but no such signature was found. Signature stripped?
```

**Решение:** нужно удалить `BNDLTOOL.SF` и `BNDLTOOL.RSA` из каждого split APK перед переподписыванием.

### 7.3 — Удаление старых подписей через .NET ZipFile (PowerShell)

```powershell
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Remove-MetaInfSignatures {
    param([string]$apkPath)
    $tmpPath = $apkPath + ".tmp"
    Copy-Item $apkPath $tmpPath
    $zip = [System.IO.Compression.ZipFile]::Open($tmpPath, 'Update')
    $toDelete = @($zip.Entries | Where-Object { $_.FullName -match "^META-INF/.*\.(SF|RSA|EC|DSA)$" })
    foreach ($entry in $toDelete) { $entry.Delete() }
    $zip.Dispose()
    Move-Item $tmpPath $apkPath -Force
}

foreach ($f in @("split_config.arm64_v8a.apk", "split_config.en.apk", "split_config.ru.apk", "split_config.xxhdpi.apk")) {
    Remove-MetaInfSignatures $f
}
```

> **ВАЖНОЕ ПРЕДУПРЕЖДЕНИЕ:**  
> `.NET ZipFile` в режиме `Update` **полностью переписывает файл** при закрытии.  
> Это **нарушает выравнивание ZIP-записей** — в частности, `resources.arsc` перестаёт быть выровнен по 4 байтам, а `.so` файлы — по 4096 байтам.  
> После этой операции нужно обязательно делать zipalign (см. шаг 8).

### 7.4 — Почему нельзя использовать jarsigner для финальной подписи

`jarsigner` создаёт только **APK Signature Scheme v1** (JAR signing).  
Android 11+ (API 30+) требует минимум **v2** подпись для установки.  
Попытка установить APK только с v1:
```
INSTALL_PARSE_FAILED_NO_CERTIFICATES: No signature found in package of version 2 or newer
```

Нужен `apksigner` или `uber-apk-signer`.

---

## Шаг 8 — Выравнивание ZIP (zipalign)

После удаления META-INF и любой модификации ZIP-файла нужно выровнять записи.

**Требования Android:**
- `resources.arsc` — должен быть выровнен по **4 байта** (требование Android 7+)
- `.so` файлы (нативные библиотеки) — выровнены по **4096 байт** (требование при `extractNativeLibs=false`)

Мы изменили это на `extractNativeLibs=true`, но `resources.arsc` всё равно нужно выровнять.

Проблема: Android SDK не установлен, `zipalign.exe` нет. Написан кастомный скрипт:

**`D:\Programs\apktool\work\zipalign.py`** — Python-скрипт, который перепаковывает ZIP с правильным выравниванием. Ключевая идея: padding добавляется в `extra field` локального заголовка ZIP-записи, не изменяя содержимое файлов. Это позволяет сохранить валидность v1-подписи после zipalign.

```powershell
# Применить zipalign ко всем APK
python zipalign.py input.apk output.apk
```

### Почему нужно делать zipalign ДВАЖДЫ для split APK

Правильный порядок для split APK (которые мы испортили через .NET ZipFile):

```
1. Удалить META-INF через .NET ZipFile  → выравнивание сломано
2. zipalign.py → выравнивание исправлено (*.aligned)
3. jarsigner → v1-подпись добавлена, но ZIP переписан → выравнивание сломано снова
4. zipalign.py → выравнивание исправлено снова (*.final)
   [v1-подпись всё ещё валидна — она основана на хэшах содержимого, не ZIP-структуры]
5. uber-apk-signer → добавляет v2+v3, делает финальный zipalign встроенным инструментом
```

Для `app_modified.apk` (base) путь проще — apktool собирает его с нуля, uber-apk-signer сам выравнивает.

---

## Шаг 9 — Финальная подпись через uber-apk-signer

`uber-apk-signer.jar` — сторонний инструмент (github.com/patrickfav/uber-apk-signer), скачан в `D:\Programs\apktool\work\`.

Он умеет:
- Создавать v1 + v2 + v3 подписи
- Запускать встроенный zipalign (для Windows берёт `win-zipalign_33_0_2.exe` из своих ресурсов)
- Переподписывать уже подписанные APK (`--allowResign`)
- Верифицировать результат

```powershell
Set-Location "D:\Programs\apktool\work"

# Сложить все APK в папку to_sign/
New-Item -ItemType Directory -Name "to_sign" -Force
Copy-Item "app_modified_final.apk"              "to_sign\"
Copy-Item "split_config.arm64_v8a_final.apk"    "to_sign\"
Copy-Item "split_config.en_final.apk"           "to_sign\"
Copy-Item "split_config.ru_final.apk"           "to_sign\"
Copy-Item "split_config.xxhdpi_final.apk"       "to_sign\"

# Подписать всё
java -jar uber-apk-signer.jar `
    -a "to_sign" `
    --ks testkey.jks `
    --ksAlias testkey `
    --ksPass android `
    --ksKeyPass android `
    --allowResign `
    -o "signed"
```

Результат в папке `signed/` — файлы вида `*-aligned-signed.apk`.

> **Нельзя использовать одновременно `--overwrite` и `-o`** — это взаимоисключающие опции.

---

## Шаг 10 — Установка на устройство

```powershell
# Сначала удалить старую версию
adb uninstall com.legend.smartwatch.electronicbadge.android

# Установить все 5 файлов ОДНОЙ командой
adb install-multiple `
    "D:\Programs\apktool\work\signed\app_modified_final-aligned-signed.apk" `
    "D:\Programs\apktool\work\signed\split_config.arm64_v8a_final-aligned-signed.apk" `
    "D:\Programs\apktool\work\signed\split_config.en_final-aligned-signed.apk" `
    "D:\Programs\apktool\work\signed\split_config.ru_final-aligned-signed.apk" `
    "D:\Programs\apktool\work\signed\split_config.xxhdpi_final-aligned-signed.apk"
```

> **Важно:** `adb install-multiple` понимает только файлы с расширением `.apk`.  
> Если файлы называются `.aligned`, `.final` и т.д. — adb откажет с ошибкой `need APK file on command line`.

---

## Шаг 11 — Обход проверки Play Integrity (PairIP)

После установки при запуске приложение показывало экран:
> "To continue using SuperBand, get it on Google Play"

**Причина:**  
Приложение встроило библиотеку **PairIP** (`com.pairip.licensecheck`) — это обёртка над Google Play Integrity API. Она проверяет:
1. Установлено ли приложение из Google Play Store
2. Совпадает ли подпись APK с подписью в Play Store

Поскольку мы переподписали приложение другим ключом — проверка падает и появляется этот экран.

**Файл:** `app_decompiled/smali/com/pairip/licensecheck/LicenseClient.smali`  
**Метод:** `initializeLicenseCheck()` — точка входа для запуска проверки

**Было (метод начинался с логики проверки):**
```smali
.method public initializeLicenseCheck()V
    .locals 2

    .line 127
    sget-object v0, Lcom/pairip/licensecheck/LicenseClient;->licenseCheckState:...
    ...
```

**Стало (метод сразу возвращается):**
```smali
.method public initializeLicenseCheck()V
    .locals 2

    return-void

    .line 127
    sget-object v0, Lcom/pairip/licensecheck/LicenseClient;->licenseCheckState:...
    ...
```

**Изменение:** только добавить `return-void` как первую инструкцию. `.locals 2` **оставить без изменений**.

**Почему это работает:**  
Мёртвый код (строки после `return-void`) не выполняется, но **Dalvik-верификатор проверяет весь байткод метода**, включая недостижимый. Мёртвый код использует регистры `v0` и `v1`, поэтому `.locals` должно оставаться `2`. Если изменить на `.locals 0` — верификатор отклонит класс при запуске приложения с ошибкой `register index out of range (1 >= 1)` и приложение вылетит сразу при старте.

После этого изменения — пересобираем, подписываем и устанавливаем заново (повторить шаги 6, 9, 10).

---

## Полная таблица ошибок

| # | Ошибка | Причина | Решение |
|---|--------|---------|---------|
| 1 | `INSTALL_FAILED_MISSING_SPLIT` | Попытка установить только `base.apk` | `adb install-multiple` со всеми 5 APK |
| 2 | `signatures are inconsistent` | Split APK подписаны разными ключами | Удалить все META-INF подписи, переподписать всё одним ключом |
| 3 | `Failed to extract native libraries, res=-2` | `extractNativeLibs=false` требует page-aligned `.so`, выравнивание нарушено | Изменить манифест: `extractNativeLibs="true"` |
| 4 | `resources.arsc must be stored uncompressed and aligned on a 4-byte boundary` | .NET ZipFile испортил выравнивание при удалении META-INF | Запустить zipalign (uber-apk-signer или zipalign.py) |
| 5 | `No signature found in package of version 2 or newer` | Android 11+ требует v2-подпись, `jarsigner` даёт только v1 | Использовать `uber-apk-signer.jar` |
| 6 | `need APK file on command line` | `adb install-multiple` не принял файлы с расширением не `.apk` | Переименовать файлы в `*.apk` |
| 7 | `either provide out path or overwrite argument, cannot process both` | В `uber-apk-signer` нельзя одновременно `--overwrite` и `-o` | Убрать `--overwrite`, оставить только `-o "папка"` |
| 8 | `ic_video_thumb_handle.png: failed to read PNG signature` | WEBP-файл переименован в .png разработчиком | Флаг `--no-crunch` при сборке apktool |
| 9 | "Get this app from Play" | PairIP license check (Play Integrity API) | Обнулить метод `LicenseClient.initializeLicenseCheck()` |

---

## Файловая структура проекта после всех шагов

```
D:\Programs\apktool\work\
  base.apk                              ← оригинальный base (скачан с телефона)
  split_config.arm64_v8a.apk           ← оригинальный split (скачан с телефона)
  split_config.en.apk
  split_config.ru.apk
  split_config.xxhdpi.apk
  app_decompiled/                       ← декомпилированный код
    AndroidManifest.xml                 ← изменён: extractNativeLibs=true
    smali_classes3/xfkj/.../
      VideoCutActivity.smali            ← изменён: лимиты и fps
    smali/com/pairip/licensecheck/
      LicenseClient.smali               ← изменён: bypass проверки
  app_modified.apk                      ← пересобранный base
  testkey.jks                           ← наш тестовый ключ
  uber-apk-signer.jar                   ← инструмент подписи
  zipalign.py                           ← кастомный zipalign
  to_sign/                              ← APK перед подписью
  signed/                               ← готовые к установке APK
    app_modified_final-aligned-signed.apk
    split_config.arm64_v8a_final-aligned-signed.apk
    split_config.en_final-aligned-signed.apk
    split_config.ru_final-aligned-signed.apk
    split_config.xxhdpi_final-aligned-signed.apk
```

---

## Быстрый план для повторного запуска (если нужно менять что-то ещё)

```powershell
Set-Location "D:\Programs\apktool\work"

# 1. Редактируем smali-файлы в app_decompiled/

# 2. Пересобрать
echo "" | apktool b app_decompiled -o app_modified.apk --no-crunch

# 3. Скопировать в папку подписи
Copy-Item app_modified.apk to_sign\app_modified_final.apk -Force

# 4. Удалить старый подписанный файл
Remove-Item signed\app_modified* -ErrorAction SilentlyContinue

# 5. Подписать
java -jar uber-apk-signer.jar -a to_sign\app_modified_final.apk --ks testkey.jks --ksAlias testkey --ksPass android --ksKeyPass android --allowResign -o signed

# 6. Установить (split APK из signed/ уже готовы)
adb uninstall com.legend.smartwatch.electronicbadge.android
adb install-multiple signed\app_modified_final-aligned-signed.apk signed\split_config.arm64_v8a_final-aligned-signed.apk signed\split_config.en_final-aligned-signed.apk signed\split_config.ru_final-aligned-signed.apk signed\split_config.xxhdpi_final-aligned-signed.apk
```

> **При повторном запуске split APK (arm64_v8a, en, ru, xxhdpi) уже подготовлены и подписаны в `signed/` — их не нужно трогать заново.** Пересобирается и переустанавливается только `app_modified.apk` (base).

---

## Часть 2 — Загрузка видео через BLE без приложения

**Устройство:** BW01 (видео-брелок), BLE-адрес `9D:05:2E:7F:A2:05`  
**Прошивка:** `LJ733_V1_BadgeOK`, FW `V33277`, HW `LJ733_MB_V1.1`  
**Скрипт:** `D:\Programs\apktool\work\upload_video.py`  
**Требования:** `pip install bleak`, Python 3.10+, ffmpeg в PATH

### Зачем

Модифицированное приложение снимает ограничение 5 сек, но требует Bluetooth-пары с телефоном. Скрипт `upload_video.py` загружает видео напрямую через BLE, без телефона.

### Как использовать

```powershell
# Загрузить видео на брелок (ffmpeg конвертирует автоматически)
python upload_video.py "BW01" my_video.mp4

# Или по MAC-адресу
python upload_video.py "9D:05:2E:7F:A2:05" my_video.mp4
```

Скрипт сам:
1. Конвертирует видео через ffmpeg (center-crop → 480×480, 24fps, **MJPEG/AVI**, yuvj420p, PCM audio)
2. Находит рабочий BLE-канал через probe
3. Выполняет handshake с устройством
4. Загружает файл чанками по 200 байт

### Обнаруженный BLE-протокол (BajiProtocol)

#### Правильный канал (НЕ ae00!)

Приложение в smali ссылается на сервис `ae00` (JieLi standard), но устройство на него **не отвечает**.  
Реально работает:

| Направление | UUID |
|---|---|
| Write (host→device) | `7e400002-b5a3-f393-e0a9-e50e24dcca9d` |
| Notify (device→host) | `7e400003-b5a3-f393-e0a9-e50e24dcca9d` |

#### Формат пакета host→device (маркер `0xCD`)

```
[0xCD][len 2B BE][0x25][0x01][module][cmd_data_len 2B BE][cmd][payload]
```

Минимум 9 байт.

#### Формат пакета device→host (маркер `0xDC`)

Короче — нет `protocol_version` и `cmd_data_len`:

```
[0xDC][len 2B BE][0x25][module][cmd][payload]
```

Минимум 8 байт.

#### Модули и команды

| Модуль | ID | Команды |
|---|---|---|
| FILE_TRANSFER | 0x01 | TRANSFER_START(0x00), TRANSFER_STOP(0x01), TRANSFER_ACK(0x02), TRANSFER_NACK(0x03), NEXT_CHUNK_REQ(0x04), RETRY_REQUEST(0x05), TRANSFER_COMPLETE(0x06), FILE_DATA(0x0A), STATUS(0x0B), RECEIVED_CHECKSUM(0x0C), TOTAL_TRANSFERRED(0x0D), **VERIFICATION_RESULT(0x0E)** |
| MEDIA_MANAGEMENT | 0x02 | MEDIA_ID_REQUEST(0x0D), MEDIA_ID_RESPONSE(0x0E) |
| SYSTEM_INFO | 0x03 | DEVICE_INFO_REQUEST(0x00), DEVICE_INFO_RESPONSE(0x01) |

> **Важно:** прошивка использует `cmd=0x00` как универсальный ACK для всех модулей (не `cmd=0x02` как в smali-спецификации).

#### Последовательность соединения

1. Подписаться на notify `7e400003`
2. Отправить `DEVICE_INFO_REQUEST` (module=0x03, cmd=0x00)
3. Устройство отвечает `DEVICE_INFO_REQUEST` (module=0x03, cmd=0x00, payload=`09 00`) — запрашивает наши данные
4. Ответить `DEVICE_INFO_RESPONSE` (module=0x03, cmd=0x01, payload=device_info)
5. Отправить `MEDIA_ID_REQUEST` (module=0x02, cmd=0x0D)
6. Устройство отвечает дважды:
   - (module=0x02, cmd=0x00, payload=`[media_id, 0x00]`) — ожидание
   - (module=0x02, cmd=0x00, payload=`[media_id, 0x01]`) — готово; `media_id` = первый байт
7. Отправить `TRANSFER_START` (module=0x01, cmd=0x00, 14-байт payload)
8. Устройство ACK: (module=0x01, cmd=0x00)
9. Отправлять `FILE_DATA` чанки (module=0x01, cmd=0x0A), устройство ACK каждый
10. Отправить `TRANSFER_COMPLETE` (module=0x01, cmd=0x06, 12-байт payload)
11. Устройство подтверждает `RECEIVED_CHECKSUM` (cmd=0x0C)
12. **Подождать 2 секунды**
13. **Отправить `VERIFICATION_RESULT` (module=0x01, cmd=0x0E, payload=`[fileId 8B BE]`)**
    — это регистрирует файл в галерее устройства! Без этого шага файл загружается, но **не отображается** в галерее/плейлисте.
14. Если устройство ответит cmd=0x0E, ответить подтверждением: payload=`[fileId 8B BE][0x01 1B]`

#### TRANSFER_START payload (14 байт)

```python
struct.pack(">BIBBBBBI", 0x07, file_size, 0x08, file_type, 0x0A, func_type, 0x09, media_id)
```

Теги: `0x07`=fileSize(4B), `0x08`=fileType, `0x0A`=funcType, `0x09`=mediaId(4B).

`func_type` для видео = **`FUNC_TYPE_BACKGROUND = 0x01`** (не PREVIEW=0x04!).
FunctionType: BACKGROUND=0x01, STICKER=0x02, FONT=0x03, PREVIEW=0x04.

#### FILE_DATA chunk payload (17 + data байт)

```python
struct.pack(">QII?", file_id, chunk_index, len(chunk_data), is_last) + chunk_data
```

- `file_id` = `media_id` (НЕ timestamp!) — устройство использует его для идентификации файла
- `MAX_CHUNK_SIZE` = 200 байт

#### TRANSFER_COMPLETE payload (12 байт)

```python
struct.pack(">QI", file_id, zlib.crc32(video_data) & 0xFFFFFFFF)
```

### Формат видео — критично!

Устройство **не умеет** воспроизводить H.264/MP4. Требуется **AVI с кодеком MJPEG**.

Точная FFmpeg-команда (из `VideoCutActivity.smali`, строки 2855 и 3052):
```
-r 24 -c:v mjpeg -vtag mjpg -pix_fmt yuvj420p -q:v 10
-coder 1 -flags +loop+global_header -pred 1 -qmin 10 -qmax 20
-vsync cfr -video_track_timescale 24 -packetsize 4096
-c:a pcm_s16le -ar 16000 -ac 1 -f avi
```

Аудио: PCM 16-bit LE, 16 kHz, моно. Видео: MJPEG, yuvj420p.

> `VideoPushViewModel.smali` содержит другую команду (H.264/MP4) — это конвертация для **предпросмотра в UI приложения**, а не для загрузки на устройство.

### Частые ошибки при reverse engineering

| Симптом | Причина | Решение |
|---|---|---|
| Устройство не отвечает | Неправильный канал (ae00/ae01) | Использовать 7e400002/7e400003 |
| Пакеты от устройства не парсятся | Маркер 0xDC вместо 0xCD, формат короче | Отдельная ветка разбора для 0xDC |
| Видео загружается, но не появляется на брелке | Неверный формат (H.264/MP4 вместо MJPEG/AVI) | Конвертировать в AVI/MJPEG с PCM аудио |
| Видео загружается, но не появляется на брелке | `file_id` = timestamp вместо `media_id` + пустой TRANSFER_COMPLETE | `file_id = media_id`; добавить `[fileId][crc32]` в TRANSFER_COMPLETE |
| "Write Not Permitted" на ae01 | ae01 поддерживает только write-without-response | `response=False` в `write_gatt_char` |
| **Видео загружается (0x25 FileTransfer), но всё равно не появляется в галерее** | 0x25 FileTransfer маршрутизирует в OTA/системную область, а не в галерею | **Использовать 0x1f WatchTheme протокол** (см. Часть 3) |

> **Важно:** протокол 0x25 FileTransfer **не подходит для загрузки видео в галерею**. Файлы, загруженные через 0x25, попадают в системную область (OTA) и никогда не появляются в видео-галерее/плейлисте брелка. Для загрузки в галерею необходим протокол **0x1f WatchTheme** (класс `WatchTheme3Tools` / `lt2.smali` в приложении). В текущей версии `upload_video.py` протокол 0x25 используется **только** для handshake (DEVICE_INFO), а сама загрузка происходит через 0x1f.

---

## Часть 3 — Протокол 0x1f WatchTheme (загрузка в галерею)

**Ключевое открытие:** анимация на экране брелка запускается **немедленно при отправке START-команды** (до передачи каких-либо данных) — именно это отличало загрузку через приложение. Команда `c42.g` (START, cmd=0x02) в smali вызывается сразу, ещё до начала передачи блоба.

### Источники в smali

| Класс | Путь | Роль |
|---|---|---|
| `lt2` | `smali_classes2/lt2.smali` | Основная логика WatchTheme: построение blob, START/DATA/FINISH payload, обработчик ответов |
| `c42` | `smali_classes2/c42.smali` | Построение пакетов: `t()` = 8-байт заголовок, `g/e/f` = START/DATA/FINISH |
| `he1` | `smali_classes2/he1.smali` | Утилита: `a([Z)B` — массив булевых → байт (binary string → base-2 int) |
| `c.b0()` | `smali_classes2/com/.../bluetooth/c.smali` | Dispatcher входящих BLE пакетов; байт[5]=0x01 → WatchTheme обработчик (`lt2.o().N()`) |

### Формат пакета host→device

```
[0xCD][len 2B BE][0x1F][0x01][CMD][payloadLen 2B BE][payload...]
```

где `len = 5 + payloadLen`. Это стандартный заголовок `c42.t()`.

### Команды (product_id = 0x1F)

| Символ в smali | CMD | Назначение |
|---|---|---|
| `c42.g([B)` | 0x02 | **START** — начало загрузки, запускает анимацию на экране |
| `c42.e([B)` | 0x01 | **DATA** — один чанк blob-данных |
| `c42.f([B)` | 0x03 | **FINISH** — конец передачи, содержит контрольную сумму |

### Формат ответов device→host

Устройство отправляет **два типа** пакетов в ответ на 0x1f команды:

#### Тип 1 — Short ACK (игнорировать)
```
[0xDC][len 2B][0x1F][CMD][payload...]
```
Пример: `dc 00 05 1f 02 00 19 01` — подтверждение START.  
Это промежуточный ACK, не несёт информации о статусе. **Игнорировать, ждать тип 2.**

#### Тип 2 — ResponseCode пакет (основной)
```
[0xCD][len 2B][0x20][0x01][0x01][payloadLen 2B][responseCode 4B BE]
```
Пример: `cd 00 09 20 01 01 00 04 00 00 03 e8` → responseCode = 1000

> Устройство использует маркер **0xCD** (как и host!) и product_id **0x20** (не 0x1F!) для этих ответов.  
> Байт[5] = 0x01 — именно он направляет пакет в WatchTheme-обработчик в `c.b0()`.

### Коды ответов (responseCode)

Из конструктора `lt2`: `this.l = [1000, 100_000_000)`, `this.m = [100_000_000, 200_000_000)`.

| Код | Смысл | Действие |
|---|---|---|
| **1000** | Готов (normalized = 0) | Отправить чанк 1 |
| **1000 + N** | Чанк N принят (normalized = N) | Отправить чанк N+1 |
| **2** | Успех (загрузка завершена) | FINISH принят, файл в галерее |
| **1** | Ошибка контрольной суммы | Повторить |
| 3–9 | Другие ошибки | — |

### Структура blob (lt2.S())

Для загрузки только видео (без превью-изображений):
```python
blob = struct.pack(">I", len(avi_bytes)) + avi_bytes
# = [4 байта BE: размер AVI][байты AVI]
```

Если бы были несколько файлов (preview, scale-preview, bg, watchTheme, defaultBg), blob был бы:
`[size1 4B][size2 4B]...[file1 bytes][file2 bytes]...`

### START payload (17 байт, lt2.Q())

```python
watchID     = 5538          # 0x15a2 — hardcoded ID для видео/фон в прошивке
file_type   = 1             # AVI
feature_bits = 0b001000     # = 8: he1.a() на 6-bool [isShowBgColor, hasPreview, hasScale, hasBg, hasWT, hasDefault]
                            # hasBg=True (index 3) → binary "001000" → base-2 int = 8

payload = (
    struct.pack(">I", watchID)           # 4B BE: watchID
    + bytes([file_type, feature_bits])   # fileType=1, featureBits=8
    + bytes([0, 0, 0])                   # R, G, B (цвет фона — чёрный)
    + struct.pack(">I", len(blob))       # 4B BE: полный размер blob
    + bytes([0, 0, 0, 0])               # style bytes (нули)
)
# Итого: 17 байт
```

**Как работает `he1.a()`:** принимает массив из 6 bool → строит строку "0" или "1" от последнего элемента к первому (MSB=последний) → парсит как двоичное число → возвращает byte.  
Для `[False, False, False, True, False, False]` (hasBg=True при index=3): строка = "001000" = 8.

### DATA chunk payload (lt2.O())

```python
def build_wt_chunk(index_1based: int, chunk_data: bytes) -> bytes:
    index_bytes = struct.pack(">H", index_1based)  # 2B BE, индекс 1-based
    indexed = index_bytes + chunk_data
    checksum = sum(b & 0xFF for b in indexed) & 0xFFFFFFFF
    return indexed + struct.pack(">I", checksum)   # 4B BE checksum
```

Чанк = `[2B BE индекс][данные][4B BE checksum]`. Checksum — сумма байт **индекса + данных** (не только данных!).  
`lt2.r` начинается с 0, индекс в пакете = `r + 1` (1-based).

### FINISH payload (lt2.k())

```python
total_sum = sum(b & 0xFF for b in blob) & 0xFFFFFFFF
finish_payload = struct.pack(">I", total_sum)   # 4B BE: сумма ВСЕХ байт blob
```

### Полная последовательность загрузки

```
1. Подключиться + подписаться на notify 7e400003
2. Handshake 0x25 DEVICE_INFO (модуль 0x03)
3. Конвертировать видео в AVI/MJPEG (ffmpeg)
4. Построить blob = [4B BE AVI_size][AVI bytes]
5. Вычислить chunk_size (стандарт приложения: 5000 байт)

6. Отправить 0x1f START (cmd=0x02, 17-байт payload)
   → Устройство начинает анимацию на экране
   → Ждать responseCode = 1000 (тип 2 пакет: 0xCD...0x20)

7. Stop-and-wait цикл:
   Для каждого чанка (индекс 1-based, N = 1..total_chunks):
     a. Отправить 0x1f DATA (cmd=0x01), payload = build_wt_chunk(N, data)
     b. Ждать responseCode = 1000 + N
     c. Перейти к следующему чанку

8. Отправить 0x1f FINISH (cmd=0x03), payload = [4B BE blob_checksum]
   → Ждать responseCode = 2 (SUCCESS)
   → Видео зарегистрировано в галерее!
```

### Производительность

Stop-and-wait (один ACK на чанк): ~16 KB/s. AVI 950 KB → ~60 секунд.  
Приложение использует чанки по 5000 байт с той же моделью.

### Ключевое отличие от 0x25 FileTransfer

| | 0x25 FileTransfer | 0x1f WatchTheme |
|---|---|---|
| Куда попадает файл | OTA/системная область | Галерея/плейлист |
| Класс в приложении | `FileTransferService` | `WatchTheme3Tools` (`lt2`) |
| Регистрация в галерее | Нет | Да (автоматически) |
| Размер чанка | 200 байт | 5000 байт |
| Анимация на экране | Нет | Да (сразу при START) |
| Checksum чанка | нет | 4B BE byte-sum |
| Blob-обёртка | нет (raw файл) | [4B size][file bytes] |

---

## Часть 4 — Обход ограничения размера (fake-size bypass)

**Контекст:** Прошивка брелка ограничивает размер видео на слот примерно **1 740 798 байт** (~1.66 MB), хотя физическая память устройства явно больше (~8 MB+, проверено загрузкой нескольких видео). Ограничение программное.

### Как определили точный лимит

При попытке загрузить файл > лимита устройство возвращает responseCode = `101740798` вместо стандартного ответа. По коду lt2.smali метод `u(J)V` декодирует его через строку:

```python
s = str(code)        # "101740798"
m_type  = int(s[1])  # s[1] = '0' → тип 0 (фатальная ошибка)
m_value = int(s[2:]) # 1740798 — вероятно, лимит в байтах
```

Тип 0 → фатальная ошибка (`V(1010, value)`), тип 1 → тихий отказ (наблюдался при `118544356`).

### Трёхуровневая валидация прошивки

Прошивка проверяет правильность blob **в три шага**:

#### Уровень 1 — Проверка размера в START (cmd=0x02)
Устройство проверяет: `declared_blobSize ≤ firmware_limit`  
**Обход:** в START payload (поле `blobSize`) указываем маленькое значение, например 1 700 000.

#### Уровень 2 — Проверка согласованности blob[0:4] в первом DATA-чанке
Устройство проверяет: `blob[0:4] (BE) + 4 == declared_blobSize`  
Если не совпадает → responseCode = 0 (ошибка валидации).  
**Обход:** подделываем `blob[0:4] = struct.pack(">I", declared_size - 4)`.

#### Уровень 3 — Контрольная сумма в FINISH (cmd=0x03)
Устройство верифицирует контрольную сумму только первых `declared_blobSize` байт, а не всего принятого blob.  
**Обход:** вычисляем checksum только от `blob[:declared_size]`.

### КРИТИЧЕСКАЯ ОШИБКА — причина crash loop

**Что произошло (попытка 6):** Были обойдены все три уровня валидации, однако:
- Объявленный размер: `declared_blobSize = 1 700 000`
- Реальный размер blob: 5.5 MB (полное AVI на 45 сек)
- **Переданы ВСЕ 1103 чанка** (весь 5.5 MB blob)

**Результат:** Прошивка выделила буфер под 1.7 MB, а мы записали в него 5.5 MB данных → переполнение буфера → порча памяти → crash loop (брелок пытается загрузиться, сразу вылетает, watchdog сбрасывает).

**Устройство в режиме зарядки:** не падает, показывает анимацию зарядки — BLE доступен.

**Правильный подход:** при fake-size загрузке передавать **только** первые `declared_size` байт blob, не более:
```python
send_limit = declared_size if declared_size else blob_len
total_chunks = (send_limit + WT_CHUNK_SIZE - 1) // WT_CHUNK_SIZE
```

### Проблема с AVI RIFF заголовком при обрезке

Когда мы отправляем только первые N байт из 5.5 MB AVI, RIFF-заголовок в начале файла говорит `fileSize = 5.5 MB`. AVI-плеер прошивки попытается обратиться к данным за пределами N байт → crash.

**Решение:** после обрезки blob до declared_size патчим RIFF заголовок:
- `blob[4:8]` — RIFF chunk size = `(N - 4 - 8)` (от "AVI " и дальше)
- `avih.dwTotalFrames` — реальное число кадров, которые влезают в N байт

### Математика аудио (жёсткое ограничение)

Прошивка требует PCM 16-bit LE 16 kHz mono аудио:
- Аудио занимает **32 000 байт/сек**
- Для 45 секунд: **1 440 000 байт только аудио**
- Лимит прошивки: 1 740 798 байт → для видео остаётся лишь 300 798 байт (~334 байт/кадр при 20fps)

Это физически невозможно для любого осмысленного визуального качества. **Максимальная реальная длина** видео при разумном качестве:

| Длительность | Аудио (16kHz) | Осталось на видео | fps=20, разрешение |
|---|---|---|---|
| 45 сек | 1.37 MB | 371 KB (~334 B/кадр) | Нереально |
| 30 сек | 915 KB | 810 KB (~1350 B/кадр) | 160×160 q=31 (едва) |
| 20 сек | 610 KB | 1.08 MB (~2700 B/кадр) | 240×240 q=25 (OK) |
| 14 сек | 427 KB | 1.28 MB (~4550 B/кадр) | 320×320 q=15 (хорошо) |

Если устройство поддерживает 8 kHz аудио (телефонное качество, ~16 000 байт/сек):
- Для 45 секунд: 720 KB аудио → 1 MB на видео → 160×160 q=28 (~1134 B/кадр) — может работать.

### Код в upload_video.py

Ключевые изменения в скрипте для fake-size:

```python
# В upload():
if self._start_size_override is not None:
    fake_header = self._start_size_override - 4
    blob = struct.pack(">I", fake_header) + avi_data
else:
    blob = struct.pack(">I", avi_len) + avi_data

# Количество чанков — только до declared_size, не весь blob:
send_limit = self._start_size_override if self._start_size_override else blob_len
total_chunks = (send_limit + WT_CHUNK_SIZE - 1) // WT_CHUNK_SIZE

# FINISH checksum — только первые declared_size байт:
finish_payload = build_wt_finish_payload(blob, self._start_size_override)
```

```python
def build_wt_finish_payload(blob: bytes, declared_size: int = None) -> bytes:
    checksum_data = blob[:declared_size] if declared_size is not None else blob
    total_sum = sum(b & 0xFF for b in checksum_data) & 0xFFFFFFFF
    return struct.pack(">I", total_sum)
```


---

## Part 5: JLI OTA — Factory Reset (восстановление брелка)

### Как был восстановлен брелок

После crash-loop (Part 4) брелок был восстановлен через приложение SuperBand:
1. Зашёл в SuperBand -> нажал кнопку **Online Upgrade**
2. Приложение скачало прошивку с сервера Gulaike и залило через BLE
3. Устройство полностью сбросилось до заводского состояния (~3-5 минут)

### Протокол JLI OTA (Jieli Technology)

BW01 использует чип **Jieli Technology** (PLAM_TYPE = 0x7).
Для прошивки используется **отдельный BLE сервис** (не 7e400002!):

| UUID | Назначение |
|---|---|
| 0000ae00-0000-1000-8000-00805f9b34fb | OTA Service |
| 0000ae01-0000-1000-8000-00805f9b34fb | Write (host->device) |
| 0000ae02-0000-1000-8000-00805f9b34fb | Notify (device->host) |

### Формат пакета

    FE DC BA [FLAGS] [OPCODE] [PLEN_HI] [PLEN_LO] [PAYLOAD...] [EF]

- Prefix: FE DC BA (confirmed from ParseHelper.smali:array_0)
- End flag: EF (-0x11)
- FLAGS: bit7 = type (1=command from sender, 0=response), bit6 = expects_response
- PLEN: длина PAYLOAD = SN + data
- PAYLOAD (command, type=1): [SN][data...]
- PAYLOAD (response, type=0): [STATUS][SN][data...]

### OTA последовательность (pull model!)

Устройство САМО запрашивает блоки прошивки (не хост толкает):

    Host -> Device:  0xE3  ENTER_OTA     (нет данных, flags=0xC0)
    Device -> Host:  0xE3  response      status=0 = OK
    Host -> Device:  0xE8  NOTIFY_SIZE   data = firmware_size[4B BE]
    Device -> Host:  0xE8  response      status=0 = OK
    Device -> Host:  0xE5  block request param = offset[4B BE] + len[2B BE]
    Host -> Device:  0xE5  block resp    status=0, data = firmware[offset:offset+len]
    ... повторяется до полной передачи ...
    Устройство само перезагружается и применяет прошивку

### Firmware API (Gulaike)

    GET https://tomato.gulaike.com/api/v1/config/app?name=BW01&type=1&version=0
    Authorization: Bearer 6fcb7f58475b4e5aad8f0f1cadce235e

- Ответ содержит URL для скачивания прошивки
- Прошивка может быть в ZIP; внутри файл .ufw / .bin / .img
- API может быть геоблокирован (нужен VPN или ручное скачивание)

### Скрипт: jli_ota.py

    python jli_ota.py

Скрипт автоматически:
1. Загружает прошивку с Gulaike API (или использует кешированный bw01_firmware.bin)
2. Подключается к брелку по MAC 9D:05:2E:7F:A2:05
3. Выполняет полный JLI OTA протокол
4. Брелок сбрасывается до заводского состояния

Важно: Если OTA сервис 0000ae00 не найден — нужно сначала открыть SuperBand -> Online Upgrade,
чтобы устройство переключилось в OTA режим.

### Ключевые файлы smali

| Файл | Что содержит |
|---|---|
| com/jieli/jl_bt_ota/tool/ParseHelper.smali:1249 | packSendBasePacket() — точный формат пакета |
| com/jieli/jl_bt_ota/model/cmdHandler/OtaCmdHandler.smali | Парсинг команд (pull model) |
| com/jieli/jl_bt_ota/model/parameter/FirmwareUpdateBlockParam.smali | Формат 0xE5 запроса |
| com/jieli/jl_bt_ota/model/parameter/NotifyUpdateContentSizeParam.smali | Формат 0xE8 |
| xfkj/fitpro/activity/ota/api/HttpHelper.smali | Gulaike API URL + Bearer token |
| xfkj/fitpro/activity/ota/OTAProxyUtils.smali:351 | Роутинг plarmType=7 -> JliOTAActivity |


---

## Часть 6 — Коды ответа брелка и реальный лимит

### Как декодировать код отказа

При превышении лимита брелок возвращает responseCode в диапазоне [100M, 1000M).
Декодирование (lt2.smali, метод `u(J)`):

```python
s = str(code)
m_type  = int(s[1])   # тип ошибки
m_value = int(s[2:])  # ПРЕВЫШЕНИЕ в байтах (не лимит!)
# реальный лимит = blob_size - m_value
```

**Ключевой момент:** `m_value` — это НА СКОЛЬКО БАЙТ blob превысил лимит, а не сам лимит.

### Подтверждение двумя разными попытками

    Попытка 1: blob = 4,931,904 байт → код 102695488 → m_value = 2,695,488
               лимит = 4,931,904 - 2,695,488 = 2,236,416 байт

    Попытка 2: blob = 2,306,948 байт → код 100070532 → m_value = 70,532
               лимит = 2,306,948 - 70,532 = 2,236,416 байт

Оба дают один и тот же результат: **реальный лимит слота = 2,236,416 байт (~2.13 MB)**.

Если бы m_value было самим лимитом — попытка 2 говорила бы «лимит 70KB», что абсурдно.

### Коды из probe-запроса (declared_size=200MB)

    Код 297763584 → диапазон [200M, 1000M) → s[2:] = «7763584» = 7,763,584 байт
    Это НЕ лимит слота. Предположительно: суммарный свободный Flash (~7.4 MB).

Probe с объявленным 200MB смотрит на другое ограничение, чем реальная загрузка.

### Лимит слота watchID 5538

**2,236,416 байт (~2.13 MB)** — это аппаратный предел буфера в прошивке для watchID 5538.
Без модификации прошивки это не изменить.

Фиксировано в upload_video.py: при отказе теперь показывается:
- «Blob size: X MB»
- «Over limit: Y bytes» (само m_value — превышение)
- «Device limit: Z MB» (blob - overage = реальный лимит)

### Рабочие параметры загрузки (45 секунд, подтверждено)

    python upload_video.py BW01 IMG_4463.MP4 --duration 45 --audio-rate 8000 --resolution 140x140 --quality 31 --fps 18

Бюджет при этих настройках:
- Аудио 8kHz: 45s × 16,000 байт/сек = 720,000 байт (~0.69 MB)
- Видео: ~1.5 MB на 810 кадров (45s × 18fps) → ~1852 байт/кадр при 140×140
- Итого blob: < 2,236,416 байт ✓

### Таблица параметров для разных длительностей (8kHz аудио, лимит 2.13 MB)

    Длительность | Аудио  | Видео бюджет | Параметры                              | Статус
    -------------|--------|--------------|----------------------------------------|-------
    45 сек       | 720 KB | ~1.51 MB     | 140x140 q=31 fps=18                    | РАБОТАЕТ ✓
    45 сек       | 720 KB | ~1.51 MB     | 140x140 q=30 fps=20  → blob=2.20MB     | Отказ (70KB перебор)
    35 сек       | 560 KB | ~1.67 MB     | 160x160 q=26 fps=20                    | Должно вписаться
    30 сек       | 480 KB | ~1.75 MB     | 160x160 q=22 fps=20                    | Должно вписаться
