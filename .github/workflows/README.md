# GitHub Actions - Автоматическая сборка

**Личный проект** - настроено для использования в твоём личном GitHub репозитории.

---

## 🤖 Доступные Workflows

### 1. Build Windows EXE
**Файл:** `build-windows.yml`

**Что делает:**
- Собирает Windows .exe на настоящем Windows
- Создаёт готовый пакет для распространения
- Автоматически запускается при push в main (если изменились .py файлы)

**Ручной запуск:**
1. Открыть GitHub репозиторий
2. Вкладка **"Actions"**
3. Выбрать **"Build Windows EXE"**
4. Нажать **"Run workflow"** → "Run workflow"
5. Подождать 5-10 минут
6. Скачать артефакт **"NOC-Toolkit-Windows"** (готовый .zip)

**Что получишь:**
- `noc-toolkit-windows.zip` - готовый пакет с .exe, документацией и run.bat
- `NOC-Toolkit.exe` - standalone файл (отдельно)

---

### 2. Build Multi-Platform Release
**Файл:** `build-release.yml`

**Что делает:**
- Собирает для **Windows + macOS + Linux** одновременно
- Создаёт архивы для каждой платформы
- Генерирует MD5 checksums
- Создаёт release summary

**Ручной запуск:**
1. Открыть GitHub репозиторий
2. Вкладка **"Actions"**
3. Выбрать **"Build Multi-Platform Release"**
4. Нажать **"Run workflow"**
5. (Опционально) Ввести версию: `v1.0.0`
6. Нажать "Run workflow"
7. Подождать 10-15 минут
8. Скачать артефакты:
   - `noc-toolkit-windows` (.zip для Windows)
   - `noc-toolkit-macos` (.tar.gz для Mac)
   - `noc-toolkit-linux` (.tar.gz для Linux)
   - `release-summary` (итоговый отчёт)

---

## 📥 Как скачать собранные файлы

1. Перейти на страницу завершённого workflow
2. Прокрутить вниз до секции **"Artifacts"**
3. Нажать на нужный артефакт для скачивания

**Артефакты хранятся 30-90 дней** (зависит от workflow)

---

## 💰 Лимиты GitHub Actions (бесплатно)

**Публичные репозитории:**
- ✅ **Безлимитные** минуты сборки
- ✅ Полностью бесплатно

**Приватные репозитории:**
- ✅ **2000 минут/месяц** бесплатно
- Одна сборка Windows: ~5-10 минут
- Multi-platform: ~15 минут
- **Вывод:** ~130-200 сборок в месяц бесплатно

---

## 🔧 Настройка после клонирования

### Первый push:

```bash
cd /Users/master/noc-toolkit
git add .github/workflows/
git commit -m "Add GitHub Actions workflows for cross-platform builds"
git push
```

### Первый запуск:

1. Открыть https://github.com/ТвойUsername/noc-toolkit/actions
2. Нажать "I understand my workflows, go ahead and enable them"
3. Выбрать workflow и запустить

---

## 🎯 Быстрый старт

**Хочешь Windows .exe прямо сейчас?**

```bash
# 1. Commit workflows
git add .github/
git commit -m "Add CI/CD workflows"
git push

# 2. Открыть в браузере:
#    https://github.com/YOUR_USERNAME/noc-toolkit/actions
#
# 3. Выбрать "Build Windows EXE" → "Run workflow"
#
# 4. Через 5-10 минут скачать готовый .zip
```

---

## 📋 Что происходит при сборке

### Windows workflow:
1. ✅ Поднимает Windows виртуалку
2. ✅ Устанавливает Python 3.10
3. ✅ Устанавливает зависимости из requirements.txt
4. ✅ Устанавливает PyInstaller
5. ✅ Запускает `pyinstaller NOC-Toolkit.spec --clean`
6. ✅ Проверяет что .exe создан
7. ✅ Создаёт пакет с документацией
8. ✅ Создаёт run.bat
9. ✅ Архивирует в .zip
10. ✅ Загружает артефакт

**Время:** ~5-10 минут

---

## 🔍 Troubleshooting

### Workflow не запускается
**Решение:** Проверь что workflows включены в настройках репозитория:
- Settings → Actions → General → "Allow all actions"

### Ошибка при сборке
1. Открыть failed workflow
2. Посмотреть логи (кликнуть на шаг с ошибкой)
3. Исправить проблему
4. Push исправлений (workflow запустится автоматически)

### Не вижу артефакты
**Решение:** Проверь что workflow завершился успешно (зелёная галочка)

---

## 📝 Примечания

1. **Это не корпоративный CI/CD** - это твой личный GitHub Actions
2. **Бесплатно** для публичных репозиториев
3. **Автоматическая сборка** при push (только для build-windows.yml)
4. **Ручная сборка** всегда доступна через "Run workflow"
5. **Артефакты автоматически удаляются** через 30-90 дней

---

## 🎉 Готово!

Теперь у тебя есть:
- ✅ Автоматическая сборка Windows .exe
- ✅ Мультиплатформенная сборка (Win/Mac/Linux)
- ✅ Готовые пакеты для распространения
- ✅ Checksums для проверки целостности

**Просто нажми кнопку в GitHub и получишь готовый .exe!** 🚀
