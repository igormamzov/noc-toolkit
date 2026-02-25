# 🚀 NOC Toolkit - Быстрый старт для Windows

**Для товарища на Windows 11 без Python**

---

## 📥 Что нужно сделать (5 минут)

### 1. Установить Python

**Скачать и установить:**
- Перейти на: https://www.python.org/downloads/
- Скачать "Python 3.10" или новее
- ✅ **ВАЖНО:** При установке отметить галочку **"Add Python to PATH"**

![Python PATH](https://docs.python.org/3/_images/win_installer.png)

---

### 2. Получить код

**Вариант A:** Скачать ZIP архив (проще)
1. Распаковать в папку `C:\noc-toolkit`

**Вариант B:** Клонировать через Git
```cmd
git clone <URL>
cd noc-toolkit
```

---

### 3. Запустить автоматическую сборку

Открыть **Command Prompt** в папке проекта и запустить:

```cmd
build-windows.bat
```

Скрипт автоматически:
- ✅ Проверит Python
- ✅ Установит зависимости
- ✅ Установит PyInstaller
- ✅ Соберёт .exe файл
- ✅ Создаст пакет для использования

**Время:** ~3-5 минут

---

### 4. Готово!

После успешной сборки:

```
✓ Файл создан: dist\NOC-Toolkit.exe (~80-100 MB)
✓ Пакет готов: noc-toolkit-windows-release\
```

**Запуск:**
```cmd
cd noc-toolkit-windows-release
run.bat
```

---

## 🎯 Вариант "Всё вручную"

Если автоматический скрипт не сработал:

```cmd
REM Установить зависимости
pip install -r requirements.txt
pip install pyinstaller

REM Собрать .exe
pyinstaller NOC-Toolkit.spec --clean

REM Готово! Файл в dist\NOC-Toolkit.exe
```

---

## ⚠️ Возможные проблемы

### "pip не найден"
**Решение:** Переустановить Python с галочкой "Add to PATH"

### Антивирус блокирует
**Решение:** Добавить папку `dist\` в исключения Windows Defender

### "Failed to execute script"
**Решение:** Запустить с флагом debug:
```cmd
pyinstaller NOC-Toolkit.spec --clean --debug=all
```

---

## 📖 Полная документация

Смотри:
- [WINDOWS_BUILD_INSTRUCTIONS.md](WINDOWS_BUILD_INSTRUCTIONS.md) - детальная инструкция
- [README.md](README.md) - документация toolkit
- [README_RU.md](README_RU.md) - русская документация

---

## 🆘 Нужна помощь?

1. Проверить версию Python: `python --version` (должно быть 3.10+)
2. Проверить PyInstaller: `pyinstaller --version`
3. Посмотреть логи: `type build\NOC-Toolkit\warn-NOC-Toolkit.txt`

---

**После сборки:**
1. Архивировать папку `noc-toolkit-windows-release\`
2. Можно удалить исходный код и оставить только .exe
3. Передать архив коллегам - им Python **не нужен**!

---

**Успехов! 🚀**
