# NOC Toolkit - Инструкция для сборки Windows .exe

**Дата:** 25 февраля 2026
**Цель:** Создать standalone .exe файл для Windows без установки Python

---

## 📋 Требования

### Что нужно установить:

1. **Python 3.10+**
   - Скачать с: https://www.python.org/downloads/
   - ✅ При установке отметить: "Add Python to PATH"

2. **Git** (опционально, для клонирования репозитория)
   - Скачать с: https://git-scm.com/download/win

---

## 🚀 Шаг за шагом

### Шаг 1: Получить исходный код

**Вариант A:** Если есть Git
```cmd
git clone <URL_РЕПОЗИТОРИЯ>
cd noc-toolkit
```

**Вариант B:** Скачать ZIP архив
1. Скачать архив с кодом
2. Распаковать в папку (например: `C:\noc-toolkit`)
3. Открыть Command Prompt в этой папке

---

### Шаг 2: Установить зависимости

Открыть **Command Prompt** (cmd) в папке проекта:

```cmd
REM Установить зависимости проекта
pip install -r requirements.txt

REM Установить PyInstaller
pip install pyinstaller
```

**Проверка:** Убедиться что всё установилось без ошибок.

---

### Шаг 3: Тестовый запуск

Проверить что toolkit работает:

```cmd
python noc-toolkit.py
```

Должно появиться меню с 3 инструментами. Нажмите `0` для выхода.

---

### Шаг 4: Сборка .exe файла

```cmd
pyinstaller NOC-Toolkit.spec --clean
```

**Ожидаемый результат:**
- Процесс займёт 2-5 минут
- В конце должно быть: `Building EXE from EXE-00.toc completed successfully`
- Создаётся файл: `dist\NOC-Toolkit.exe`

---

### Шаг 5: Проверка результата

```cmd
dir dist\
```

Должны увидеть файл `NOC-Toolkit.exe` размером ~80-100 MB.

Проверка типа файла:
```cmd
dist\NOC-Toolkit.exe
```

Должно открыться меню toolkit.

---

### Шаг 6: Создание пакета для распространения

Создать папку с готовым пакетом:

```cmd
REM Создать директорию релиза
mkdir noc-toolkit-windows-release

REM Скопировать файлы
copy dist\NOC-Toolkit.exe noc-toolkit-windows-release\
copy .env.example noc-toolkit-windows-release\
copy README.md noc-toolkit-windows-release\
copy README_RU.md noc-toolkit-windows-release\
```

Создать файл `noc-toolkit-windows-release\run.bat`:

```batch
@echo off
echo ============================================
echo NOC Toolkit for Windows
echo ============================================
echo.

NOC-Toolkit.exe

echo.
echo ============================================
pause
```

---

### Шаг 7: Создать архив

```cmd
REM Упаковать в ZIP
powershell Compress-Archive -Path noc-toolkit-windows-release -DestinationPath noc-toolkit-v1.0.0-windows.zip
```

**Готово!** Файл `noc-toolkit-v1.0.0-windows.zip` готов к распространению.

---

## 📦 Содержимое итогового пакета

```
noc-toolkit-windows-release/
├── NOC-Toolkit.exe           # ~80-100 MB
├── run.bat                   # Батник для запуска
├── .env.example              # Шаблон конфигурации
├── README.md                 # Документация (English)
└── README_RU.md              # Документация (Русский)
```

---

## 🔧 Решение проблем

### Проблема 1: "pip не найден"

**Симптом:**
```
'pip' is not recognized as an internal or external command
```

**Решение:**
1. Переустановить Python, отметив "Add Python to PATH"
2. Или использовать полный путь:
   ```cmd
   C:\Python310\python.exe -m pip install pyinstaller
   ```

---

### Проблема 2: Антивирус блокирует .exe

**Симптом:** Windows Defender удаляет/блокирует NOC-Toolkit.exe

**Решение:**
1. Добавить в исключения Windows Defender:
   - Открыть "Windows Security"
   - "Virus & threat protection" → "Manage settings"
   - "Add or remove exclusions"
   - Добавить папку `dist\`

2. Или временно отключить Real-time protection при сборке

---

### Проблема 3: "Failed to execute script" или тулы не запускаются

**Симптом:** При запуске .exe появляется ошибка, или тулы показывают [✗]

**Решение:**
1. Проверить файл `noc-toolkit-debug.log` — он автоматически создаётся рядом с EXE при каждом запуске
2. В логе видно:
   - Какие пути используются (`SCRIPT_DIR`, `TOOLS_DIR`, `EXE_DIR`)
   - Нашёлся ли `.env` и какие переменные загружены
   - Список файлов в `tools/` директории
   - Какие тулы запускались и с каким результатом
3. Если в логе `TOOLS_DIR does not exist!` — тулы не были упакованы в EXE
4. Если `IMPORT ERROR` — зависимость не указана в `hiddenimports` в `NOC-Toolkit.spec`
5. Пересобрать с debug: `pyinstaller NOC-Toolkit.spec --clean --debug=all`

### Проблема 3a: ".env не найден" хотя файл лежит рядом с .exe

**Симптом:** `⚠ Config: No .env file found` при том что `.env` рядом с EXE

**Решение:** Это было исправлено в v0.2.0. Убедитесь что используете актуальную версию кода.

---

### Проблема 4: Большой размер файла (>100 MB)

**Решение:**
1. Это нормально для PyInstaller (включает Python runtime)
2. Можно уменьшить с UPX:
   ```cmd
   REM Скачать UPX: https://upx.github.io/
   REM Указать путь в команде:
   pyinstaller NOC-Toolkit.spec --clean --upx-dir=C:\path\to\upx
   ```

---

## ✅ Контрольный список

### Подготовка:
- [ ] Python 3.10+ установлен
- [ ] pip работает
- [ ] Исходный код распакован
- [ ] Command Prompt открыт в правильной папке

### Сборка:
- [ ] requirements.txt установлен
- [ ] PyInstaller установлен
- [ ] Тестовый запуск работает
- [ ] `pyinstaller NOC-Toolkit.spec --clean` выполнен успешно

### Проверка:
- [ ] `dist\NOC-Toolkit.exe` создан
- [ ] Размер файла ~80-100 MB
- [ ] .exe запускается и показывает меню
- [ ] Все 3 инструмента видны

### Упаковка:
- [ ] Папка релиза создана
- [ ] Все файлы скопированы
- [ ] run.bat создан
- [ ] ZIP архив создан

---

## 📞 Помощь

**Если что-то не работает:**

1. **Проверить версию Python:**
   ```cmd
   python --version
   ```
   Должно быть: `Python 3.10.x` или выше

2. **Проверить PyInstaller:**
   ```cmd
   pyinstaller --version
   ```
   Должно быть: `6.x.x`

3. **Посмотреть логи:**
   ```cmd
   type build\NOC-Toolkit\warn-NOC-Toolkit.txt
   ```

4. **Очистить и пересобрать:**
   ```cmd
   rmdir /s /q build dist
   pyinstaller NOC-Toolkit.spec --clean
   ```

---

## 🎯 Быстрый запуск (для опытных)

```cmd
pip install -r requirements.txt && pip install pyinstaller
pyinstaller NOC-Toolkit.spec --clean
mkdir noc-toolkit-windows-release
copy dist\NOC-Toolkit.exe noc-toolkit-windows-release\
copy .env.example noc-toolkit-windows-release\
copy README*.md noc-toolkit-windows-release\
```

**Готово!** Файл в `dist\NOC-Toolkit.exe`

---

## 📝 Примечания

1. **Первый запуск медленный**
   - PyInstaller распаковывает файлы при первом запуске
   - Последующие запуски будут быстрее

2. **.env файл**
   - Нужно создать `.env` рядом с .exe
   - Скопировать из `.env.example` и заполнить свои данные

3. **Обновление**
   - При обновлении кода - пересобрать .exe
   - Просто запустить: `pyinstaller NOC-Toolkit.spec --clean`

---

**Успешной сборки! 🚀**
