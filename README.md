# KidsAccount

## Overview

**KidsAccount** appears to be a local desktop application for tracking a child’s brokerage and savings account under parental management. The repository suggests a small household finance tool with a Qt-based interface.

## Top-level contents

- `depot.png`
- `kids_money_qt.py`
- `kids_money_qt_updated.py`

## What this project appears to do

Based on the repository description and filenames, the application likely supports:
- tracking balances in a savings/depot setup,
- viewing contributions and totals,
- presenting financial information in a simple Qt UI,
- comparing an original and updated version of the app.

## Installation

No dependency file is visible at the top level, so a likely minimal setup is:

```bash
git clone https://github.com/RauchendeKanonen/KidsAccount.git
cd KidsAccount
python3 -m venv .venv
source .venv/bin/activate
pip install PyQt6
```

## Running

```bash
python3 kids_money_qt.py
```

or

```bash
python3 kids_money_qt_updated.py
```

## What the README should add

- what data is stored,
- whether the app uses local files or a database,
- whether values are entered manually,
- privacy expectations,
- backup/export workflow.

## Scope note

This project would benefit from a clear statement that it is a local household tool, not professional financial software.

## License

No visible license from the public top-level snapshot.
