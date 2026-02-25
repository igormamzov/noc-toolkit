# VS Code Settings

This directory contains VS Code workspace settings for the NOC Toolkit project.

## Files

### settings.json
Project-specific settings including:
- Python interpreter path
- Linting configuration (flake8)
- File exclusions
- Editor settings

### extensions.json
Recommended VS Code extensions for this project:
- **Python** - Python language support
- **Pylance** - Fast Python language server
- **Black Formatter** - Code formatting
- **Markdown All in One** - Markdown support
- **markdownlint** - Markdown linting

## Setup

When you open this project in VS Code, you'll be prompted to install recommended extensions. Click "Install All" to set up your environment.

## Python Interpreter

The default interpreter is set to:
```
/Users/master/miniconda3/bin/python3
```

If you're using a different Python installation or virtual environment, update `settings.json`:
```json
{
    "python.defaultInterpreterPath": "/path/to/your/python"
}
```

## Troubleshooting

### "Unable to handle ... python" warning
This usually means VS Code can't find the Python interpreter. Solutions:
1. Open Command Palette (Cmd+Shift+P)
2. Run "Python: Select Interpreter"
3. Choose the correct Python version

### Linting errors
If you see unexpected linting errors:
1. Install flake8: `pip install flake8`
2. Or disable linting in settings.json: `"python.linting.enabled": false`
