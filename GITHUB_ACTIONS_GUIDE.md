# 🚀 Быстрый гайд: GitHub Actions

**Личный проект** - собирай Windows .exe прямо с Mac через GitHub!

---

## 🎯 Зачем это нужно

- ✅ Собирать Windows .exe **не имея Windows**
- ✅ Автоматическая сборка при push
- ✅ Мультиплатформенная сборка (Win/Mac/Linux)
- ✅ **Бесплатно** (публичный репозиторий = unlimited builds)
- ✅ Делиться готовыми .exe с товарищами

---

## 📋 Первый раз: Настройка (5 минут)

### 1. Создать GitHub репозиторий (если ещё нет)

```bash
cd /Users/master/noc-toolkit

# Инициализация (если ещё не сделано)
git init
git add .
git commit -m "Initial commit: NOC Toolkit"

# Создать репозиторий на GitHub.com
# https://github.com/new
# Имя: noc-toolkit
# Visibility: Public (для unlimited builds)

# Связать с GitHub
git remote add origin https://github.com/ТвойUsername/noc-toolkit.git
git branch -M main
git push -u origin main
```

### 2. Включить GitHub Actions

1. Открыть: https://github.com/ТвойUsername/noc-toolkit/actions
2. Нажать: **"I understand my workflows, go ahead and enable them"**

**Готово!** Теперь можешь собирать .exe через GitHub.

---

## 🔨 Как собрать Windows .exe

### Способ 1: Автоматически (при каждом push)

```bash
# Внеси изменения в код
nano tools/pd-monitor/pd_monitor.py

# Commit и push
git add .
git commit -m "Update pd-monitor"
git push

# GitHub автоматически начнёт сборку!
# Открой: https://github.com/ТвойUsername/noc-toolkit/actions
```

### Способ 2: Вручную (по кнопке) ⭐ РЕКОМЕНДУЮ

1. **Открыть:** https://github.com/ТвойUsername/noc-toolkit/actions
2. **Выбрать:** "Build Windows EXE" (левая панель)
3. **Нажать:** "Run workflow" (справа) → "Run workflow" (зелёная кнопка)
4. **Ждать:** 5-10 минут
5. **Скачать:** Scroll вниз → "Artifacts" → "NOC-Toolkit-Windows"

**Готово!** У тебя Windows .exe собранный на настоящем Windows.

---

## 📦 Что получаешь

### После сборки в Artifacts:

**1. NOC-Toolkit-Windows** (zip архив)
```
noc-toolkit-windows.zip
├── NOC-Toolkit.exe       ← Главный файл (~80-100 MB)
├── run.bat               ← Удобный запуск
├── .env.example          ← Шаблон настроек
├── README.md
└── README_RU.md
```

**2. NOC-Toolkit-exe-only** (только .exe)
- Чистый `NOC-Toolkit.exe` без документации

---

## 🌍 Мультиплатформенная сборка

**Для создания релиза на все платформы:**

1. Открыть: https://github.com/ТвойUsername/noc-toolkit/actions
2. Выбрать: **"Build Multi-Platform Release"**
3. Нажать: "Run workflow"
4. Ввести версию: `v1.0.0` (опционально)
5. Нажать: "Run workflow"
6. Подождать ~10-15 минут

**Получишь:**
- `noc-toolkit-windows` - Windows .exe + архив
- `noc-toolkit-macos` - Mac executable + архив
- `noc-toolkit-linux` - Linux executable + архив
- `release-summary` - Итоговый отчёт с checksums

---

## 💡 Советы

### Когда использовать какой workflow:

| Ситуация | Workflow |
|----------|----------|
| Нужен только Windows .exe | **Build Windows EXE** |
| Релиз для всех платформ | **Build Multi-Platform Release** |
| Быстро протестировать изменения | **Build Windows EXE** |
| Официальный релиз v1.0.0 | **Build Multi-Platform Release** |

### Ускорение:

**Отключить авто-сборку при push:**
- Отредактировать [.github/workflows/build-windows.yml](.github/workflows/build-windows.yml)
- Закомментировать секцию `on: push:`
- Оставить только `workflow_dispatch` (ручной запуск)

---

## 🔍 Как посмотреть логи сборки

1. Открыть workflow run
2. Кликнуть на шаг (например: "Build EXE")
3. Увидишь весь вывод PyInstaller

**Если сборка failed:**
- Красный крестик ❌
- Кликнуть на failed step
- Посмотреть ошибку
- Исправить → push → workflow запустится снова

---

## 📊 Мониторинг использования

**Проверить сколько минут использовано:**

1. GitHub.com → Settings (твой профиль)
2. Billing and plans
3. Plans and usage
4. Actions → Usage this month

**Публичный репозиторий:** Unlimited (показывает только для статистики)

**Приватный репозиторий:** 2000 минут/месяц бесплатно

---

## 🎉 Готово!

Теперь ты можешь:

✅ Собирать Windows .exe с Mac
✅ Не просить товарища собирать на Windows
✅ Автоматические сборки при push
✅ Мультиплатформенные релизы
✅ Делиться готовыми .exe с командой

**Просто жми кнопку и получай .exe!** 🚀

---

## 📞 Помощь

**Проблемы с GitHub Actions?**
- Смотри детальную документацию: [.github/workflows/README.md](.github/workflows/README.md)
- Логи сборки в интерфейсе GitHub Actions

**Проблемы с .exe файлом?**
- Смотри [WINDOWS_BUILD_INSTRUCTIONS.md](WINDOWS_BUILD_INSTRUCTIONS.md)

---

**Удачных сборок, товарищ!** 🚀
